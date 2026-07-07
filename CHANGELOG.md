# Changelog

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
- Added documentation, examples, tests, and GitHub Actions.
