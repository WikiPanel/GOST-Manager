"""Runtime assertions executed inside the hardened transient Watchdog unit."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from gost_watchdog.commands import SubprocessPingExecutor
from gost_watchdog.daemon import RuntimeLoader, WatchdogDaemon
from gost_watchdog.models import Clock
from gost_watchdog.storage import WatchdogStore
from watchdog_soak import run_benchmark


def _systemctl(*arguments: str) -> str:
    result = subprocess.run(
        ["systemctl", *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        shell=False,
        timeout=15.0,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("systemctl AF_UNIX communication failed")
    return result.stdout


def _traffic_snapshot() -> str:
    return _systemctl(
        "list-units",
        "--all",
        "--plain",
        "--no-legend",
        "gost-iran-*.service",
        "gost-kharej-*.service",
    )


def main() -> int:
    root = Path(sys.argv[1])
    state_directory = root / "var/lib/gost-manager/watchdog"
    before_files = {
        path: path.read_bytes()
        for path in (
            root / "etc/gost-manager/watchdog.conf",
            root / "etc/gost/iran-1.env",
            root / "etc/systemd/system/gost-iran-1.service",
        )
    }
    ping = SubprocessPingExecutor()("127.0.0.1", 1)
    if ping.status != "success":
        raise RuntimeError(f"sandbox Ping failed safely as {ping.status}")
    traffic_before = _traffic_snapshot()

    loader = RuntimeLoader(
        path_root=str(root),
        installed=False,
    )
    profiles, errors, _interval = loader.load()
    if errors or len(profiles) != 1 or profiles[0].config.mode != "disabled":
        raise RuntimeError("sandbox profile did not remain Disabled")

    def forbidden_ping(_destination: str, _timeout: int) -> object:
        raise AssertionError("Disabled profile attempted Ping")

    store = WatchdogStore(str(state_directory / "runtime.sqlite3"))
    try:
        WatchdogDaemon(
            store,
            loader,
            ping_executor=forbidden_ping,  # type: ignore[arg-type]
            clock=Clock(),
        ).run_cycle()
    finally:
        store.close()
    if _traffic_snapshot() != traffic_before:
        raise RuntimeError("Watchdog runtime changed a traffic unit")
    if any(path.read_bytes() != content for path, content in before_files.items()):
        raise RuntimeError("Watchdog runtime changed managed traffic configuration")

    writable_marker = state_directory / "sandbox-write-ok"
    writable_marker.write_text("ok\n", encoding="ascii")
    forbidden = root / "etc/gost-manager/watchdog.conf"
    try:
        with forbidden.open("a", encoding="ascii") as stream:
            stream.write("unsafe\n")
    except OSError:
        pass
    else:
        raise RuntimeError("sandbox allowed a forbidden read-only config write")

    metrics = run_benchmark(state_directory)
    print(
        "WATCHDOG_RUNTIME "
        f"ping={ping.status} af_unix=ok profiles_disabled={len(profiles)} "
        "traffic_actions=0 forbidden_write=blocked"
    )
    print(
        "WATCHDOG_SOAK "
        + " ".join(f"{key}={value}" for key, value in metrics.items())
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
