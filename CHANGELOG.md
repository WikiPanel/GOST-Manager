# Changelog

## Unreleased
- Hardened the monitoring collector core for the issue #8 compatibility contract: production `MAPPINGS`/`TUNNEL_PORT` env parsing, SQLite WAL/busy-timeout storage, versioned v1-to-v2 migrations, quality-labelled metrics, structured events, bounded retention, incremental minute rollups, and deterministic collector tests.
- Completed issue #11 host, network, TCP/IP, storage, service, process, socket, tunnel, and collector-self metric coverage with injectable sources, persistent delta state, and deduplicated transition events.
- Split the monitoring collector into focused standard-library modules while preserving schema v4 and existing Direct Mode env compatibility.
- Made socket collection status-aware, split listener and full-connection cadences, corrected reserved filesystem capacity, and isolated source and entity failures without emitting false listener transitions.
- Aggregated multi-process systemd units from cgroup PID sets, separated fast and slow process sources, added endpoint-aware tunnel socket counts and checkpoint timing, and documented the 48-hour monitoring storage budget.
- Included 30-day minute rollups and operational headroom in the monitoring storage budget, and prevented incomplete or MainPID-fallback snapshots from emitting false process-replacement events.
- Bounded structured-event storage with an explicit 30-day retention policy enforced by atomic maintenance and reflected independently in capacity estimates.

## 0.1.2
- Removed GitHub Actions workflow because validation is handled locally.

## 0.1.1
- Fixed Iran tunnel creation to always require `Port mappings`.
- Added stricter mapping tests for empty values, invalid formats, and duplicate listen ports.
- Changed delete, status, logs, and restart flows to use a smart numbered tunnel selector.
- Added tunnel discovery for service/env orphans so broken tunnels can still be selected.
- Updated documentation for the mapping prompt and selector UX.

## 0.1.0
- Initial GOST Manager release.
- Added GOST install/update from official GitHub Releases.
- Added Kharej SOCKS5 tunnel creation.
- Added Iran TCP forwarding tunnel creation.
- Added numbered systemd services.
- Added env-based configuration.
- Added status, logs, restart, delete, list, and cleanup options.
- Added documentation, examples, tests, and local validation.
