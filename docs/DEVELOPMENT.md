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
python3 -m unittest discover -s tests -p 'test_monitoring*.py'
```

Compile every monitoring module and test before running the suite:

```bash
python3 -m py_compile monitoring/*.py tests/test_monitoring*.py
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
  lib/gost-run-iran.sh lib/gost-run-kharej.sh \
  packaging/gost-monitor packaging/gost-monitor-admin packaging/gost-monitor-collector \
  tests/run-tests.sh tests/integration-test-lib.sh tests/test-install.sh \
  tests/test-menu.sh tests/test-uninstall.sh tests/test-systemd-linux.sh
```

The temporary-root Issue #6 suites are:

```bash
bash tests/test-install.sh
bash tests/test-menu.sh
bash tests/test-uninstall.sh
bash tests/test-systemd-linux.sh
```

They use command stubs and never write to the host's real `/etc`, `/usr/local`, `/var/lib`, systemd, packages, NGINX, firewall, or GOST services.

`tests/test-systemd-linux.sh` skips clearly on non-Linux hosts. On Linux it requires the real `systemd-analyze`, verifies a temporary unit against the complete host unit environment, checks every temporary executable/config path, audits production traffic isolation, and runs the temporary-root installer with real unit verification. The Ubuntu 22.04/24.04 matrix is defined in `.github/workflows/monitoring-integration.yml`.

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
