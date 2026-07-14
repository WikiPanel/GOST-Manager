# Changelog

## Unreleased

- Classified hosts with neither conntrack sysctl file as an unsupported
  optional monitoring capability, preserving historical error counters while
  stopping repeated errors and showing the state explicitly in snapshots.
- Added an idempotent Server Stability wizard that verifies bounded sysctl
  tuning and installs exact per-profile systemd resource-limit drop-ins
  without restarting GOST or changing Direct Mode configuration.
- Simplified v0.2 to official upstream GOST Direct Mode and optional local
  Monitoring Lite.
- Removed the unreleased Gateway desired-state/runtime packages, launchers,
  runner, tests, installer/uninstaller integration, menu placeholder, and
  Gateway-only documentation.
- Added independent multi-server Direct Mode profile management with numeric
  gap allocation, optional labels, safe list/detail/edit/clone workflows,
  exact selected/all restart actions, global configured/live port checks, and
  profile-isolated rollback.
- Added canonical `ALLOWED_IRAN_SOURCES` support while retaining legacy
  `IRAN_IP`, ordered profile-scoped firewall rules, exact firewall rollback,
  hidden confirmed password input, strict non-executable env parsing, and
  atomic same-directory env/unit writes.
- Extended schema-version-4 Monitoring Lite entity metadata with stable
  profile number, optional label, and safe canonical allowed-source metadata;
  existing history and unlabeled profiles remain compatible.
- Cancelled NGINX Gateway and Native GOST Gateway; no controller, failover,
  connection migration, shared traffic process, or GOST source change was
  introduced.
- Removed NGINX discovery, current entities, rendering, health rules, fixtures,
  and capacity assumptions from Monitoring Lite while preserving generic
  multi-process collection.
- Preserved existing Iran/Kharej env files, Direct Mode units and service
  states, official upstream GOST installation, SQLite schema version 4,
  monitoring history, cadence, and retention.
- Updated the representative six-service Monitoring Lite profile to 485 fast
  series, 9 socket series, 48 slow series, and about 542 rollup series, with a
  conservative 0.451 GiB database estimate and 1 GiB reservation.

## Monitoring development history

- Added Monitoring Lite with 10/30/60/900-second cadences, 6-hour raw and
  24-hour rollup/event retention, a compact seven-item menu, retained Advanced
  tools, and deterministic performance fixtures.
- Added host-based Linux systemd verification, installed-path policy,
  configured DB resolution, collector/admin locking, crash-safe WAL-aware
  purge, an exact runtime manifest, metadata-safe installation rollback, and
  dependency-aware uninstall.
- Added strict monitoring configuration, a hardened isolated systemd service,
  atomic install/upgrade rollback, operator menu workflows, maintenance, and
  history purge while preserving Direct Mode traffic.
- Added bounded raw/rollup/hybrid queries, observational health, live and
  historical dashboards, JSON/CSV export parity, a one-million-row streaming
  ceiling, concurrent reads, and indexed query-plan tests.
- Added schema v4 host, network, TCP/IP, storage, service, process, socket,
  tunnel, and collector-self metric coverage with injectable sources,
  persistent delta state, quality labels, and deduplicated transition events.
- Added multi-process cgroup PID aggregation, split fast/slow process cadence,
  endpoint-aware Direct Mode socket counts, checkpoint timing, bounded source
  state, and storage budgeting.
- Bounded structured-event storage with an explicit independent retention
  policy enforced by atomic maintenance.

## 0.1.2

- Removed GitHub Actions workflow because validation is handled locally.

## 0.1.1

- Fixed Iran tunnel creation to always require Port mappings.
- Added stricter mapping tests for empty values, invalid formats, and duplicate
  listen ports.
- Changed delete, status, logs, and restart flows to use a smart numbered
  tunnel selector.
- Added tunnel discovery for service/env orphans so broken tunnels can still
  be selected.
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
