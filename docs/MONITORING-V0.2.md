# Monitoring Lite v0.2

## Scope

Monitoring Lite is a local and optional operational view for Direct Mode. It
does not depend on NGINX, does not enter the traffic path, and never owns GOST
service lifecycle. NGINX Gateway and Native GOST Gateway are cancelled.

The subsystem answers four bounded questions:

1. Is the host resource-constrained?
2. Are external or loopback network counters abnormal?
3. Are current Direct Mode services and listeners healthy?
4. Is the collector itself current and reliable?

Multi-profile management does not expand Monitoring Lite into a controller.
Monitoring remains read-only and observes every valid numbered profile.

## Runtime

The installer copies only the Python standard-library modules listed in
packaging/monitoring-runtime-manifest.txt and installs:

    /usr/local/sbin/gost-monitor
    /usr/local/sbin/gost-monitor-admin
    /usr/local/sbin/gost-monitor-collector
    /etc/gost-manager/monitoring.env
    /etc/systemd/system/gost-monitor-collector.service
    /var/lib/gost-manager/metrics.sqlite3

The collector unit has bounded restart behavior, low CPU and I/O priority,
private runtime/state directories, and one writable state path. It has no
Requires, PartOf, BindsTo, stop, restart, or reload relationship with Direct
Mode services.

The daemon, one-shot collection, and history purge share the private advisory
lock /run/gost-manager/collector.lock. Contention returns exit code 4.

## Configuration

The installed root-owned 0600 configuration accepts only:

    GOST_MONITOR_DB=/var/lib/gost-manager/metrics.sqlite3
    GOST_ENV_DIR=/etc/gost
    GOST_MONITOR_SAMPLE_INTERVAL=20
    GOST_MONITOR_TCP_INTERVAL=60
    GOST_MONITOR_SLOW_INTERVAL=120
    GOST_MONITOR_MAINTENANCE_INTERVAL=1800
    GOST_MONITOR_LOAD_SHEDDING=1

The parser rejects unknown or duplicate keys, unsafe quoting or substitution,
relative paths, invalid cadences, and installed paths outside the managed
roots. The file is parsed as data and is never sourced by a shell.
Existing config files without the load-shedding key remain valid, default to
enabled, and are not rewritten during upgrade.

`gost-monitor-admin apply-preset high-traffic` applies exactly these intervals.
It displays current and updated values, creates a same-directory backup,
validates, writes atomically with file and directory fsync, and rejects
symlinks, untrusted ownership, or permissions broader than `0600`. Reapplying
the same values is a no-op. Only the explicit `--restart-collector` flag may
restart `gost-monitor-collector.service`; no GOST service command is issued.

## Metric quality

Every metric uses one semantic quality:

- exact: directly observed from an authoritative local source;
- derived: calculated from exact counters and elapsed monotonic time;
- estimated: useful but not authoritative;
- unavailable: the source could not provide a valid observation.

Rates use monotonic counter deltas and monotonic elapsed time. Counter resets,
gaps, process replacement, and interface churn cannot become negative rates or
spikes. /proc/<pid>/io is not treated as network traffic.

One failed source or entity does not stop collection of unrelated metrics.
Malformed non-empty proc or socket output is unavailable, while a valid empty
socket snapshot is exact. Failure and recovery events are transition-aware and
deduplicated.

## Retained sections

### Host

- CPU and derived percentages;
- load 1/5/15;
- RAM, swap, dirty, and writeback;
- root and monitoring filesystems;
- filesystem bytes/inodes and diskstats;
- system file-handle capacity.

### Network

- per-interface RX/TX bytes and packets per second;
- errors and drops;
- interface state, speed, and MTU where available;
- external totals;
- loopback totals kept separate.

### TCP and connections

- ESTABLISHED;
- SYN-SENT;
- SYN-RECV;
- CLOSE-WAIT;
- TIME-WAIT;
- retransmits;
- listen drops and overflows where available;
- conntrack use and capacity where available.

Conntrack is an optional host capability with three explicit states:

- `available`: both sysctl files are readable and valid, so count, maximum,
  and derived utilization are collected;
- `unsupported`: both sysctl files are absent, so numeric metrics are
  unavailable without an active source error;
