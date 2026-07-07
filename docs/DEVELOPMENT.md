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
shellcheck gost-manager.sh install.sh uninstall.sh lib/gost-run-iran.sh lib/gost-run-kharej.sh tests/run-tests.sh
```

Or:

```bash
make lint
```

## Release

1. Run `make check`.
2. Update `CHANGELOG.md`.
3. Tag the repository with the release version.
4. Push the tag and repository to GitHub.
5. Confirm GitHub Actions passes.
