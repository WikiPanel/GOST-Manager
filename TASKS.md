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
- Add GitHub Actions

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