- `failed`: only one file exists, a read is denied, or a value cannot be
  parsed.

Unsupported cycles do not increment `source_errors_total` and are not included
in `source_error_codes`. The historical cumulative total is preserved. State
changes emit at most one informational `optional_source_unsupported` or
`optional_source_available` event, and repeated cycles in one state emit
nothing. These informational events do not degrade health. The capability is
checked every cycle, so files may appear or disappear without restarting the
collector. Monitoring never runs `modprobe` or changes the host kernel.

### GOST services

All current exact gost-iran-<number>.service and
gost-kharej-<number>.service units are discovered. Metrics include:

- active state and main PID;
- aggregate CPU, RSS, process count, threads, and FDs;
- listener count and established sockets;
- restart count;
- quality and observation age.

Authoritative cgroup PID sets aggregate multi-process units. Incomplete process
snapshots become unavailable for that cycle and do not create false
process-replacement events. Slow FD/limit/filesystem work uses the slow cadence
instead of every fast sample.

### Tunnels

Validated Iran and Kharej env files create current Direct Mode tunnel
entities. Metrics include:

- side and service;
- immutable profile number and optional safe profile label;
- configured ports;
- canonical allowed Iran sources and source count for Kharej profiles;
- observed listeners;
- current connection counts;
- quality and age.

The manager's profile list counts established sockets only when the socket
snapshot and exact GOST PID ownership are authoritative. Otherwise it prints
`unknown`; zero means an authoritative snapshot found no owned established
socket. Monitoring metrics retain their existing exact/estimated/unavailable
quality labels and endpoint-correlation semantics.

Only safe topology values are stored. Usernames, passwords, tokens, and other
credentials never enter metrics, events, entity metadata, collector state, or
exports.

Legacy `IRAN_IP` is interpreted as one canonical `/32` source. New
`ALLOWED_IRAN_SOURCES` values are canonicalized, deduplicated, and bounded.
Labels are display metadata; the stable tunnel entity ID remains `iran-N` or
`kharej-N`. Existing schema-version-4 history remains valid.

### Collector

- last successful sample and age;
- collection and checkpoint duration;
- missed deadlines;
- bounded active source errors;
- metrics/events written estimates;
- database size.
- overrun count, active load-shedding state, and heavy-work deferral.

Fast cycles collect basic CPU, memory, load, service state, and process
identity. Network/protocol counters and one shared full TCP snapshot run on the
TCP cadence. FD enumeration, socket ownership, cgroup/process detail,
disk/filesystem data, and other expensive work run on the Slow cadence. PID
changes invalidate identity-bound caches; unavailable due snapshots are not
presented as exact cached data.

When a cycle misses its deadline, `collection_overrun` and one
`collection_load_shedding_entered` transition are recorded. The next cycles
continue Fast metrics while TCP/Slow/maintenance work is deferred. Three
on-time cycles restore full collection and emit one recovery transition. No
unbounded queue is created, and the feature can be disabled with
`GOST_MONITOR_LOAD_SHEDDING=0`.

## Views and commands

The main Monitoring menu provides:

    1) Live resources
    2) Last 10 minutes
    3) Last 30 minutes
    4) Last 1 hour
    5) Services and tunnels
    6) Collector status
    7) Advanced tools
    0) Back

Normal output contains HOST, NETWORK, TCP/CONNECTIONS, GOST SERVICES, TUNNELS,
and COLLECTOR sections. It contains no NGINX, Gateway, Route, or Exit section.
Non-interactive terminals receive a plain snapshot; interactive live output
uses bounded ANSI refresh.

Direct commands include:

    gost-monitor snapshot
    gost-monitor live
    gost-monitor summary --window 10m
    gost-monitor summary --window 30m
    gost-monitor summary --window 1h
    gost-monitor-admin status
    gost-monitor-admin maintenance

The existing Advanced tools retain current detail, event, custom summary,
JSON/CSV export, diagnostic, maintenance, purge, and collector-control
workflows. Query safety limits, watermark validation, bounded streaming, and
JSON/CSV semantic parity remain unchanged.

## Storage and retention

SQLite schema version 4 remains unchanged. The database uses WAL mode, a busy
timeout, transactions, foreign keys, indexed bounded queries, and atomic
maintenance.

