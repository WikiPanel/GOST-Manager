# GOST Manager v0.2 Monitoring

## Goals

The monitoring subsystem must answer four operational questions from inside the manager:

1. What is happening on this server right now?
2. What happened during the last 10 minutes, 30 minutes, 1 hour, or a custom interval?
3. Is the bottleneck the host, NGINX, a GOST service, a route, a port, or the network path?
4. Is a reported number exact, calculated from exact counters, estimated, or unavailable?

Monitoring must never become part of the traffic path and must never restart traffic services automatically in the first v0.2 implementation.

## Components

```text
gost-monitor-collector.service
      ↓ sample every 5 seconds by default
local kernel/systemd/NGINX/GOST observations
      ↓
/var/lib/gost-manager/metrics.sqlite3
      ↓
gost-manager Monitoring menu
      ├── Live dashboard
      ├── 10-minute summary
      ├── 30-minute summary
      ├── 1-hour summary
      ├── Custom window
      ├── Detailed service/route view
      └── JSON/CSV export
```

The collector and query CLI use Python 3 standard library only. Bash remains the menu and orchestration layer.

## Sampling and retention

Defaults:

- sample interval: 5 seconds;
- raw sample retention: 48 hours;
- one-minute rollup retention: 30 days;
- cleanup interval: 15 minutes;
- maximum tolerated missed-sample gap before coverage is marked incomplete: 2.5 sample intervals.

All values are configurable within safe bounds. The database must use WAL mode, a busy timeout, explicit transactions, indexes for time-window queries, and periodic checkpoint/retention cleanup.

Database growth must be bounded. Retention cleanup failure is reported but must not interrupt traffic services.

## Metric quality labels

Every displayed metric has one of these labels:

- `exact`: directly observed at one sample point from the kernel, systemd, NGINX status, or an authoritative service counter;
- `derived`: calculated from exact counters, such as bytes/second, percentage, average, peak, or p95;
- `estimated`: an attribution or approximation that cannot be proven exactly with the available source;
- `unavailable`: the host or service cannot expose the metric safely.

The UI must not hide these labels in detailed views. Estimated metrics must not be used as the only input for automatic health decisions.

## Historical summary contract

For each selected interval, the query layer reports where meaningful:

- latest;
- minimum;
- average weighted by elapsed time when appropriate;
- maximum/peak;
- p95;
- first and last sample timestamps;
- sample count;
- expected sample count;
- coverage percentage;
- counter-reset count;
- missing-gap count.

Averages with poor coverage are visibly marked incomplete.

## Host metrics

### CPU and scheduler

Sources: `/proc/stat`, `/proc/loadavg`, and monotonic elapsed time.

- total CPU utilization;
- user;
- system;
- softirq;
- irq;
- iowait;
- steal;
- idle;
- load average 1/5/15;
- logical CPU count.

CPU percentages are derived from deltas between cumulative kernel counters. A single raw `/proc/stat` snapshot is never presented as a percentage.

### Memory

Source: `/proc/meminfo`.

- total;
- available;
- used using `MemTotal - MemAvailable`;
- cache/buffers detail;
- swap total and used;
- dirty/writeback memory.

### Disk and filesystem

Sources: `statvfs`, `/proc/diskstats` when available.

- free/used space for `/`, `/etc/gost-manager`, `/var/lib/gost-manager`, and log/storage filesystems;
- inode use;
- disk read/write rate;
- I/O time/utilization where the kernel exposes a reliable counter;
- monitoring database size.

### Network interfaces

Sources: `/proc/net/dev` and `/sys/class/net`.

Per interface and aggregate non-loopback totals:

- receive/transmit bytes;
- receive/transmit packets;
- receive/transmit errors;
- receive/transmit drops;
- derived bytes/second and packets/second;
- link state, MTU, and speed when available.

Loopback is shown separately because NGINX Gateway Mode intentionally creates local traffic between NGINX and GOST. It must not be added to external throughput totals.

### TCP/IP stack

Sources: `/proc/net/snmp`, `/proc/net/netstat`, and `ss` snapshots at a lower configurable frequency where needed.

- established connections;
- SYN-SENT, SYN-RECV, FIN-WAIT, CLOSE-WAIT, TIME-WAIT, and orphan counts where available;
- active/passive opens;
- failed connection attempts;
- resets;
- retransmitted segments and derived retransmit rate;
- listen overflows/drops where exposed;
- socket memory summary when safely available.

