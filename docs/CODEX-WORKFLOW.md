# Codex delivery workflow

## Product boundary

Work in this repository targets official upstream GOST Direct Mode and local
Monitoring Lite. NGINX Gateway and Native GOST Gateway are cancelled.
Multi-server management means independent numbered Direct Mode profiles only;
it must not introduce a controller, shared process, failover, or connection
migration.

## Roles

The product owner approves behavior, operator UX, and production acceptance.
The technical lead maintains issue and PR scope, reviews security and backward
compatibility, and decides readiness. Codex implements one focused issue at a
time, follows AGENTS.md, runs repository checks, and never uses production
secrets or merges its own pull request.

## Delivery loop

    Product decision
      -> focused issue and acceptance criteria
      -> dedicated branch
      -> implementation and deterministic tests
      -> make check
      -> technical review
      -> disposable/staging Iran and Kharej test
      -> human approval and merge

## Environment

The development environment needs Bash, Python 3, ShellCheck, make, and common
Linux utilities used by the supported tests. Tests must not require root or a
running systemd instance; privileged commands use temporary paths and
deterministic stubs. Linux unit verification uses systemd-analyze when
available.

Production secrets must never enter environment variables, fixtures, logs,
metrics, events, or Git history.

## Review gates

1. make check passes.
2. Direct Mode regression tests pass.
3. Monitoring Python tests pass.
4. Ubuntu 22.04 and Ubuntu 24.04 gates pass.
5. Managed-file and rollback boundaries are explicit.
6. Existing Iran/Kharej env files, units, modes, and active states are
   preserved by upgrade.
7. Monitoring quality labels and units are accurate.
8. No vendored or modified GOST source/artifact exists.
9. Documentation includes operator validation and rollback.
10. Profile operations prove exact-service isolation, credential redaction,
    global/local live-port safety, and profile-scoped firewall rollback.

## Server acceptance

Use a disposable Iran/Kharej pair first. Record:

- OS and architecture;
- official GOST version;
- profile numbers without secret values;
- commands executed;
- connectivity results;
- CPU, memory, file descriptor, packet, retransmit, and connection metrics;
- rollback result.

Start with one profile and a small traffic share, then expand only after
metrics remain stable. Monitoring is observational and must never become a
runtime dependency.

## Merge and release policy

- A human or technical lead approves merges.
- main remains releasable.
- User-visible changes update CHANGELOG.md and both README languages.
- Releases are tagged only after local and Ubuntu matrix validation.
