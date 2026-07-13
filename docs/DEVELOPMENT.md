# Development

## Bash Coding Style

- Use `set -Eeuo pipefail` in every Bash script.
- Use arrays for dynamic command arguments.
- Keep validation functions small and testable.
- Avoid `eval`.
- Quote variable expansions unless intentional pattern matching is needed.
- Keep system paths centralized in `gost-manager.sh`.

## Run Tests

```bash
bash tests/run-tests.sh
python3 -m unittest discover -s tests -p 'test_gateway*.py'
python3 -m unittest discover -s tests -p 'test_monitoring*.py'
```

Compile every Gateway and monitoring module and test before running the suite:

```bash
python3 -m py_compile \
  gateway/*.py \
  monitoring/*.py \
  tests/test_gateway*.py \
  tests/test_monitoring*.py
```

The tests do not require root and do not modify:

```text
/etc/gost
/etc/systemd/system
iptables
/usr/local/bin
/usr/local/sbin
```

## Run Shellcheck

```bash
shellcheck -x -P SCRIPTDIR gost-manager.sh install.sh uninstall.sh \
  lib/gost-run-iran.sh lib/gost-run-kharej.sh lib/gost-run-gateway-exit.sh \
  packaging/gost-monitor packaging/gost-monitor-admin packaging/gost-monitor-collector \
  packaging/gost-gateway packaging/gost-gateway-runtime packaging/gost-gateway-nginx \
  tests/run-tests.sh tests/integration-test-lib.sh tests/test-install.sh \
  tests/test-menu.sh tests/test-uninstall.sh tests/test-systemd-linux.sh \
  tests/test-gateway-runner.sh tests/test-nginx-gateway-runner.sh \
  tests/test-nginx-integration.sh
```

The temporary-root Issue #6 suites are:

```bash
bash tests/test-install.sh
bash tests/test-menu.sh
bash tests/test-uninstall.sh
bash tests/test-systemd-linux.sh
bash tests/test-gateway-runner.sh
```

They use command stubs and never write to the host's real `/etc`, `/usr/local`, `/var/lib`, systemd, packages, NGINX, firewall, or GOST services.

Gateway desired-state tests use dedicated absolute temporary paths for the
shared document, node document, backup directory, and lock file. They cover
strict parsing, cross-document validation, CRUD, revision conflicts,
concurrent writers, failure injection, rollback, symlink rejection, bounded
backups, output canaries, Direct Mode isolation, and maximum cardinality. The
Gateway package must not call subprocesses or traffic-service commands.

For a manual state-only smoke test, create a dedicated temporary directory,
resolve its physical path, and pass every path override to
`python3 -m gateway.cli`. Do not use the production defaults during development.
The full CLI and schema contract is in `docs/GATEWAY-STATE-V0.2.md`.

Gateway runtime tests use explicit absolute temporary paths and injected
systemctl/`ss` runners. They cover strict secret input and storage, non-secret
rendering, exact listener ownership, first/unchanged/single-Exit apply,
service-state rollback, exact service control, Direct Mode isolation, and a
256-Exit allocation/time bound. The runtime contract and a manual temporary-root
workflow are in `docs/GATEWAY-RUNTIME-V0.2.md`. Never use production defaults or
start a real Gateway Exit service on a development host.

Dedicated NGINX Gateway tests cover strict Path interpolation, deterministic
config/manifest rendering, cgroup-wide listener ownership, backend readiness,
port conflicts, first activation, metadata-only updates, no-op behavior,
graceful reload, rollback, dependency installation, package isolation, and a
256-Route scale bound. `tests/test-nginx-integration.sh` skips outside Linux;
on Ubuntu with `/usr/sbin/nginx` it verifies exact Host/Path WebSocket routing,
active-active, active-passive failover, master-PID continuity, held connections,
and 1,000 simultaneous upgraded connections without using `/etc/nginx`.

`tests/test-systemd-linux.sh` skips clearly on non-Linux hosts. On Linux it requires the real `systemd-analyze`, verifies temporary Monitoring and generated Gateway Exit units against complete fixture paths, rejects malformed controls, audits traffic isolation, and runs the temporary-root installer with real unit verification. The Ubuntu 22.04/24.04 matrix is defined in `.github/workflows/monitoring-integration.yml`.

Installer rollback failures retain the printed backup paths. Follow the emitted commands in order: stop only `gost-monitor-collector.service`, remove each candidate destination, copy each retained `original` back with `cp -a`, run `systemctl daemon-reload`, restore the recorded enable/disable and start/stop state, then confirm `systemctl status gost-monitor-collector.service --no-pager`. No recovery command targets traffic.

Or:

```bash
make lint
```

## Release

1. Run `make check`.
2. Update `CHANGELOG.md`.
3. Tag the repository with the release version.
4. Push the tag and repository to GitHub.
5. Confirm local validation passes on the release branch.