### Conntrack

Sources: `/proc/sys/net/netfilter/nf_conntrack_count` and `nf_conntrack_max` when present.

- current count;
- maximum;
- utilization percentage;
- unavailable state on hosts without conntrack.

### File handles

Sources: `/proc/sys/fs/file-nr` and `/proc/sys/fs/file-max`.

- allocated system file handles;
- maximum;
- utilization percentage.

## Service metrics

Services include NGINX and every managed `gost-*` unit.

Sources: systemd properties, cgroup files, `/proc/<pid>`, and local status endpoints.

- active/sub state;
- main PID and start time as systemd identity metadata;
- authoritative service process count from `cgroup.procs`;
- restart count;
- CPU time and derived CPU percentage aggregated across the cgroup PID set;
- aggregate RSS, anonymous memory, file cache where available;
- aggregate task/thread count;
- aggregate open file-descriptor count and limits;
- `established_sockets_total` across every authoritative service PID, without claiming that every socket is a remote tunnel leg;
- cgroup memory/current and peak where available;
- listener ownership;
- recent unit failures;
- service network accounting when the host supports reliable cgroup/systemd IP accounting.

`/proc/<pid>/io` is filesystem/process I/O and must never be labeled as network traffic.

If systemd IP accounting is enabled for a unit, the values are displayed as exact unit ingress/egress IP-accounting counters. They are not automatically labeled as unique user payload because loopback and external legs may both contribute.

## NGINX metrics

A generated loopback-only status endpoint may expose NGINX basic status.

- active client connections;
- accepted and handled connections;
- total requests;
- reading/writing/waiting states;
- NGINX process CPU, memory, tasks, and FDs;
- public listener health;
- configuration test result timestamp;
- last successful reload timestamp;
- reload/rollback failures recorded by the manager.

NGINX basic status is aggregate, not per route.

## Route and tunnel metrics

### Exact current route sessions

In NGINX Gateway Mode, each WebSocket routed to a GOST backend creates an established loopback connection to that route's unique internal port. The collector may count established sockets for each managed internal port.

The UI labels this as:

```text
current loopback upstream connections (exact snapshot)
```

It is a strong representation of current route sessions, but reconnect races and handshake-in-progress states are reported separately where possible.

### Tunnel state

For every managed tunnel:

- associated route and exit;
- primary/backup/active role;
- service state;
- internal listener state and owner;
- remote Kharej endpoint;
- established remote socket count;
- connection states;
- process CPU/RSS/tasks/FDs;
- unit IP-accounting counters when available;
- restart count and recent errors.

`established_remote_sockets` is exact only when the configured Kharej endpoint is a numeric IP and port, the full socket snapshot is authoritative, and socket ownership can be correlated to the service cgroup PID set. Hostname endpoints or missing PID attribution are reported as unavailable. Cached values between full snapshots are identity-bound and labelled estimated.

### Bytes and throughput attribution

The collector must prefer authoritative per-service counters. When only host-wide interface counters are available, it must not invent per-route byte totals.

Allowed labels include:

- `exact unit IP accounting`;
- `exact host interface total`;
- `derived rate from exact counter`;
- `estimated route payload` only if a documented estimator is explicitly enabled;
- `unavailable per route`.

### Failures and failover

Exact counters:

- systemd restart count;
- manager NGINX validation/reload/rollback failures;
- connection error counters exposed by authoritative sources;
- tunnel health transitions recorded by the collector.

A route failover counter is incremented only when the manager/health subsystem can prove that a new handshake used a backup after a primary failure. Log-text guesses must be labeled estimated or omitted.

## Health states

### Node

- `healthy`: traffic services are active, required listeners exist, resource thresholds are not critical, and sampling is current;
- `degraded`: at least one route/tunnel is unhealthy or a resource threshold is exceeded;
- `unknown`: observations are stale or required sources are unavailable;
- `critical`: public gateway service/listener is down or the host is near an operator-defined hard limit.

### Route

- `healthy`: NGINX route is present and at least one associated tunnel is ready;
- `degraded`: a backup is serving or one member is unavailable;
- `down`: no associated tunnel is ready;
- `disabled`: desired state disables the route;
- `unknown`: data is stale.

Initial v0.2 health is observational. It does not rewrite NGINX membership automatically.

## Live dashboard

The live view refreshes in place and includes a compact summary:

