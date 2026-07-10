# TASKS

## Task #1 - Bootstrap GOST Manager repository
Status: Done

Scope:
- Create project structure
- Add main Bash manager
- Add installer and uninstaller
- Add runner scripts
- Add examples
- Add docs
- Add tests
- Add local validation commands

Validation:
- bash syntax check
- shellcheck
- unit-style Bash tests

## Task #2 - Implement GOST official release installer
Status: Done

Scope:
- Detect architecture
- Download latest stable release from official go-gost/gost GitHub Releases
- Verify checksum when available
- Backup existing binary
- Install /usr/local/bin/gost
- Show gost version

## Task #3 - Implement Kharej tunnel management
Status: Done

Scope:
- Create numbered kharej env
- Create numbered systemd service
- Start/enable service
- Optional iptables allow/drop rules
- Safe firewall deletion

## Task #4 - Implement Iran tunnel management
Status: Done

Scope:
- Parse port mappings
- Validate busy listen ports
- Create numbered iran env
- Create numbered systemd service
- Start/enable service
- Print test commands

## Task #5 - Documentation and operator runbook
Status: Done

Scope:
- English README
- Persian README
- Usage docs
- Operations docs
- Security docs
- Development docs

## Task #6 - Fix Iran mappings prompt and tunnel selector UX
Status: Done

Scope:
- Require `Port mappings` during Iran tunnel creation
- Reject empty, invalid, out-of-range, and duplicate listen port mappings
- Abort before writing files when Iran listen ports are busy
- Replace manual `Tunnel side: iran/kharej` prompts with a numbered tunnel selector
- Include orphan service/env entries in tunnel discovery
- Update tests and documentation for the new UX

# v0.2 roadmap

The v0.2 work is tracked by focused issues and pull requests. Existing Direct Mode remains supported throughout development.

## Task #7 - v0.2 foundation and agent guidance
Status: In progress

Scope:
- Add repository-level `AGENTS.md`
- Define NGINX Gateway Mode architecture
- Define Native GOST Gateway as `Coming soon`
- Define monitoring metric semantics and retention
- Define state/secrets/generated-file boundaries
- Define small-PR delivery sequence

## Task #8 - Built-in monitoring core
Status: Planned

Scope:
- Python standard-library collector
- SQLite schema, WAL, retention, and rollups
- systemd collector service
- live dashboard
- 10m/30m/1h/custom historical summaries
- exact/derived/estimated/unavailable labels
- JSON/CSV export
- host, NGINX, GOST, service, port, socket, and route observations
- deterministic tests for rates, resets, gaps, and retention

## Task #9 - NGINX Gateway state and CRUD
Status: Planned

Scope:
- Versioned JSON desired state
- Create/edit/delete/list routes
- Create/edit/delete/list gateway tunnels
- Validate duplicate Host + Path
- Validate duplicate/conflicting internal ports
- Primary/backup and active-active membership model
- Keep credentials in local secret files
- Append NGINX and monitoring entries to the existing menu
- Add non-mutating Native GOST `Coming soon` entry

## Task #10 - Atomic NGINX renderer and runtime
Status: Planned

Scope:
- Install and detect NGINX safely
- Generate only managed NGINX files
- Loopback-only GOST listeners
- WebSocket proxy configuration
- Upstream groups and passive failover
- local-only basic status endpoint
- `nginx -t` before reload
- atomic replacement and rollback
- health/readiness observations
- invalid-config rollback tests

## Task #11 - Multiple Iran gateway firewall allowlist
Status: Planned

Scope:
- Replace single `IRAN_IP` model with `ALLOWED_GATEWAYS`
- Accept validated IPv4/CIDR lists
- Migrate legacy env files without breaking them
- Add/remove only rules owned by one managed tunnel
- Preserve operator firewall rules
- document persistence choices
- test duplicate, invalid, and rollback cases

## Task #12 - HA, synchronization, and route-aware monitoring
Status: Planned

Scope:
- Versioned non-secret export/import
- explicit synchronization workflow between Iran gateways
- last-valid-local-config runtime independence
- route/tunnel health states
- primary/backup degradation reporting
- failover events only when provable
- route and exit detail views
- capacity and bottleneck reports
- release documentation and migration guide

## Task #13 - Native GOST Gateway research
Status: Deferred after v0.2

Scope:
- Keep menu entry as `Coming soon` in v0.2
- Build a single-stage prototype only after NGINX Mode is stable
- Benchmark identical traffic against NGINX + GOST
- compare CPU, RSS, softirq, PPS, throughput, FDs, goroutines, handshake latency, and failure behavior
- make a later product decision from measured results