Default independent retention policies remain:

- raw metric points: 6 hours;
- minute rollups: 24 hours;
- structured events: 24 hours.

Existing rows are not erased by a migration. Obsolete historical generic rows
remain queryable where existing APIs already allow it and age out through
normal retention.

Retention deletes in bounded batches of 10,000 rows. Each maintenance run may
delete up to 12 batches per table, which is well above the documented insert
rate. If a run hits that limit, the remaining expired rows are drained one
batch per table on each following collection cycle until none are left. Load
shedding may postpone maintenance by at most one extra maintenance interval.
SQLite reuses the freed pages, so a database that grew before this fix stops
growing but does not shrink on disk by itself.

The representative Direct Mode profile models six GOST services at the new
cadences:

- 350 fast metric points every 20 seconds;
- 120 full-socket/network extra points every 60 seconds;
- 74 slow extra points every 120 seconds;
- approximately 544 rollup series per minute.

This produces 1,738,080 metric points per day, 434,520 raw rows over six hours,
and 783,360 minute-rollup rows over 24 hours. Conservative storage
components are:

| Component | Estimated bytes | GiB |
| --- | ---: | ---: |
| Raw metric table | 55,618,560 | 0.052 |
| Minute-rollup table | 125,337,600 | 0.117 |
| Cycles, samples, events, entities | 5,198,336 | 0.005 |
| Indexes and reusable pages | 93,077,248 | 0.087 |
| WAL and operational headroom | 55,846,349 | 0.052 |
| **Estimated total** | **335,078,093** | **0.312** |

Reserve at least 1 GiB for the representative profile. Higher service
cardinality, longer retention, or custom cadence requires a recalculated
budget.

## Performance fixture

The deterministic 1,000-user fixture models Direct Mode only: 1,000 inbound
GOST sockets, 1,000 correlated remote GOST sockets, 20 unrelated TCP rows, and
six listeners, for 2,026 socket rows. It includes six GOST services, six
tunnels, three interfaces, and representative FD counts.

The fixture measures parsing, socket attribution, complete heavy collection,
database writes, command-call cadence, and projected storage. A separate
synthetic process exposes 10,000 FDs and proves Fast cycles do not enumerate
them. Safety limits remain five seconds for the heavy cycle, 256 MiB RSS where
portable, zero deterministic missed deadlines, and exactly one shared `ss`
snapshot per due TCP cycle. It proves bounded monitoring overhead, not network
capacity.

## Compatibility and failure behavior

- Current snapshots load only current Direct Mode service/tunnel membership.
- Historical generic rows are not promoted into current entities.
- Live, 10-minute, 30-minute, and 1-hour views use the existing query engine.
- Sample count, coverage, age, unavailable/reset/gap counts, and source mode
  remain visible.
- Collector failure, corrupt history, failed maintenance, or removal cannot
  stop Direct Mode traffic.
- Purge requires explicit confirmation, checkpoints WAL, uses same-directory
  recovery anchors, and restores the original database after injected failure.
- Tests use temporary directories and command stubs; they never modify the
  host's real /etc, systemd, firewall, GOST services, or /usr/local.

### VMware conntrack regression check

On a host where both conntrack sysctl files are absent, record the cumulative
counter before upgrade and compare it after several collection intervals:

```bash
BEFORE=$(gost-monitor collector --window 10m \
  | awk '/source_errors_total/ {print $0}' | tail -1)
sleep 30
AFTER=$(gost-monitor collector --window 10m \
  | awk '/source_errors_total/ {print $0}' | tail -1)
printf 'before: %s\nafter:  %s\n' "$BEFORE" "$AFTER"

gost-monitor snapshot | sed -n '1,35p'
gost-monitor export --window 10m --format csv --granularity raw \
  --entity-type collector_source --output -
```

Before this fix, `source_error_codes` could remain 1 and
`source_errors_total` could increase every 10 seconds. After upgrade, expect
`source_error_codes = 0`, an unchanged historical `source_errors_total`,
`conntrack: unsupported`, and `status: healthy` with
`reasons: observations_current` when all required observations are current.
The cumulative total is historical and is not expected to reset to zero.
