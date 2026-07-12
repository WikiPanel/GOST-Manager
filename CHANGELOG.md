# Changelog

## Unreleased
- Reset the operator experience to Monitoring Lite with 10/30/60/900-second cadences, 6-hour raw and 24-hour rollup/event retention, a compact seven-item menu, retained Advanced tools, a sub-1-GiB capacity model, and a deterministic 1,000-user performance fixture.
- Corrected final monitoring integration blockers with host-based Linux systemd verification, installed-path policy and configured DB resolution, collector/admin locking, crash-safe WAL-aware purge, exact runtime manifest and package mapping, metadata-safe installation rollback, and post-failure dependency rechecks in uninstall.
- Integrated the local monitoring package with strict configuration, hardened isolated systemd service, atomic install/upgrade rollback, operator menu workflows, admin maintenance/history purge, and dependency-aware safe uninstall while preserving Direct Mode traffic.
- Enforced metric semantics in minute/auto exports, made health incidents recovery-aware and current-membership scoped, surfaced malformed managed env sources safely, bounded compact interface membership, and added strict watermark validation plus a one-million-row streaming scan ceiling.
- Made monitoring queries rollup-watermark aware, preserved lagging raw tails with bounded streaming, limited current health to active collector membership, made health events direction/overflow aware, corrected unavailable rollup weighting, and centralized metric statistics semantics.
- Corrected the monitoring query UI after technical review with mixed-cadence latest-per-series snapshots, coherent read transactions, cost-aware 583-series history planning, required/optional service health, independent health events, complete CSV metadata/summary parity, and bounded concurrent exports.
- Added an independent read-only monitoring query layer with raw/rollup/hybrid summaries, observational health views, plain and ANSI dashboards, detail/event commands, and bounded atomic JSON/CSV export.
- Hardened the monitoring collector core for the issue #8 compatibility contract: production `MAPPINGS`/`TUNNEL_PORT` env parsing, SQLite WAL/busy-timeout storage, versioned v1-to-v2 migrations, quality-labelled metrics, structured events, bounded retention, incremental minute rollups, and deterministic collector tests.
- Completed issue #11 host, network, TCP/IP, storage, service, process, socket, tunnel, and collector-self metric coverage with injectable sources, persistent delta state, and deduplicated transition events.
- Split the monitoring collector into focused standard-library modules while preserving schema v4 and existing Direct Mode env compatibility.
- Made socket collection status-aware, split listener and full-connection cadences, corrected reserved filesystem capacity, and isolated source and entity failures without emitting false listener transitions.
- Aggregated multi-process systemd units from cgroup PID sets, separated fast and slow process sources, added endpoint-aware tunnel socket counts and checkpoint timing, and documented bounded monitoring storage.
- Included minute rollups and operational headroom in the monitoring storage budget, and prevented incomplete or MainPID-fallback snapshots from emitting false process-replacement events.
- Bounded structured-event storage with an explicit independent retention policy enforced by atomic maintenance and reflected in capacity estimates.

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
