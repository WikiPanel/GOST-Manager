# TASKS

## Completed v0.1 foundation

- Repository, manager, installer, uninstaller, Direct Mode runners, examples,
  documentation, and local validation.
- Official go-gost/gost release installation with architecture selection,
  checksum verification when available, backup, and executable installation.
- Numbered Kharej SOCKS5 tunnel management with narrowly owned firewall rules.
- Numbered Iran forwarding tunnel management with strict mapping and busy-port
  validation.
- Numbered tunnel selection for delete, status, logs, and restart.

## Completed v0.2 monitoring

- Python standard-library collector and query layer.
- SQLite schema version 4, WAL, transactions, rollups, and bounded retention.
- Exact/derived/estimated/unavailable metric semantics.
- Host, network, TCP, Direct Mode GOST service, tunnel, and collector metrics.
- Live, 10-minute, 30-minute, and 1-hour operator views.
- Bounded exports, query limits, source isolation, transition-aware events,
  and deterministic performance/storage tests.
- Atomic installer rollback and component-aware safe uninstall.

## Issue #28 - Product scope reset

Status: Implemented

The supported v0.2 product is now:

- official upstream GOST Direct Mode only;
- multiple independent Iran and Kharej profiles;
- optional local Monitoring Lite without NGINX;
- local install, service management, and safe uninstall.

NGINX Gateway and Native GOST Gateway are cancelled. Gateway desired state,
routes, bindings, secret store, runtime services, synchronization, HA, and
firewall roadmap items are removed from the active product plan.

## Issue #29 - Multi-server Direct Mode profile management

Status: Future

Issue #29 is the only named follow-up in this scope document. Its behavior and
acceptance criteria will be defined and implemented in a separate focused pull
request. No Issue #29 functionality is included in the Issue #28 cleanup.
