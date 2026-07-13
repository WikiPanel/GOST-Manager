# Development

## Product boundary

v0.2 is Direct Mode plus optional local Monitoring Lite. NGINX Gateway and
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

Compile and run Monitoring tests:

    python3 -m py_compile monitoring/*.py tests/test_monitoring*.py
    python3 -m unittest discover -s tests -p 'test_monitoring*.py'

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

Run the complete gate:

    make check
    git diff --check

ShellCheck covers the manager, installer, uninstaller, both Direct Mode
runners, Monitoring launchers, and supported Bash test suites. Local
ShellCheck instructions and make check support are intentionally retained.

## Test isolation

Unit and integration tests do not require root and do not modify the host's
real /etc, /usr/local, /var/lib, systemd, firewall, or GOST services.
Temporary-root suites use command stubs and fixture paths.

tests/test-systemd-linux.sh skips clearly on non-Linux hosts. On Linux it uses
the real systemd-analyze to validate the supported Monitoring unit and runs
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

## Release

1. Run make check and git diff --check.
2. Confirm Ubuntu 22.04 and Ubuntu 24.04 gates.
3. Update CHANGELOG.md and user documentation.
4. Record behavior, compatibility, security, tests, manual plan, and rollback
   in the pull request.
5. Keep a pull request Draft until human review is complete.
