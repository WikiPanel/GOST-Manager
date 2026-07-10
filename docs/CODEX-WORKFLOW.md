# Codex delivery workflow

## Roles

### Product owner

The product owner approves behavior, prioritization, operator UX, and production acceptance. Production credentials and server access remain under the operator's control.

### Technical lead / project manager

The technical lead:

- turns product goals into architecture and acceptance criteria;
- maintains the issue and PR sequence;
- prevents incompatible parallel changes;
- reviews security, backward compatibility, monitoring correctness, and rollback behavior;
- decides whether a PR is ready for server testing;
- records test results and release decisions.

### Codex implementation agent

Codex works on one focused issue/branch at a time, follows `AGENTS.md`, runs repository checks, and produces a reviewable diff. It must not use production secrets or merge its own PR without review.

### GitHub

GitHub issues are the backlog, branches isolate implementation, pull requests are the review and acceptance record, and `main` is always expected to remain releasable.

## Delivery loop

```text
Product decision
  ↓
Issue with scope + acceptance criteria + non-goals
  ↓
Dedicated branch
  ↓
Codex implementation task
  ↓
make check + focused tests
  ↓
Technical review and requested fixes
  ↓
Disposable/staging server test
  ↓
PR approval and merge
  ↓
Release note / migration note
```

No large multi-feature branch is used for v0.2. Schema and foundation changes land before code that depends on them.

## Branch and PR naming

Examples:

```text
feat/v0.2-foundation
feat/monitoring-core
feat/nginx-gateway-state
feat/nginx-gateway-runtime
feat/multi-gateway-firewall
feat/ha-monitoring-export
```

PR titles use a clear area prefix, for example:

```text
monitoring: add local collector and SQLite history
nginx: add atomic route generator and rollback
firewall: support multiple Iran gateway CIDRs
```

## Codex environment

The repository environment should include:

- Bash;
- Python 3;
- `shellcheck`;
- `make`;
- NGINX for offline `nginx -t` fixture validation;
- common Linux utilities used by tests.

Tests must not require a running systemd instance. Privileged commands are replaced with deterministic stubs in temporary directories.

Do not add production secrets to Codex environment variables. Test credentials must be obvious fixtures.

## Task prompt template

```text
Repository: WikiPanel/GOST-Manager
Base branch: main
Issue: #<number>

Read AGENTS.md and the relevant architecture documents first.

Goal:
<one focused result>

Required behavior:
- ...

Backward compatibility:
- Existing Direct Mode and legacy env files must continue to work.

Non-goals:
- ...

Tests required:
- ...

Validation:
- Run make check.
- Report every command and result.

Deliverable:
- Commit changes on a dedicated branch.
- Open a draft PR with behavior, risks, tests, manual test plan, and rollback plan.
```

## Parallel-work policy

Parallel Codex tasks are allowed only when their write sets do not overlap materially.

Safe examples:

- monitoring query formatting while a separate task writes documentation fixtures;
- Persian documentation while runtime code is unchanged;
- independent test fixture expansion.

Unsafe examples:

- two tasks editing `gost-manager.sh`;
- state schema and renderer implemented in parallel before the schema is accepted;
- installer and uninstaller changed by separate agents without one integration owner;
- firewall migration and delete/cleanup behavior changed independently.

When overlap is likely, work is sequential or assigned to one implementation branch.

## Review gates

A PR is not ready for production testing until all applicable gates pass:

1. `make check` passes.
2. Existing Direct Mode regression tests pass.
3. New behavior has unit/integration-style tests without root.
4. Managed-file boundaries are explicit.
5. No credentials are present in diff or logs.
6. Failure and rollback paths are tested.
7. Monitoring values have correct quality labels and units.
8. Documentation includes an operator test and rollback procedure.

## Server acceptance

Use a disposable or staging Iran/Kharej pair first. Record:

- OS and architecture;
- GOST and NGINX versions;
- exact configuration IDs without secrets;
- commands executed;
- connection and throughput test results;
- CPU, softirq, memory, FD, PPS, retransmit, and route metrics;
- failover/reconnect behavior;
- rollback result.

Production rollout starts with one gateway and a small traffic share, then expands only after metrics remain stable.

## Merge and release policy

- Codex may prepare code and reviews; a human/technical lead approves merges.
- Squash or merge strategy must preserve useful issue/PR history.
- User-visible behavior updates `CHANGELOG.md` and both relevant language documents.
- Releases are tagged only after local checks and staging acceptance.
