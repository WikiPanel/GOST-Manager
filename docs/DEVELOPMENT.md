# Development

## Product boundary

GOST Manager v2.0.1 is Direct Mode plus optional local Monitoring Lite and the
explicitly enabled Upstream Watchdog safety controller. NGINX Gateway and
Native GOST Gateway are cancelled. Do not add placeholders, hidden commands,
Gateway packages, NGINX discovery, or a second traffic runtime.

GOST itself remains the unchanged official upstream release artifact. This
repository contains no vendored GOST source, Go module, patch, or rebuilt
binary.

## Bash style

- Use set -Eeuo pipefail in every Bash script.
- Use arrays for dynamic command arguments.
- Keep validation functions small and independently testable.
- Avoid eval.
- Quote variable expansions unless pattern matching is intentional.
- Keep managed paths explicit.

## Python style

- Use Python 3 standard library only.
- Keep filesystem, procfs, command, process, and clock sources injectable.
- Label every metric exact, derived, estimated, or unavailable.
- Isolate source/entity failures so collection continues.
- Preserve schema version and retention unless an issue explicitly changes
  them.

## Local checks

Compile and run Python tests:

    python3 -m py_compile monitoring/*.py gost_watchdog/*.py tests/test_*.py
    python3 -m unittest discover -s tests

Run supported Bash suites:

    bash tests/run-tests.sh
    bash tests/test-install.sh
    bash tests/test-menu.sh
    bash tests/test-uninstall.sh
    bash tests/test-scope-reset.sh
    bash tests/test-systemd-linux.sh
    bash tests/test-profiles.sh
    bash tests/test-firewall-multi-source.sh
    bash tests/test-stability.sh
    bash tests/test-setup.sh
    bash tests/test-release-workflow.sh

Run the complete gate:

    make check
    git diff --check

ShellCheck covers the manager, installer, uninstaller, both Direct Mode
runners, Monitoring/Watchdog launchers, and supported Bash test suites. Local
ShellCheck instructions and make check support are intentionally retained.

## Test isolation

Unit and integration tests do not require root and do not modify the host's
real /etc, /usr/local, /var/lib, systemd, firewall, or GOST services.
Temporary-root suites use command stubs and fixture paths.

tests/test-systemd-linux.sh skips clearly on non-Linux hosts. On Linux it uses
the real systemd-analyze to validate the supported Monitoring and central
Watchdog units and runs
the temporary-root installer verification. The Ubuntu 22.04/24.04 matrix is
defined in .github/workflows/monitoring-integration.yml.

Installer rollback failures retain printed backup paths. Recovery commands
must target only the collector and candidate managed files; they must never
target Direct Mode traffic services.

## Compatibility checks

Changes to install, upgrade, uninstall, env discovery, or service discovery
must prove:

- existing Iran and Kharej env fixtures remain byte-identical;
- existing Direct Mode units retain bytes and mode;
- active Direct Mode service state remains unchanged;
- exact managed unit matching does not include unmanaged services;
- official upstream GOST install behavior remains unchanged;
- Monitoring does not discover nginx.service;
- current output contains no obsolete Gateway entities;
- historical generic rows remain governed by existing retention.
- strict profile parsing never executes env content or exposes credential
  canaries;
- configured and live local-port validation spans both Direct Mode sides;
- edit/clone/delete rollback changes only the selected profile;
- multi-source firewall ordering and rollback preserve unrelated rules;
- 50 Iran plus 50 Kharej discovery, inventory, list rendering, and monitoring
  discovery each remain under the documented three-second bound.
- Server Stability discovers only exact numbered GOST units, performs no
  service restart, preserves env/unit bytes, and is idempotent after its first
  successful run.
- Watchdog defaults to Disabled per profile, validates exact Iran unit names,
  checks unique IPs concurrently, persists stop ownership, bounds events to 24
  hours, and never controls a Kharej or arbitrary service.

## Release

1. Run `make check` and `git diff --check`.
2. Confirm Ubuntu 22.04 and Ubuntu 24.04 gates.
3. Update `VERSION`, `CHANGELOG.md`, both README languages, and
   `docs/releases/vX.Y.Z.md`.
4. Record behavior, compatibility, security, tests, manual plan, and rollback
   in the pull request.
5. Keep a pull request Draft until human review is complete.
6. Create the version tag only after approval. The tag workflow validates the
   exact tag, builds deterministic assets, verifies SHA256 locally, and then
   publishes. Manual workflow runs are validation-only unless their protected
   `publish` input is explicitly enabled.

## Real-server release validation

This plan is for a disposable or approved staging server. It is documented for
human execution and must not run automatically against production.

### Phase A: pre-merge branch validation

Phase A is the executable pre-merge blocker. On an approved Ubuntu 22.04 or
24.04 staging server, install from the exact pull-request branch:

```bash
git clone --depth 1 \
  --branch release/v2.0.0-setup \
  https://github.com/WikiPanel/GOST-Manager.git \
  /opt/GOST-Manager-v2-review
cd /opt/GOST-Manager-v2-review
bash install.sh --install-dependencies
```

Before running the installer on an existing staging server:

1. Capture every exact `gost-iran-*.service` and
   `gost-kharej-*.service` `MainPID` and `NRestarts` value.
2. Checksum `/etc/gost/*.env`, exact managed traffic units, their drop-ins,
   and `/etc/sysctl.d/99-gost-stability.conf`.
3. Record firewall state and the monitoring database inode, size, schema
   version, latest readable samples, and current manager version.
4. Record collector health and active-user continuity using an approved
   traffic-level observation without storing credentials.

After installation, compare every recorded value and verify the new manager
version. Expected results are zero traffic PID changes, zero restart-count
increases, zero env/unit/drop-in/firewall/stability-file changes, preserved
monitoring database and readable history, compatible schema, healthy
Monitoring Lite, and uninterrupted active users.

Phase A validates the local transactional installer and manager upgrade. It
does not validate the unpublished public Release URL. Do not manually replace
the production `/opt/GOST-Manager` source during review unless the operator
explicitly approves that action.

### Phase B: post-release public setup smoke

Run Phase B only after `setup.sh` is merged to `main`, the `v2.0.0` tag exists,
and both v2.0.0 release assets have been published. Test the public command on
one fresh supported staging server and one approved existing canary server:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/WikiPanel/GOST-Manager/main/setup.sh)
```

Repeat the same traffic-continuity, configuration-preservation, monitoring
history, collector-health, source-version, and installed-version checks. The
exact public latest-release path cannot be tested until its release assets
exist, so this post-release smoke is not a pre-merge blocker and requires no
RC tag or prerelease.
