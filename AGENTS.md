# GOST Manager agent instructions

These instructions apply to the whole repository. Read `README.md`, `README.fa.md`, `docs/DEVELOPMENT.md`, and the v0.2 architecture documents before changing runtime behavior.

## Product invariants

- Preserve the existing Direct Mode and all existing `/etc/gost/{iran,kharej}-*.env` installations.
- v0.2 adds NGINX Gateway Mode first.
- Native GOST Gateway must be visible as `Coming soon` only. It must not create, delete, or alter runtime configuration in v0.2.
- A controller or synchronization tool must never be required for runtime traffic. Every gateway must continue using its last valid local configuration.
- Never commit production IPs, UUIDs, passwords, tokens, private keys, or generated secrets.
- Do not change unrelated firewall rules, NGINX files, systemd units, or GOST services.

## Implementation rules

- Bash files use `set -Eeuo pipefail`.
- Quote expansions and use arrays for dynamic commands. Never use `eval`.
- Keep validation and rendering functions small and independently testable.
- Use Python 3 standard library for structured state, SQLite monitoring, and complex parsing. Do not add a production Python package dependency without an approved issue.
- Store desired state separately from generated files. Generated files are never the source of truth.
- Write configuration atomically: render to a temporary file, validate it, back up the current managed file, replace it atomically, and roll back on failure.
- Before any NGINX reload, run `nginx -t`. Use graceful reload for ordinary route changes.
- Bind internal GOST gateway listeners to `127.0.0.1` unless a feature explicitly requires a public listener.
- Use stable IDs for routes, tunnels, and exit nodes. User-facing names may change; IDs must not silently change.
- New systemd services must have bounded restart behavior, high file-descriptor limits where needed, and no dependency on network services outside the local host for startup success.
- Monitoring failures must never restart, block, or stop NGINX or GOST traffic services.

## Monitoring correctness

- Label every metric as one of: `exact`, `derived`, `estimated`, or `unavailable`.
- Do not present `/proc/<pid>/io` as network traffic.
- Rates must be calculated from monotonic counter deltas and elapsed monotonic time.
- Detect counter resets, process restarts, interface changes, and missing samples.
- Historical summaries must include sample count and coverage; averages without adequate coverage are misleading.
- Store timestamps as UTC Unix time. Use local time only when formatting output.
- The collector must use SQLite transactions, WAL mode, a busy timeout, retention cleanup, and bounded database growth.
- Avoid per-packet logging and expensive commands on every sample.

## Compatibility and safety tests

- `make check` must pass before a PR is ready.
- Unit tests must run without root and must not modify `/etc`, systemd, NGINX, iptables/nftables, or `/usr/local`.
- Use temporary directories and command stubs for tests that cover filesystem writes or privileged commands.
- Add regression tests for legacy env files whenever state loading, migration, delete, cleanup, or service discovery changes.
- Add rollback tests for invalid generated NGINX configuration.
- Add tests for duplicate route `host + path`, duplicate internal ports, unsafe paths, and conflicts with unmanaged listeners.

## Pull-request discipline

- Keep PRs small and focused on one delivery milestone.
- Include: behavior summary, compatibility impact, security impact, test evidence, manual test plan, and rollback plan.
- Update user documentation and `CHANGELOG.md` for user-visible behavior.
- Do not merge a foundation or schema change together with large runtime behavior unless the issue explicitly requires it.

## Review guidelines

Treat the following as release-blocking:

- deletion or overwrite outside managed paths;
- firewall lockout or rules broader than requested;
- secret exposure in logs, generated files, tests, or Git history;
- broken upgrade paths for existing Direct Mode installations;
- NGINX reload without successful validation and rollback;
- inaccurate monitoring labels or calculations that may lead to false capacity conclusions;
- unbounded database, log, process, file-descriptor, or memory growth;
- a new runtime single point of failure introduced by synchronization or monitoring.
