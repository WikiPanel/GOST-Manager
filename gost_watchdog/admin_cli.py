"""Administration and status CLI for the central Upstream Watchdog."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from pathlib import Path

from gost_watchdog.commands import CommandError, SubprocessPingExecutor, SystemdController
from gost_watchdog.config import (
    DEFAULT_GLOBAL_CONFIG_PATH,
    DEFAULT_PROFILE_CONFIG_DIR,
    ConfigError,
    atomic_write_config,
    global_config_from_mapping,
    load_global_config,
    parse_profile_values,
    profile_config_from_mapping,
    render_global_config,
    render_profile_values,
    rooted_path,
)
from gost_watchdog.daemon import DEFAULT_ENV_DIR, DEFAULT_UNIT_DIR
from gost_watchdog.engine import MaintenanceController
from gost_watchdog.models import GlobalConfig, ManagedProfile, ProfileState
from gost_watchdog.profiles import (
    ProfileError,
    discover_profiles,
    load_managed_profile_identity,
    validate_profile_id,
)
from gost_watchdog.storage import (
    DEFAULT_DB_PATH,
    EVENT_RETENTION_SECONDS,
    SCHEMA_VERSION,
    WatchdogStore,
)


EXIT_INVALID = 2
EXIT_RUNTIME = 3


class AdminError(RuntimeError):
    exit_code = 1


class AdminInputError(AdminError):
    exit_code = EXIT_INVALID


class AdminRuntimeError(AdminError):
    exit_code = EXIT_RUNTIME


@dataclasses.dataclass(frozen=True)
class Context:
    global_config_path: Path
    profile_config_dir: Path
    env_dir: Path
    unit_dir: Path
    db_path: Path
    boundary: Path | None
    expected_uid: int | None
    owner_uid: int | None


def _context(args: argparse.Namespace) -> Context:
    root = args.path_root or None
    boundary = Path(root) if root else None
    installed = args.policy == "installed"
    expected_uid = (os.geteuid() if root else 0) if installed else None
    owner_uid = expected_uid
    return Context(
        global_config_path=rooted_path(args.config, root),
        profile_config_dir=rooted_path(args.profile_config_dir, root),
        env_dir=rooted_path(args.env_dir, root),
        unit_dir=rooted_path(args.unit_dir, root),
        db_path=rooted_path(args.db, root),
        boundary=boundary,
        expected_uid=expected_uid,
        owner_uid=owner_uid,
    )


def _global(context: Context) -> GlobalConfig:
    return load_global_config(
        context.global_config_path,
        expected_uid=context.expected_uid,
        boundary=context.boundary,
    )


def _profiles(
    context: Context,
    *,
    require_all_valid: bool = False,
) -> tuple[list[ManagedProfile], list[tuple[str | None, str]]]:
    profiles, errors = discover_profiles(
        context.env_dir,
        context.unit_dir,
        context.profile_config_dir,
        _global(context),
        expected_uid=context.expected_uid,
        boundary=context.boundary,
    )
    if require_all_valid and errors:
        raise AdminInputError(f"invalid Watchdog profile configuration: {errors[0][0] or 'global'}")
    return profiles, errors


def _profile(context: Context, profile_id: str) -> ManagedProfile:
    try:
        validate_profile_id(profile_id)
    except ProfileError as exc:
        raise AdminInputError(str(exc)) from exc
    profiles, _errors = _profiles(context)
    for profile in profiles:
        if profile.profile_id == profile_id:
            return profile
    raise AdminInputError("profile is missing, malformed, unsafe, or not managed")


def _existing_profile_values(context: Context, profile_id: str) -> dict[str, str]:
    path = context.profile_config_dir / f"{profile_id}.conf"
    if not path.exists() and not path.is_symlink():
        return {}
    try:
        return parse_profile_values(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, ConfigError) as exc:
        raise AdminInputError("existing profile config is invalid; reset it first") from exc


def _write_profile_values(
    context: Context,
    profile: ManagedProfile,
    values: dict[str, str],
) -> None:
    global_config = _global(context)
    profile_config_from_mapping(values, global_config)
    atomic_write_config(
        context.profile_config_dir / f"{profile.profile_id}.conf",
        render_profile_values(values, global_config),
        owner_uid=context.owner_uid,
        owner_gid=0 if context.owner_uid == 0 else os.getegid(),
        boundary=context.boundary,
    )


def _state_dict(
    profile: ManagedProfile,
    state: ProfileState,
    service_active: bool | None,
    summary: dict[str, object],
    now: int,
) -> dict[str, object]:
    outage = None
    if state.outage_started_at is not None:
        outage = max(0, now - state.outage_started_at)
    return {
        "profile_id": profile.profile_id,
        "kharej_ip": profile.kharej_ip,
        "mode": profile.config.mode,
        "watchdog_state": state.display_state,
        "service_active": service_active,
        "maintenance": state.maintenance,
        "manual_override": state.manual_override,
        "stopped_by_watchdog": state.stopped_by_watchdog,
        "stopped_by_maintenance": state.stopped_by_maintenance,
        "failure_count": state.failure_count,
        "success_count": state.success_count,
        "check_status": state.check_status,
        "probe_error_category": state.last_probe_error_category,
        "pending_action": state.pending_action,
        "check_interval_seconds": profile.config.check_interval_seconds,
        "ping_timeout_seconds": profile.config.ping_timeout_seconds,
        "failure_threshold": profile.config.failure_threshold,
        "success_threshold": profile.config.success_threshold,
        "recovery_hold_seconds": profile.config.recovery_hold_seconds,
        "recovery_jitter_max_seconds": profile.config.recovery_jitter_max_seconds,
        "last_check_at": state.last_check_at,
        "last_transition_at": state.last_transition_at,
        "outage_duration_seconds": outage,
        "summary_24h": summary,
    }


def _open_store(context: Context) -> WatchdogStore:
    try:
        return WatchdogStore(str(context.db_path))
    except (OSError, RuntimeError) as exc:
        raise AdminRuntimeError("Watchdog database is unavailable") from exc


def command_validate(context: Context, all_profiles: bool) -> int:
    _global(context)
    if all_profiles:
        _profiles(context, require_all_valid=True)
    print("Watchdog config valid")
    return 0


def command_migrate(context: Context) -> int:
    store = _open_store(context)
    store.close()
    try:
        os.chmod(context.db_path, 0o600)
        if context.owner_uid is not None:
            os.chown(
                context.db_path,
                context.owner_uid,
                0 if context.owner_uid == 0 else os.getegid(),
            )
    except OSError as exc:
        raise AdminRuntimeError("Watchdog database permissions could not be enforced") from exc
    print(f"Watchdog schema version: {SCHEMA_VERSION}")
    return 0


def command_profiles(context: Context) -> int:
    profiles, errors = _profiles(context)
    for profile in profiles:
        print(f"{profile.profile_id}\t{profile.kharej_ip}\t{profile.config.mode}")
    for profile_id, _category in errors:
        if profile_id:
            print(f"{profile_id}\tUNAVAILABLE\tdisabled")
    return 0


def command_effective(context: Context, profile_id: str, as_json: bool) -> int:
    profile = _profile(context, profile_id)
    values = dataclasses.asdict(profile.config)
    if as_json:
        print(json.dumps(values, sort_keys=True))
    else:
        for key, value in values.items():
            print(f"{key}={value}")
    return 0


def command_effective_global(context: Context, as_json: bool) -> int:
    values = dataclasses.asdict(_global(context))
    if as_json:
        print(json.dumps(values, sort_keys=True))
    else:
        for key, value in values.items():
            print(f"{key}={value}")
    return 0


def command_status(context: Context, profile_id: str | None, as_json: bool) -> int:
    profiles, errors = _profiles(context)
    if profile_id is not None:
        profiles = [profile for profile in profiles if profile.profile_id == profile_id]
        if not profiles:
            raise AdminInputError("managed profile was not found")
    store = _open_store(context)
    systemd = SystemdController()
    now = int(time.time())
    output: list[dict[str, object]] = []
    try:
        for profile in profiles:
            state = store.get_state(
                profile.profile_id, profile.service_name, profile.kharej_ip
            )
            try:
                active: bool | None = systemd.is_active(profile.service_name)
            except CommandError:
                active = None
            output.append(
                _state_dict(
                    profile,
                    state,
                    active,
                    store.summary(now, profile.profile_id),
                    now,
                )
            )
    finally:
        store.close()
    if as_json:
        print(json.dumps({"profiles": output, "errors": errors}, sort_keys=True))
        return 0
    if not output:
        print("No valid managed Iran profiles found.")
        return 0
    print("PROFILE  KHAREJ IP  MODE      STATE        SERVICE   MAINT  OVERRIDE  OWNER  FAIL/SUCCESS")
    for item in output:
        service = "unknown" if item["service_active"] is None else (
            "active" if item["service_active"] else "inactive"
        )
        display_state = (
            item["check_status"]
            if item["check_status"] == "probe_error"
            else item["watchdog_state"]
        )
        print(
            f"{item['profile_id']:<8} {item['kharej_ip']:<10} {item['mode']:<9} "
            f"{display_state:<12} {service:<9} "
            f"{str(item['maintenance']).lower():<6} {str(item['manual_override']).lower():<9} "
            f"{str(item['stopped_by_watchdog']).lower():<6} "
            f"{item['failure_count']}/{item['success_count']}"
        )
    return 0


def command_set_mode(
    context: Context,
    profile_id: str,
    mode: str,
    owned_action: str | None,
) -> int:
    profile = _profile(context, profile_id)
    if owned_action is not None and not (
        profile.config.mode == "auto" and mode in {"monitor", "disabled"}
    ):
        raise AdminInputError("owned service action is only valid when leaving Auto mode")
    store = _open_store(context)
    try:
        state = store.get_state(
            profile.profile_id, profile.service_name, profile.kharej_ip
        )
        needs_choice = (
            profile.config.mode == "auto"
            and mode in {"monitor", "disabled"}
            and state.stopped_by_watchdog
        )
        if needs_choice and owned_action is None:
            raise AdminInputError(
                "service is stopped by Watchdog; disabling Auto recovery requires "
                "--owned-action keep-stopped or start-if-healthy"
            )
        if needs_choice and owned_action == "start-if-healthy":
            MaintenanceController(store, SystemdController()).start_owned_for_mode_change(
                profile
            )
    except (ValueError, CommandError) as exc:
        raise AdminRuntimeError(str(exc)) from exc
    finally:
        store.close()
    values = _existing_profile_values(context, profile_id)
    values["MODE"] = mode
    _write_profile_values(context, profile, values)
    if needs_choice and owned_action == "keep-stopped":
        print(
            "Warning: service remains stopped and automatic recovery is now disabled."
        )
    print(f"{profile_id} mode set to {mode}")
    return 0


def command_configure_profile(context: Context, args: argparse.Namespace) -> int:
    profile = _profile(context, args.profile_id)
    if args.mode is not None and args.mode != profile.config.mode:
        raise AdminInputError(
            "mode changes must use set-mode so stopped-service ownership is handled"
        )
    values = _existing_profile_values(context, args.profile_id)
    option_map = {
        "mode": "MODE",
        "check_interval": "CHECK_INTERVAL_SECONDS",
        "ping_timeout": "PING_TIMEOUT_SECONDS",
        "failure_threshold": "FAILURE_THRESHOLD",
        "success_threshold": "SUCCESS_THRESHOLD",
        "recovery_hold": "RECOVERY_HOLD_SECONDS",
        "recovery_jitter": "RECOVERY_JITTER_MAX_SECONDS",
    }
    changed = False
    for attribute, key in option_map.items():
        value = getattr(args, attribute)
        if value is not None:
            values[key] = str(value)
            changed = True
    if not changed:
        raise AdminInputError("at least one profile setting is required")
    _write_profile_values(context, profile, values)
    return command_effective(context, args.profile_id, False)


def command_reset_profile(context: Context, profile_id: str) -> int:
    try:
        profile = load_managed_profile_identity(
            profile_id,
            context.env_dir,
            context.unit_dir,
            context.profile_config_dir,
            _global(context),
            expected_uid=context.expected_uid,
        )
    except ProfileError as exc:
        raise AdminInputError(str(exc)) from exc
    global_config = _global(context)
    atomic_write_config(
        context.profile_config_dir / f"{profile.profile_id}.conf",
        render_profile_values({"MODE": "disabled"}, global_config),
        owner_uid=context.owner_uid,
        owner_gid=0 if context.owner_uid == 0 else os.getegid(),
        boundary=context.boundary,
        recovery_replace=True,
    )
    print(f"{profile_id} overrides reset; mode is disabled")
    return 0


def command_set_global(context: Context, args: argparse.Namespace) -> int:
    config = _global(context)
    values = {
        "CHECK_MODE": config.check_mode,
        "CHECK_INTERVAL_SECONDS": str(config.check_interval_seconds),
        "PING_TIMEOUT_SECONDS": str(config.ping_timeout_seconds),
        "FAILURE_THRESHOLD": str(config.failure_threshold),
        "SUCCESS_THRESHOLD": str(config.success_threshold),
        "RECOVERY_HOLD_SECONDS": str(config.recovery_hold_seconds),
        "RECOVERY_JITTER_MAX_SECONDS": str(config.recovery_jitter_max_seconds),
    }
    option_map = {
        "check_interval": "CHECK_INTERVAL_SECONDS",
        "ping_timeout": "PING_TIMEOUT_SECONDS",
        "failure_threshold": "FAILURE_THRESHOLD",
        "success_threshold": "SUCCESS_THRESHOLD",
        "recovery_hold": "RECOVERY_HOLD_SECONDS",
        "recovery_jitter": "RECOVERY_JITTER_MAX_SECONDS",
    }
    changed = False
    for attribute, key in option_map.items():
        value = getattr(args, attribute)
        if value is not None:
            values[key] = str(value)
            changed = True
    if not changed:
        raise AdminInputError("at least one global setting is required")
    updated = global_config_from_mapping(values)
    atomic_write_config(
        context.global_config_path,
        render_global_config(updated),
        owner_uid=context.owner_uid,
        owner_gid=0 if context.owner_uid == 0 else os.getegid(),
        boundary=context.boundary,
    )
    print(render_global_config(updated), end="")
    return 0


def command_ping(context: Context, profile_id: str) -> int:
    profile = _profile(context, profile_id)
    result = SubprocessPingExecutor()(
        profile.kharej_ip, profile.config.ping_timeout_seconds
    )
    print(f"{profile.profile_id} ping: {result.status}")
    return 0 if result.status == "success" else 1


def command_events(
    context: Context,
    profile_id: str | None,
    limit: int,
    as_json: bool,
) -> int:
    if profile_id is not None:
        validate_profile_id(profile_id)
    store = _open_store(context)
    try:
        rows = store.events(int(time.time()), profile_id=profile_id, limit=limit)
    finally:
        store.close()
    if as_json:
        print(json.dumps(rows, sort_keys=True))
    else:
        for row in rows:
            print(
                f"{row['ts']} {row['profile_id'] or '-'} {row['code']} "
                f"{row['previous_state'] or '-'}->{row['new_state'] or '-'} "
                f"{row['action_result'] or '-'}"
            )
    return 0


def command_summary(context: Context, profile_id: str | None, as_json: bool) -> int:
    profiles, _errors = _profiles(context)
    if profile_id is not None:
        profiles = [profile for profile in profiles if profile.profile_id == profile_id]
        if not profiles:
            raise AdminInputError("managed profile was not found")
    store = _open_store(context)
    now = int(time.time())
    try:
        output = {
            profile.profile_id: store.summary(now, profile.profile_id)
            for profile in profiles
        }
    finally:
        store.close()
    if as_json:
        print(json.dumps(output, sort_keys=True))
    else:
        for current, values in output.items():
            print(
                f"{current}: outages={values['outage_count']} "
                f"downtime={values['total_downtime_seconds']}s "
                f"longest={values['longest_outage_seconds']}s "
                f"stops={values['automatic_stop_count']} "
                f"starts={values['automatic_start_count']} "
                f"failed_actions={values['failed_action_count']}"
            )
    return 0


def command_maintenance(context: Context, profile_id: str, action: str) -> int:
    profile = _profile(context, profile_id)
    store = _open_store(context)
    try:
        state = MaintenanceController(store, SystemdController()).apply(profile, action)
    except (ValueError, CommandError) as exc:
        raise AdminRuntimeError(str(exc)) from exc
    finally:
        store.close()
    print(f"{profile_id} maintenance={str(state.maintenance).lower()}")
    return 0


def command_rearm(context: Context, profile_id: str) -> int:
    profile = _profile(context, profile_id)
    store = _open_store(context)
    try:
        state = MaintenanceController(store, SystemdController()).rearm(profile)
    except (ValueError, CommandError) as exc:
        raise AdminRuntimeError(str(exc)) from exc
    finally:
        store.close()
    print(f"{profile_id} manual_override={str(state.manual_override).lower()}")
    return 0


def _add_timing_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--check-interval", type=int)
    parser.add_argument("--ping-timeout", type=int)
    parser.add_argument("--failure-threshold", type=int)
    parser.add_argument("--success-threshold", type=int)
    parser.add_argument("--recovery-hold", type=int)
    parser.add_argument("--recovery-jitter", type=int)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gost-watchdog-admin")
    parser.add_argument("--policy", choices=("generic", "installed"), default="generic")
    parser.add_argument("--path-root")
    parser.add_argument("--config", default=DEFAULT_GLOBAL_CONFIG_PATH)
    parser.add_argument("--profile-config-dir", default=DEFAULT_PROFILE_CONFIG_DIR)
    parser.add_argument("--env-dir", default=DEFAULT_ENV_DIR)
    parser.add_argument("--unit-dir", default=DEFAULT_UNIT_DIR)
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-config")
    validate.add_argument("--all", action="store_true")
    subparsers.add_parser("migrate")
    subparsers.add_parser("profiles")
    effective = subparsers.add_parser("effective")
    effective.add_argument("profile_id")
    effective.add_argument("--json", action="store_true")
    effective_global = subparsers.add_parser("effective-global")
    effective_global.add_argument("--json", action="store_true")
    status = subparsers.add_parser("status")
    status.add_argument("--profile")
    status.add_argument("--json", action="store_true")
    set_mode = subparsers.add_parser("set-mode")
    set_mode.add_argument("profile_id")
    set_mode.add_argument("mode", choices=("disabled", "monitor", "auto"))
    set_mode.add_argument(
        "--owned-action", choices=("keep-stopped", "start-if-healthy")
    )
    configure = subparsers.add_parser("configure-profile")
    configure.add_argument("profile_id")
    configure.add_argument("--mode", choices=("disabled", "monitor", "auto"))
    _add_timing_arguments(configure)
    reset = subparsers.add_parser("reset-profile")
    reset.add_argument("profile_id")
    global_parser = subparsers.add_parser("set-global")
    _add_timing_arguments(global_parser)
    ping = subparsers.add_parser("ping")
    ping.add_argument("profile_id")
    events = subparsers.add_parser("events")
    events.add_argument("--profile")
    events.add_argument("--limit", type=int, default=200)
    events.add_argument("--json", action="store_true")
    summary = subparsers.add_parser("summary")
    summary.add_argument("--profile")
    summary.add_argument("--json", action="store_true")
    maintenance = subparsers.add_parser("maintenance")
    maintenance.add_argument("profile_id")
    maintenance.add_argument(
        "action", choices=("enter-keep", "enter-stop", "exit-no-start", "exit-start")
    )
    rearm = subparsers.add_parser("rearm")
    rearm.add_argument("profile_id")
    return parser


def dispatch(args: argparse.Namespace) -> int:
    context = _context(args)
    if args.command == "validate-config":
        return command_validate(context, args.all)
    if args.command == "migrate":
        return command_migrate(context)
    if args.command == "profiles":
        return command_profiles(context)
    if args.command == "effective":
        return command_effective(context, args.profile_id, args.json)
    if args.command == "effective-global":
        return command_effective_global(context, args.json)
    if args.command == "status":
        return command_status(context, args.profile, args.json)
    if args.command == "set-mode":
        return command_set_mode(
            context, args.profile_id, args.mode, args.owned_action
        )
    if args.command == "configure-profile":
        return command_configure_profile(context, args)
    if args.command == "reset-profile":
        return command_reset_profile(context, args.profile_id)
    if args.command == "set-global":
        return command_set_global(context, args)
    if args.command == "ping":
        return command_ping(context, args.profile_id)
    if args.command == "events":
        return command_events(context, args.profile, args.limit, args.json)
    if args.command == "summary":
        return command_summary(context, args.profile, args.json)
    if args.command == "maintenance":
        return command_maintenance(context, args.profile_id, args.action)
    if args.command == "rearm":
        return command_rearm(context, args.profile_id)
    raise AdminInputError("unsupported command")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return dispatch(args)
    except (ConfigError, ProfileError, AdminError) as exc:
        code = exc.exit_code if isinstance(exc, AdminError) else EXIT_INVALID
        print(f"Error: {exc}", file=sys.stderr)
        return code
    except (OSError, CommandError) as exc:
        print(f"Error: Watchdog operation failed safely: {exc}", file=sys.stderr)
        return EXIT_RUNTIME


if __name__ == "__main__":
    raise SystemExit(main())