```text
HOST       CPU  RAM  LOAD  NET RX/TX  PPS  RETRANS  CONNTRACK  FDs
NGINX      STATE  CPU  RSS  ACTIVE  WRITING  FDs  PUBLIC PORT
GOST       SERVICE  STATE  CPU  RSS  CONNS  FDs  RESTARTS
ROUTES     ROUTE  HEALTH  CURRENT  PRIMARY/BACKUP  ERRORS
```

Keys or menu actions open detailed host, NGINX, service, route, socket, or database views. Non-interactive terminals receive a plain snapshot instead of ANSI refresh control.

## Historical views

Preset windows:

- 10 minutes;
- 30 minutes;
- 1 hour.

Custom input accepts safe duration forms such as `90s`, `15m`, `2h`, or explicit start/end timestamps within retention.

Views include:

- host resource summary;
- network and PPS summary;
- TCP/retransmit summary;
- NGINX summary;
- per-service summary;
- per-route summary;
- event timeline for restarts, health transitions, config changes, and sampling gaps.

## Events and audit trail

A separate event table stores bounded, structured events:

- collector start/stop;
- process restart detection;
- service state transition;
- listener disappearance/return;
- route health transition;
- NGINX validation/reload/rollback result;
- state import/export/apply;
- database retention/checkpoint failure;
- metric source becoming unavailable/available.

Events contain identifiers and safe diagnostics, never credentials.

## Export

Exports support JSON and CSV for a selected time window and entity scope. Every export includes:

- schema version;
- node ID;
- UTC time range;
- sample interval;
- metric quality labels;
- coverage metadata;
- units.

Secret state is never included in monitoring export.

## Performance guardrails

- Collector defaults to 5-second sampling, not per-second polling.
- Expensive commands such as full socket enumeration run at a lower cadence or only for managed ports.
- Use `/proc`, cgroup, and local status files before spawning commands.
- Use prepared SQLite statements and batch one sample in one transaction.
- Bound query result size and export windows.
- Collector CPU, RSS, database write latency, sample duration, and missed deadlines are themselves monitored.
- A collector overrun skips or delays monitoring work; it never applies backpressure to traffic services.

### Representative storage budget

The planning profile is one NGINX unit with one master and two workers plus six managed GOST services. It assumes five-second fast samples, 30-second full socket snapshots, 60-second FD/limit/filesystem samples, 48-hour raw retention, and 30-day minute-rollup retention.

The current metric-family model, measured with deterministic fixtures for that exact service profile, records 522 points per fast cycle, 9 additional points per full socket cycle, and 52 additional points per slow cycle. The completed-minute rollup has approximately 583 metric series. The resulting retained row counts are:

- 9,120,960 metric points per day;
- 18,241,920 raw metric points over 48 hours;
- 25,185,600 minute-rollup rows over 30 days;
- 34,560 `sample_cycles` rows and 241,920 `metric_samples` rows over 48 hours;
- a planning allowance of 150,000 deduplicated event rows over 30 days and 2,048 entity rows.

The deterministic capacity estimate uses 128 bytes per raw metric row and 160 bytes per minute-rollup row before indexes. It reserves 128 bytes per sample-cycle row, 192 bytes per metric-sample row, and 512 bytes per event or entity row. Small schema, tunnel, and collector-state tables are covered by the entity allowance and the free-page factor. It then adds 50 percent of table bytes for SQLite primary-key and secondary indexes, B-tree fill variance, and reusable free pages, followed by 20 percent for WAL growth, checkpoints, and operational headroom.

Under those conservative assumptions the estimated occupancy is:

- 2.17 GiB for the raw `metric_points` table;
- 3.75 GiB for the `minute_rollups` table;
- 0.12 GiB for `sample_cycles`, `metric_samples`, events, and entities;
- 3.02 GiB for indexes and free-page overhead;
- 1.81 GiB for WAL and operational headroom;
- 10.89 GiB estimated total database footprint.

Operators should reserve at least 12 GiB for the monitoring database under this profile. A 5 GiB reservation is not sufficient once 30-day minute rollups are included. Hosts with more interfaces, disks, services, metric cardinality, or event volume need additional space; reducing raw/rollup retention or metric cardinality reduces the requirement. The 150,000-event allowance is a 30-day planning horizon, so unusually high long-term event volume must be budgeted separately.

