# Upstream Watchdog v1

## Purpose and boundary

The Upstream Watchdog prevents an unreachable Kharej host from causing a
long-running connection/socket storm on its matching Iran profile. One central
Python daemon discovers exact managed `iran-N` profiles, checks each unique
validated Ping or TCP target concurrently, and can control only the matching
`gost-iran-N.service`.

It is separate from Monitoring Lite and outside GOST's traffic process. It
does not modify tunnel env files, traffic units, firewall rules, runners, or
GOST command lines. Installation enables the central daemon but leaves every
profile Disabled, so install and update perform no GOST traffic service action.

## Exact defaults

```text
CHECK_MODE=ping
CHECK_INTERVAL_SECONDS=2
PING_TIMEOUT_SECONDS=1
FAILURE_THRESHOLD=10
SUCCESS_THRESHOLD=10
RECOVERY_HOLD_SECONDS=10
RECOVERY_JITTER_MAX_SECONDS=10
```

These defaults detect an outage after approximately 20 seconds of consecutive
failures. Recovery requires approximately 20 seconds of consecutive successes,
then a 10-second hold and a random 0-10-second jitter. Checks use a monotonic,
non-overlapping scheduler. Probe keys are `(CHECK_MODE, KHAREJ_IP, TUNNEL_PORT
when TCP, PING_TIMEOUT_SECONDS)`: profiles sharing every value reuse one result,
while a different mode, port, or timeout runs an independent check. At most 32
checks run concurrently. `PING_TIMEOUT_SECONDS` remains the compatible config
key but is the timeout for either probe mode.

Probe results are typed as `success`, `unreachable`, or `probe_error`. Ping
return code `1` is a normal unreachable result. In TCP mode, refused, timed
out, host/network unreachable, and reset-during-connect results are
`unreachable`. Local FD/memory exhaustion, permission errors, invalid target
configuration, name-resolution/runtime failures, a missing Ping capability, or
an unexpected local socket/command failure is `probe_error`. Probe errors do
not change success/failure counters and can never stop or start traffic. Persistent errors
produce one safe event per error transition and one recovery event, without
command lines, paths, stderr, or credentials.

## Modes and state machine

- `disabled`: no probe, state event, or service action.
- `monitor`: configured probe, state tracking, and transition history, with no
  service action.
- `auto`: the same checks plus the validated stop/recovery actions.

States are `unknown`, `healthy`, `degraded`, `down`, `recovering`, and
`maintenance`. A success resets failures; a failure resets recovery progress.
Auto Protect stops an active profile once at the failure threshold. It first
persists a durable action intent, queries the exact service immediately before
the action, verifies the result, and then finalizes ownership. A restart
reconciles unfinished stop and start intents for every valid profile before
probe scheduling, including Monitor and Disabled profiles. Reconciliation either
observes that the exact intended state already exists or executes the persisted
action once, verifies the result, finalizes matching ownership, and clears the
intent. A failed resumed action records one safe bounded event, enters manual
override, clears the intent, and is not retried each cycle. Disabled profiles
never run a probe; only a previously authorized durable action may be completed.
If the final ownership write fails after a stop, Watchdog attempts one safe
compensating start; a failed compensation leaves the durable intent available
for deterministic restart reconciliation. No Kharej or arbitrary unit can be
targeted.

A Watchdog-owned stop continues to receive checks. It is started once only
after the success threshold, full hold, bounded jitter, and a final healthy
check, provided mode remains `auto`, maintenance is off, and no manual override
exists. A failed start or stop does not enter a retry loop.

## Manual actions and maintenance

Service-state mismatches are treated as operator actions. The daemon records a
safe `watchdog_manual_override` transition and suspends automatic actions until
the operator explicitly uses `Re-arm manual override`. It never auto-starts an
operator-stopped profile. Manual start/stop reconciliation runs on a separate
10-second cadence rather than spawning one `systemctl is-active` process per
profile on every two-second probe cycle. Every actual start/stop still uses a
fresh exact-unit query.

Leaving Auto mode while a service is Watchdog-owned and stopped requires an
explicit choice: keep it stopped, start it now only if upstream is healthy, or
cancel. The ordinary mode-change path never silently changes service state.

Per-profile maintenance supports:

1. Enter and keep current service state.
2. Enter and stop the service now.
3. Exit without starting.
4. Exit and start only when upstream is healthy and the stop was maintenance-owned.

Checks may continue during maintenance, but automatic stop/start actions are
suspended. Maintenance ownership and Watchdog ownership are persisted
separately.

## Configuration and state

```text
/etc/gost-manager/watchdog.conf
/etc/gost-manager/watchdog.d/iran-N.conf
/var/lib/gost-manager/watchdog/watchdog.sqlite3
/usr/local/lib/gost-manager/gost_watchdog/
/usr/local/sbin/gost-upstream-watchdog
/usr/local/sbin/gost-watchdog-admin
/etc/systemd/system/gost-upstream-watchdog.service
```

