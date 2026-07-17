# Upstream Watchdog v1

## Purpose and boundary

The Upstream Watchdog prevents an unreachable Kharej host from causing a
long-running connection/socket storm on its matching Iran profile. One central
Python daemon discovers exact managed `iran-N` profiles, checks each unique
`KHAREJ_IP` concurrently, and can control only the matching
`gost-iran-N.service`.

It is separate from Monitoring Lite and outside GOST's traffic process. It
does not modify tunnel env files, traffic units, firewall rules, runners, or
GOST command lines. Installation enables the central daemon but leaves every
profile Disabled, so install and update perform no GOST traffic service action.

## Exact defaults

```text
CHECK_MODE=ping
CHECK_INTERVAL_SECONDS=2
PING_TIMEOUT_SECONDS=1
FAILURE_THRESHOLD=10
SUCCESS_THRESHOLD=10
RECOVERY_HOLD_SECONDS=10
RECOVERY_JITTER_MAX_SECONDS=10
```

These defaults detect an outage after approximately 20 seconds of consecutive
failures. Recovery requires approximately 20 seconds of consecutive successes,
then a 10-second hold and a random 0-10-second jitter. Checks use a monotonic,
non-overlapping scheduler. Probe keys are `(KHAREJ_IP, PING_TIMEOUT_SECONDS)`:
profiles sharing both values reuse one result, while profiles sharing an IP
with different timeout overrides run independent checks. At most 32 checks run
concurrently.

Probe results are typed as `success`, `unreachable`, or `probe_error`. Ping
return code `1` is a normal unreachable result. A missing or non-executable
Ping, permission failure, local execution timeout, unsupported invocation, or
other unexpected local return code is `probe_error`. Probe errors do not change
success/failure counters and can never stop or start traffic. Persistent errors
produce one safe event per error transition and one recovery event, without
command lines, paths, stderr, or credentials.

## Modes and state machine

- `disabled`: no Ping, state event, or service action.
- `monitor`: Ping, state tracking, and transition history, with no service action.
- `auto`: the same checks plus the validated stop/recovery actions.

States are `unknown`, `healthy`, `degraded`, `down`, `recovering`, and
`maintenance`. A success resets failures; a failure resets recovery progress.
Auto Protect stops an active profile once at the failure threshold. It first
persists a durable action intent, queries the exact service immediately before
the action, verifies the result, and then finalizes ownership. A restart
reconciles unfinished stop and start intents for every valid profile before Ping
scheduling, including Monitor and Disabled profiles. Reconciliation either
observes that the exact intended state already exists or executes the persisted
action once, verifies the result, finalizes matching ownership, and clears the
intent. A failed resumed action records one safe bounded event, enters manual
override, clears the intent, and is not retried each cycle. Disabled profiles
never run Ping; only a previously authorized durable action may be completed.
If the final ownership write fails after a stop, Watchdog attempts one safe
compensating start; a failed compensation leaves the durable intent available
for deterministic restart reconciliation. No Kharej or arbitrary unit can be
targeted.

A Watchdog-owned stop continues to receive checks. It is started once only
after the success threshold, full hold, bounded jitter, and a final healthy
check, provided mode remains `auto`, maintenance is off, and no manual override
exists. A failed start or stop does not enter a retry loop.

## Manual actions and maintenance

Service-state mismatches are treated as operator actions. The daemon records a
safe `watchdog_manual_override` transition and suspends automatic actions until
the operator explicitly uses `Re-arm manual override`. It never auto-starts an
operator-stopped profile. Manual start/stop reconciliation runs on a separate
10-second cadence rather than spawning one `systemctl is-active` process per
profile on every two-second Ping cycle. Every actual start/stop still uses a
fresh exact-unit query.

Leaving Auto mode while a service is Watchdog-owned and stopped requires an
explicit choice: keep it stopped, start it now only if upstream is healthy, or
cancel. The ordinary mode-change path never silently changes service state.

Per-profile maintenance supports:

1. Enter and keep current service state.
2. Enter and stop the service now.
3. Exit without starting.
4. Exit and start only when upstream is healthy and the stop was maintenance-owned.

Checks may continue during maintenance, but automatic stop/start actions are
suspended. Maintenance ownership and Watchdog ownership are persisted
separately.

## Configuration and state

```text
/etc/gost-manager/watchdog.conf
/etc/gost-manager/watchdog.d/iran-N.conf
/var/lib/gost-manager/watchdog/watchdog.sqlite3
/usr/local/lib/gost-manager/gost_watchdog/
/usr/local/sbin/gost-upstream-watchdog
/usr/local/sbin/gost-watchdog-admin
/etc/systemd/system/gost-upstream-watchdog.service
```

Profile files may override mode, interval, timeout, thresholds, hold, and
jitter; omitted values inherit the global config. Parsing uses a strict
allowlist and never sources env files. Writes are private, atomic, fsynced, and
reject symlinks. Invalid config fails closed without changing traffic. The
reset/recovery command validates only the exact managed Iran env/unit identity,
ignores the broken profile config, and atomically replaces only that profile
file with `MODE=disabled`. This safely recovers malformed content, unsafe
permissions, symlinked profile files, and overrides made incompatible by a
later global timing change.

SQLite stores persistent state and transition/action events only. WAL, busy
timeout, fixed safe columns, indexed bounded queries, and batched pruning are
used. Events older than exactly 24 hours are removed. Ping attempts are not
written as events, and credentials are neither loaded nor stored.

## Operator workflow

Choose `12) Upstream Watchdog` to inspect status, set a profile to Monitor Only
or Auto Protect, edit overrides, test Ping, manage maintenance, inspect bounded
history and outage summaries, configure global defaults, inspect/restart only
the central service, or re-arm a manual override.

The status view includes the effective settings, service state, health state,
maintenance/manual-override ownership, counters, timestamps, and current outage
duration. Menu prompts use those machine-readable current effective values;
pressing Enter preserves them and changing one field writes only that override.
Detailed history is newest first and bounded. The 24-hour summary intersects
each completed or ongoing outage with the exact 24-hour window, so total and
longest downtime can never exceed 86,400 seconds.

`ping` is an installer runtime dependency mapped to Ubuntu's `iputils-ping`.
Before staging or replacing any file, installation runs the supported argv form
against `127.0.0.1`; missing authorization, missing Ping, insufficient
permission, or an incompatible implementation fails without mutation. This
local capability check never enables Auto Protect and never depends on a public
host.

## ICMP limitation

A successful Ping proves host reachability, not that the remote SOCKS5 service itself is healthy.

The v1 checker interface and state machine are isolated from the Ping executor
so a future issue can add a validated TCP or SOCKS check without weakening unit
validation, ownership, scheduling, or secret-handling rules.

## Controlled rollout

1. Update/install Watchdog runtime.
2. Confirm all profiles are Disabled.
3. Enable one approved profile in Monitor Only.
4. Observe for at least 24 hours.
5. Switch that profile to Auto Protect.
6. Perform a controlled Kharej shutdown.
7. Confirm the profile stops after about 20 seconds, Iran host sockets remain bounded, checks continue while stopped, recovery starts the profile after qualification, and no server reboot is required.
8. Roll out gradually to remaining profiles.

During the 24-hour one-profile Monitor Only observation, record the Watchdog
database and WAL sizes, disk write rate, daemon CPU and RSS, Ping subprocess
rate, and systemctl subprocess rate. With ten profiles at the two-second
default, the current per-profile state write can reach five SQLite transactions
per second. That write rate is not a correctness blocker for the one-profile
staging observation, but it must be measured before enabling all profiles.

## Rollback

Set affected profiles to `disabled` first. The component-aware uninstaller can
stop and remove only `gost-upstream-watchdog.service` and its runtime while
preserving operator configuration and history. Purging Watchdog data requires
a separate confirmation and the exact phrase `DELETE WATCHDOG DATA`. Uninstall
never starts or stops a GOST traffic profile.