Process CPU/stat and aggregate RSS/thread observations remain on the fast cadence. `/proc/<pid>/fd`, process limits, cgroup file memory, filesystem capacity, and database-size observations use the slow cadence. A service PID set comes from `cgroup.procs`; MainPID fallback totals are estimated rather than exact. Only a complete authoritative cgroup PID set plus complete fast process snapshots advances process-set transition state. A missing fast snapshot makes process metrics unavailable for that cycle without emitting `pid_replaced`, and non-authoritative MainPID fallback never overwrites the last authoritative identity. Identity-bound socket and slow-process caches are neither read nor replaced when the current identity cannot be confirmed. Inactive historical source-error keys are retained for at most 48 hours and capped at 64 keys, while the global error total remains cumulative.

The deterministic performance suite parses and attributes a synthetic 20,000-row socket snapshot within the five-second cycle budget and verifies that a synthetic 10,000-entry FD directory is enumerated once, not six times, across six five-second cycles.

## Acceptance tests

- Live view works with NGINX absent, GOST absent, and both present.
- Historical 10m/30m/1h summaries show correct averages and peaks from deterministic fixtures.
- Counter reset and process restart do not create negative rates or huge spikes.
- Missing samples reduce coverage and are visible.
- Interface add/remove and PID replacement are handled.
- SQLite database remains bounded after simulated retention.
- Concurrent collector/query/export operations do not corrupt the database.
- Monitoring service failure leaves NGINX and GOST untouched.
- No test requires root or modifies the real host.

## Issue #8 collector-core contract status

The accepted collector core uses `/var/lib/gost-manager/metrics.sqlite3` by default and samples every 5 seconds.  It uses `time.monotonic()` scheduling primitives, explicit SQLite sample transactions, WAL mode, busy timeout, foreign keys, 48-hour raw retention, 30-day one-minute rollup retention, and 15-minute maintenance cadence.

Legacy Direct Mode discovery is intentionally narrow.  Iran env files read listen/target ports only from validated `MAPPINGS`; Kharej env files read the listener only from validated `TUNNEL_PORT`.  The collector never scans arbitrary env values, so IP addresses, credentials, UUIDs, and tokens are not treated as ports.  Malformed env files produce structured `env_parse_error` events and do not stop the rest of the collection cycle.  Monitoring does not write to existing env files.

Metric samples store a unit and one of `exact`, `derived`, `estimated`, or `unavailable`.  Optional kernel sources that are missing are stored as NULL/unavailable instead of fake zeroes.  Loopback interface counters are recorded separately from external interface counters.  `/proc/<pid>/io` is not used as a network source.

## Issue #11 metric coverage status

The collector implementation is split into independently testable standard-library modules:

- `models` and `entities` for stable models and secret-safe Direct Mode discovery;
- `schema` for schema v4 migration, persistence, retention, rollups, and WAL maintenance;
- `proc_readers` and `network_readers` for host, process, disk, interface, and TCP/IP counters;
- `systemd_readers` and `socket_readers` for managed-service, cgroup, listener, and connection observations;
- `event_state` for persisted transition state and deduplicated events;
- `collector` and `scheduler` for fault-isolated collection and monotonic cadence.

CPU, network, TCP/IP, memory, swap, filesystem, diskstats, conntrack, file-handle, GOST, NGINX, process, cgroup, listener, tunnel, and collector-self metrics now use the quality labels defined above. Counter rates are calculated only from persisted counter deltas and monotonic elapsed time. Reset and gap samples are marked and never converted into negative rates or spikes.

Every filesystem, procfs, command, clock, process, and statvfs source used by the collector is injectable. A failed source or managed entity records unavailable metrics and a source-error counter while unrelated sources continue. Source, service, PID, listener, interface, cycle, maintenance, and checkpoint events are transition-aware, so an unchanged warning is not written every sample.

Socket commands and proc network tables are structurally validated: a successful empty `ss` snapshot is authoritative, while non-empty malformed output is unavailable. Full socket collection stores separate attempt and success timestamps, so a failed heavy snapshot is not retried on every fast cycle. Collector totals include checkpoint duration on maintenance cycles; `metrics_written`, `events_written`, and row-attempt counts remain estimated because checkpoint result persistence occurs after the main sample transaction.

Tunnel metadata may contain only the remote `host:port` endpoint. Env usernames and passwords are not copied into metrics, events, entity metadata, collector state, or test exports.