Profile files may override mode, check mode, interval, timeout, thresholds,
hold, and jitter; omitted values inherit the global config. Parsing uses a
strict allowlist and never sources env files. The target reader streams exact
managed Iran env files and retains only `KHAREJ_IP` and `TUNNEL_PORT`; values
for `GOST_USER`, `GOST_PASS`, `MAPPINGS`, and every other key are ignored. A
missing, nonnumeric, or out-of-range TCP port produces a safe config/probe error
and no traffic action. Ping mode remains usable when `TUNNEL_PORT` is absent.
Writes are private, atomic, fsynced, and reject symlinks. Invalid config fails
closed without changing traffic. The reset/recovery command validates only the
exact managed Iran env/unit identity,
ignores the broken profile config, and atomically replaces only that profile
file with `MODE=disabled`. This safely recovers malformed content, unsafe
permissions, symlinked profile files, and overrides made incompatible by a
later global timing change.

SQLite schema v3 stores persistent state and transition/action events only. Each
profile event retains its allowlisted event-time check mode (`ping` or `tcp`);
global events retain `NULL`. Migration backfills profile-specific events from
Ping-only releases to `ping` without changing history or indexes. WAL, busy
timeout, fixed safe columns, indexed bounded queries, and batched pruning are
used. Events older than exactly 24 hours are removed. Individual probe attempts
are not written as events, and credentials are neither loaded nor stored.

## Operator workflow

Choose `12) Upstream Watchdog` to inspect status, set a profile to Monitor Only
or Auto Protect, edit overrides, select Ping/TCP, test the configured probe,
manage maintenance, inspect bounded history and outage summaries, configure
global defaults, inspect/restart only
the central service, or re-arm a manual override.

The equivalent noninteractive commands are:

```bash
gost-watchdog-admin set-check-mode iran-1 tcp
gost-watchdog-admin probe iran-1
gost-watchdog-admin set-check-mode iran-1 ping
```

Changing check mode writes only that profile's Watchdog config. It does not
restart GOST, change Auto Protect mode, or clear manual override. `profiles`,
`status`, JSON status, and `effective` expose the current effective check mode.
Events expose the mode stored when each event was created. Summary labels its
profile setting as `current_check_mode` in JSON and `current_check` in text so it
cannot be mistaken for the mode of every aggregated historical outage.

The status view includes the effective settings, service state, health state,
maintenance/manual-override ownership, counters, timestamps, and current outage
duration. Menu prompts use those machine-readable current effective values;
pressing Enter preserves them and changing one field writes only that override.
Detailed history is newest first and bounded. The 24-hour summary intersects
each completed or ongoing outage with the exact 24-hour window, so total and
longest downtime can never exceed 86,400 seconds.

`ping` is an installer runtime dependency mapped to Ubuntu's `iputils-ping`.
Before staging or replacing any file, installation runs the supported argv form
against `127.0.0.1`; missing authorization, missing Ping, insufficient
permission, or an incompatible implementation fails without mutation. This
local capability check never enables Auto Protect and never depends on a public
host.

## Probe limitations

A successful Ping proves host reachability, not that the remote SOCKS5 service
itself is healthy. A successful TCP connect proves that
`KHAREJ_IP:TUNNEL_PORT` accepted a connection; it does not perform SOCKS
authentication or verify end-to-end forwarding.

Both executors retain the same typed state machine, bounded worker pool, exact
unit validation, ownership, scheduling, and secret-handling rules.

## Controlled rollout

1. Update/install Watchdog runtime.
2. Confirm all profiles are Disabled and still use Ping unless explicitly changed.
3. Validate `KHAREJ_IP:TUNNEL_PORT`, select TCP for one approved profile, and
   run `gost-watchdog-admin probe iran-N`.
4. Enable that profile in Monitor Only and observe it.
5. Switch that profile to Auto Protect only after explicit manual re-arm and
   an approved controlled test.
6. Perform a controlled Kharej TCP outage.
7. Confirm the profile stops after the threshold, Iran host sockets remain
   bounded, checks continue while stopped, recovery starts the profile once
   after qualification, and no server reboot is required.
8. Roll out gradually to remaining profiles using
   `docs/GM-203-PRODUCTION-RUNBOOK.md`.

During the one-profile Monitor Only observation, record the Watchdog database
and WAL sizes, disk write rate, daemon CPU and RSS, Ping/TCP probe
rate, and systemctl subprocess rate. With ten profiles at the two-second
default, the current per-profile state write can reach five SQLite transactions
per second. That write rate is not a correctness blocker for the one-profile
staging observation, but it must be measured before enabling all profiles.

## Rollback

Set affected profiles to `disabled` first. The component-aware uninstaller can
stop and remove only `gost-upstream-watchdog.service` and its runtime while
preserving operator configuration and history. Purging Watchdog data requires
a separate confirmation and the exact phrase `DELETE WATCHDOG DATA`. Uninstall
never starts or stops a GOST traffic profile.

For a check-only rollback that preserves mode and ownership, use
`gost-watchdog-admin set-check-mode iran-N ping`. Never clear manual override or
restart a healthy traffic service as part of that rollback.
