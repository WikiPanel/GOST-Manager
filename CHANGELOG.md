# Changelog

## Unreleased
- Hardened the monitoring collector core for the issue #8 compatibility contract: production `MAPPINGS`/`TUNNEL_PORT` env parsing, SQLite WAL/busy-timeout storage, versioned v1-to-v2 migrations, quality-labelled metrics, structured events, bounded retention, incremental minute rollups, and deterministic collector tests.
- Completed issue #11 host, network, TCP/IP, storage, service, process, socket, tunnel, and collector-self metric coverage with injectable sources, persistent delta state, and deduplicated transition events.
- Split the monitoring collector into focused standard-library modules while preserving schema v4 and existing Direct Mode env compatibility.

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
