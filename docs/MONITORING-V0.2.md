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
python3 -m monitoring.query_cli
      ├── plain snapshot / ANSI live dashboard
      ├── cadence-aware historical summaries
      ├── host/network/service/tunnel/collector details
      ├── structured event timeline
      └── bounded JSON/CSV export
```

The collector, query CLI, strict config parser, and admin CLI use Python 3 standard library only. Issue #6 installs them as an independent local subsystem; monitoring is never a traffic runtime dependency.

### Production integration

The complete package is installed under `/usr/local/lib/gost-manager/monitoring`. Fixed launchers `/usr/local/sbin/gost-monitor`, `/usr/local/sbin/gost-monitor-collector`, and `/usr/local/sbin/gost-monitor-admin` set `PYTHONPATH=/usr/local/lib/gost-manager` and preserve arguments without `eval`. No module is copied to system site-packages and pip is not used.

`packaging/monitoring-runtime-manifest.txt` is the exact runtime source allow-list. The installer rejects missing, duplicate, absolute, traversal, non-regular, or symlinked entries and does not install unlisted Python files. Dependency mapping is explicit: `python3` to `python3`, `systemctl`/`systemd-analyze` to `systemd`, `ss` to `iproute2`, `cmp` to `diffutils`, and core file tools to `coreutils`; every missing command is rechecked after opt-in package installation.

`/etc/gost-manager/monitoring.env` is root-owned mode `0600` and is parsed only as strict `KEY=VALUE` data. The allow-list is `GOST_MONITOR_DB`, `GOST_ENV_DIR`, `GOST_MONITOR_SAMPLE_INTERVAL` (5..60), `GOST_MONITOR_TCP_INTERVAL` (10..300 and at least sample), `GOST_MONITOR_SLOW_INTERVAL` (30..900 and at least sample), and `GOST_MONITOR_MAINTENANCE_INTERVAL` (300..86400 and at least slow). Unknown/duplicate/empty keys, relative or non-normalized paths, shell substitutions, unsafe quoting/whitespace, NULs, and invalid integers/cross-field cadence combinations are rejected before database mutation. A valid existing config is preserved on upgrade; an invalid existing config aborts activation without replacement.

Generic parsing and installed-system policy are explicit layers. Generic library/test calls may use temporary absolute paths. Installed `GOST_MONITOR_DB` values must be file descendants of `/var/lib/gost-manager`; alternate filenames and nested directories are supported. Installed `GOST_ENV_DIR` must be `/etc/gost` or a descendant. `/srv`, `/root`, `/tmp`, prefix lookalikes, parent/final symlinks, and symlinks introduced before runtime open are rejected. Installer migration, collector/query launchers, status, maintenance, purge, menu output, and uninstall all resolve the same strict config value. `gost-monitor-admin config --format json` exposes only the database path, env directory, and validated cadence fields; `--format value --field database_path` is the Bash-safe single-value interface.

`gost-monitor-admin` provides `config`, `validate-config`, `migrate`, `status`, `maintenance`, and explicitly confirmed `purge-history`. Status is read-only. Maintenance commits rollup and all three retention policies before a separate WAL checkpoint. The daemon, one-shot collector, and purge share an exclusive private `fcntl.flock` at `/run/gost-manager/collector.lock`; contention returns exit code `4`, while a stale unheld lock file is reusable.

Purge checkpoints the authoritative WAL and refuses a busy checkpoint before replacement work. It fsyncs the old DB, creates and validates a private unique same-directory schema-v4 replacement, preserves mode/owner, and creates collision-resistant hard-link backup and recovery anchors without copying a multi-GiB database. One `os.replace` installs the new canonical inode. The parent directory is fsynced at durability boundaries; replacement validation and WAL/SHM cleanup complete before recovery anchors are discarded. Any injected pre-commit failure restores the original DB and sidecars atomically, validates schema/data, releases the lock, and cleans temporary artifacts. A concurrent read-only query may cause safe refusal but never exposes partial new history.

`gost-monitor-collector.service` uses `After=local-fs.target`, bounded `Restart=on-failure`, low CPU/I/O priority, `UMask=0077`, `StateDirectoryMode=0700`, `RuntimeDirectory=gost-manager`, `RuntimeDirectoryMode=0700`, `ProtectSystem=strict`, and a single write path `/var/lib/gost-manager`. It deliberately does not use traffic `Requires=`, `PartOf=`, `BindsTo=`, `PrivateNetwork`, hidden `/proc`, netlink-blocking address-family restrictions, or NGINX/GOST lifecycle hooks. Fresh install enables/starts it; upgrade preserves the prior collector enabled/active state. Rollback stops/disables a partial fresh activation while the unit exists, restores old files before restoring exact upgrade state, verifies final active/enabled/loaded state, and retains recovery backups plus exact commands if proof fails.

Static assertions protect production paths and hardening. Real Linux validation renders only a temporary verification copy whose `ExecStart`, `ExecStartPre`, and `EnvironmentFile` point to existing private fixture paths, then runs host `systemd-analyze verify /absolute/path/gost-monitor-collector.service`. It never uses an incomplete synthetic `--root`. Ubuntu 22.04/24.04 CI runs this test and a temporary-root install.

Main-menu options 1 through 9 retain their labels and dispatch. Option 10 opens all monitoring query/export/admin workflows; errors and live exit 130 return to the menu. An active collector is stopped only after confirmation for one-shot diagnostics and is restored after every result. Option 11 is a complete-root filesystem/command-audited `Native GOST Gateway (Coming soon)` print-only no-op. Uninstall confirmations separate monitoring service, code, config, and history from traffic units, credentials, runners, and the GOST binary. Actual service state is re-read after each removal: surviving traffic preserves both runners, credentials, and the GOST binary, while collector removal failure preserves every monitoring dependency. Existing shared directory and Direct Mode metadata are never rewritten; private managed metadata is restored on rollback.

## Sampling and retention

Defaults:

- sample interval: 5 seconds;
- raw sample retention: 48 hours;
- one-minute rollup retention: 30 days;
- structured event retention: 30 days;
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

The query health policy marks observations stale after 20 seconds. CPU at 80%, memory at 85%, and filesystem, conntrack, or system file handles at 85% produce `degraded` reasons. Filesystem, conntrack, and file handles at 95% produce `critical` only from exact or derived observations. An estimated value can degrade health but cannot independently make it critical. Missing required CPU data, unavailable listener ownership, or unavailable process snapshots produce `unknown`; an exact inactive service or exact missing tunnel listener can produce `down`. Each result includes stable reason codes, readable reasons, evaluation time, observation age, semantic quality, and affected entity. Health evaluation never starts, stops, or restarts a service and never changes NGINX, GOST, firewall, routes, or env files.

In Direct Mode, services referenced by active tunnel entity metadata are required. Current membership comes from the latest successful collector cycle: a tunnel is current only when that cycle contains an exact `env_source_valid=1` point, while a service is current when it was discovered and observed in that cycle. A later failed collector cycle does not replace the last successful membership. Removing an env file retires the tunnel and its no-longer-discovered GOST service from current snapshot and health after the next successful cycle, but their retained raw history remains queryable by exact tunnel/service detail commands. Per-tunnel metric failures do not retire a tunnel whose env discovery marker succeeded. Historical entity rows are excluded before applying the 256-current-entity bound.

Current health evaluates the newest transition for each stable service, tunnel, source, or collector incident key. A failure followed by its recovery remains visible in the historical event timeline but is no longer an active health incident; failure, recovery, then failure leaves exactly one active incident. Listener events affect current health only while their tunnel is in current membership, and events older than a recreated tunnel entity do not carry into the new lifecycle. A malformed managed env source records only its safe path identity, exposes `managed_env_invalid`, and remains degraded until `env_parse_recovered`; intentional file removal is retirement, not malformed configuration. The compact snapshot requests only `interface:external-total` and `interface:lo`, so retired transient interfaces cannot exhaust its entity bound, while exact historical interface queries remain available inside retention.

`nginx.service` remains visible but optional until trusted local gateway metadata explicitly sets `gateway_required=true`; absent or inactive optional NGINX does not change overall node health. An exact active required service with zero owned listeners is `down` with reason `required_listener_missing`. Unavailable or stale ownership is `unknown`, never a false `down`. Health-relevant events use a separate time-indexed, 200-row bounded query, so a source/restart/listener/checkpoint failure cannot be hidden by the 50-row display timeline. `service_state_changed` affects health only for a warning/error transition whose `current` state is `inactive` or `failed`; an informational transition to `active` is recovery and does not degrade health. `pid_replaced` remains an explicit restart signal. If more than 200 relevant rows exist, snapshot remains available, returns the newest bounded set, and reports `health_event_overflow` conservatively. Optional-service and optional-source events do not otherwise degrade overall health.

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

### Query CLI

The installed `gost-monitor` launcher always loads the validated
`/etc/gost-manager/monitoring.env` and uses its configured database path. The
underlying development module accepts `--db`; the static
`/var/lib/gost-manager/metrics.sqlite3` value is only the fallback used when a
new default configuration is constructed.

```bash
python3 -m monitoring.query_cli snapshot
python3 -m monitoring.query_cli live --refresh 2
python3 -m monitoring.query_cli summary --window 10m
python3 -m monitoring.query_cli summary --start 2026-07-11T10:00:00Z --end 2026-07-11T11:00:00Z
python3 -m monitoring.query_cli host --window 30m
python3 -m monitoring.query_cli network --window 30m
python3 -m monitoring.query_cli services --window 30m
python3 -m monitoring.query_cli service nginx.service --window 30m
python3 -m monitoring.query_cli tunnels --window 1h
python3 -m monitoring.query_cli tunnel iran-1 --window 1h
python3 -m monitoring.query_cli collector --window 1h
python3 -m monitoring.query_cli events --window 1h --severity warning,error
python3 -m monitoring.query_cli export --window 1h --format json --granularity auto --output -
```

Durations must be an integer followed by `s`, `m`, `h`, or `d`, such as `90s`, `15m`, `2h`, or `2d`. Zero, negative, ambiguous, overflowing, future-only, and greater-than-30-day windows are rejected. Absolute `--start` and `--end` must be supplied together and must include `Z` or an explicit UTC offset. Results retain both the requested and effective window and mark retention truncation.

The shared planner reads `collector_state.minute_rollup_watermark` before choosing a source. Only minute starts strictly before that watermark are finalized rollups. A valid watermark is a non-negative integer aligned to a 60-second boundary and no later than the current completed-minute boundary. Future, misaligned, negative, nonnumeric, or clock-rollback-invalid values are not clamped or trusted; they use the same safe bounded raw fallback as a missing watermark. Complete wall-clock minutes after a valid watermark remain raw, because ordinary 15-minute maintenance means rollups normally lag by as much as about 15 minutes and may lag longer after failure or backlog. Safe recent windows remain raw; larger windows combine finalized rollups with every available raw point after the watermark and report `hybrid`. A stale but valid watermark is allowed, and never turns an available raw tail into artificial missing coverage.

Preflight uses bounded indexed `LIMIT budget+1` queries instead of an unbounded `COUNT(*)`. A raw result within 100,000 rows is materialized and supports weighted p95. Larger raw regions are processed in entity/metric/time order by a constant-memory per-series accumulator; no SQLite temporary table or write is used. A separate configurable `max_stream_scan_rows` ceiling defaults to 1,000,000 raw rows and applies to streamed summaries plus grouped raw-minute `minute`/`auto` exports. Work above that ceiling raises `QueryLimitError`/exit code 4 before an export destination is created. Streamed raw summaries report `raw` or `hybrid` honestly, track rows scanned and peak rows buffered, and leave p95 unavailable. Auto/minute export uses persisted rollups before the watermark and a read-only grouped raw-minute stream after it. The summary materialization cap is 110,000 rows, series 5,000, current entities 256, health events 200, and exports 100,000 rows.

At the accepted 583-series production cardinality with a realistic 15-minute maintenance lag, 10 minutes remains `raw`; 30 minutes, 1 hour, and 2 hours are `hybrid`. Tests cover 0, 1, 5, 14, and 20-minute lag, missing/stalled watermark, and a physically missing finalized rollup row. Valid-watermark plans scan at most 158,313 rows and materialize/buffer at most 105,843 rows with ten SELECT statements per summary. The accepted missing-watermark two-hour fallback scans 760,663 raw rows without materializing the full result; an oversized raw window is rejected at the separate 1,000,000-row scan ceiling.

At the raw-retention boundary, complete rollup minutes end before the cutoff and raw begins exactly at the cutoff. A partial unrepresentable boundary remains missing coverage, including when the requested end occurs before the next minute boundary. No full rollup minute outside the requested interval is used and retained raw-tail points are never discarded. Expected samples use `collector_state.metric_cadence_seconds`, including 5-second fast, 30-second full-socket, and 60-second slow families; unknown families use the collector's 5-second default.

Raw numeric averages are piecewise-constant and weighted by observed duration. SQL returns at most the last valid pre-window seed per series within 2.5 cadence intervals, and no value is carried across a larger gap. Raw p95 uses deterministic weighted nearest rank: values are ordered, elapsed weights are accumulated, and the first value reaching 95% of covered time is selected.

Minute rollups expose two distinct concepts. Observation coverage is `samples / expected_samples` and includes unavailable observations because they arrived. Numeric-value coverage is `(samples - unavailable_count) / expected_samples`; only this valid fraction weights `avg_value`. An all-unavailable minute still contributes samples, expected samples, unavailable/reset/gap counts, and worst quality, but contributes zero numeric average weight. Min/max ignore NULL values. Hybrid averages combine that corrected rollup numeric weight with raw elapsed-time weight. Complete historical minutes use each row's stored `expected_samples`, so later cadence changes do not rewrite historical coverage; missing rows and partial boundaries add explicit uncovered expectations.

`monitoring.metric_semantics` is the single statistics classifier. Gauge and rate series expose latest, min, time-aware average, max, and raw-only weighted p95. Cumulative counters expose latest, first/last timestamps, quality, reset/gap and coverage counts, but no average or p95. Categorical booleans/states, identities such as PID/start identity/endpoint, and Unix timestamps expose latest, timestamp, age, quality, and meaningful transitions without numeric min/average/max/p95. Unknown numeric metrics use the conservative fallback and receive no range, average, or p95 until classified. Rollup, streamed-minute, and hybrid results never fabricate p95.

Detailed output exposes unit, semantic quality, sample/expected counts, coverage, unavailable/reset/gap counts, observation age, and source mode. Exit codes are stable: `0` success, `2` invalid input/window or missing selected entity, `3` missing/corrupt/unsupported database, `4` query/export safety limit, and `130` interrupt.

`snapshot` always produces plain text. It reads the latest point independently for each selected dashboard/health series using bounded entity/metric pairs and correlated `idx_metric_points_lookup` lookups; it does not scan 48 hours or restrict all families to the newest cycle. The latest collector cycle is read separately. Every point retains its own UTC timestamp, age, cadence, quality, and stale flag. Fast values use 5-second freshness, full-socket values 30 seconds, slow filesystem/database/FD values 60 seconds, and checkpoint values the 15-minute maintenance cadence; the accepted freshness limit is 2.5 cadence intervals. A stale point may remain visible with age but is unavailable to required health decisions.

`live` uses ANSI only on a real TTY; pipes, `TERM=dumb`, `NO_COLOR`, and `--no-color` use plain refreshes. Refresh is bounded to 0.2 through 60 seconds, `--iterations` permits finite runs, terminal width is sampled for each refresh, and cursor state is restored after interrupt, termination, or rendering failure. Operator timestamps use UTC ISO-8601 `Z` plus relative age rather than raw Unix timestamps.

### Read-only guarantee

The query layer opens SQLite with `mode=ro`, enables `PRAGMA query_only=ON` and a five-second busy timeout, and validates schema v4 using reads only. Each multi-query operation starts one explicit read transaction and rolls it back immediately after the related reads. Snapshot cycle, latest-per-series points, bounded service/tunnel metadata, display events, and health events therefore come from one coherent WAL snapshot; every live refresh opens a new transaction and no transaction is held while sleeping. Export preflight and streaming share one bounded read transaction, so a normal concurrent collector commit cannot create a false row-count mismatch. The layer never calls migration, initialization, retention, maintenance, or checkpoint functions and never executes DDL or DML. Tests inspect SQLite trace output for `INSERT`, `UPDATE`, `DELETE`, `CREATE`, `ALTER`, `DROP`, `REPLACE`, `VACUUM`, and `wal_checkpoint` and exercise concurrent WAL writers.

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

Exports support `summary`, `raw`, `minute`, and `auto` granularity with exact entity and metric filters. `auto` uses raw data for a recent small window and otherwise follows the raw/rollup/hybrid planner. JSON metadata includes:

- export and database schema versions;
- UTC Unix generation time;
- requested and effective windows;
- source mode and selected granularity;
- filters and retention policy;
- row count and truncation state.

Export schema v2 rows include entity identity, metric, semantic category, `aggregate_kind`, `value_available`, timestamp or minute start, numeric/text value, unit, quality, reset/gap markers, and coverage fields. Gauge and rate raw-minute rows use `minute_statistics` and may expose minimum, average, and maximum. Cumulative counters, booleans, categorical state, PID/start identity, Unix timestamps, endpoints, and unknown metrics never expose an arithmetic minute average: raw-minute rows preserve the deterministic latest value and timestamp as `minute_latest`, while stored rollups that lack a meaningful last value use `historical_value_unavailable` with `value_available=false`. All-unavailable minute series use the same explicit unavailable representation and retain sample, expected, unavailable, coverage, reset/gap, quality, and source metadata. JSON and CSV carry equivalent fields for these semantics.

CSV uses one stable RFC-style table with the fixed header documented by `monitoring.exporters.CSV_FIELDS`: the first `record_type=metadata` row independently carries export/schema versions, generated UTC and epoch time, requested/effective UTC and epoch windows, actual source mode, granularity, truncation, all three retention values, filters, and data-row count. Data rows repeat that metadata. Summary rows additionally preserve latest/latest timestamp, min/time-aware average/max/p95, sample/expected/coverage, unavailable/reset/gap counts, first/last timestamps, transitions, and age. A zero-data CSV still contains its metadata row; there is no ambiguous preamble.

Windows are capped at 30 days, query series at 5,000, and estimated and actual export rows at 100,000. The bounded estimate is checked before an output file is created. Rows are fetched in bounded batches; files use a same-directory temporary file, mode `0600`, atomic replace, and temporary cleanup on failure. `--output -` streams to stdout.

Exports read only the sanitized metric/entity tables and never raw env files or arbitrary collector state. Keys and text matching credential, username, password, token, authorization, or secret forms are removed or redacted as a second defense. Secret-canary fixtures verify that these values do not appear in JSON or CSV.

## Performance guardrails

- Collector defaults to 5-second sampling, not per-second polling.
- Expensive commands such as full socket enumeration run at a lower cadence or only for managed ports.
- Use `/proc`, cgroup, and local status files before spawning commands.
- Use prepared SQLite statements and batch one sample in one transaction.
- Bound query result size and export windows.
- Collector CPU, RSS, database write latency, sample duration, and missed deadlines are themselves monitored.
- A collector overrun skips or delays monitoring work; it never applies backpressure to traffic services.

### Representative storage budget

The planning profile is one NGINX unit with one master and two workers plus six managed GOST services. It assumes five-second fast samples, 30-second full socket snapshots, 60-second FD/limit/filesystem samples, 48-hour raw retention, 30-day minute-rollup retention, and an explicit 30-day structured-event retention policy.

The current metric-family model, measured with deterministic fixtures for that exact service profile, records 522 points per fast cycle, 9 additional points per full socket cycle, and 52 additional points per slow cycle. The completed-minute rollup has approximately 583 metric series. The resulting retained row counts are:

- 9,120,960 metric points per day;
- 18,241,920 raw metric points over 48 hours;
- 25,185,600 minute-rollup rows over 30 days;
- 34,560 `sample_cycles` rows and 241,920 `metric_samples` rows over 48 hours;
- 150,000 retained event rows at 5,000 deduplicated events per day over the independent 30-day event window, plus 2,048 entity rows.

The deterministic capacity estimate uses 128 bytes per raw metric row and 160 bytes per minute-rollup row before indexes. It reserves 128 bytes per sample-cycle row, 192 bytes per metric-sample row, and 512 bytes per event or entity row. Small schema, tunnel, and collector-state tables are covered by the entity allowance and the free-page factor. It then adds 50 percent of table bytes for SQLite primary-key and secondary indexes, B-tree fill variance, and reusable free pages, followed by 20 percent for WAL growth, checkpoints, and operational headroom.

Under those conservative assumptions the estimated occupancy is:

- 2.17 GiB for the raw `metric_points` table;
- 3.75 GiB for the `minute_rollups` table;
- 0.12 GiB for `sample_cycles`, `metric_samples`, events, and entities;
- 3.02 GiB for indexes and free-page overhead;
- 1.81 GiB for WAL and operational headroom;
- 10.89 GiB estimated total database footprint.

Operators should reserve at least 12 GiB for the monitoring database under this profile. A 5 GiB reservation is not sufficient once 30-day minute rollups are included. Hosts with more interfaces, disks, services, metric cardinality, or event volume need additional space; reducing raw, rollup, or event retention or reducing metric cardinality lowers the requirement. Maintenance deletes structured events with timestamps older than `EVENT_RETENTION_SECONDS`; events exactly at the cutoff remain. `EVENT_RETENTION_SECONDS` is an explicit 30-day policy and does not alias the rollup-retention constant.

Process CPU/stat and aggregate RSS/thread observations remain on the fast cadence. `/proc/<pid>/fd`, process limits, cgroup file memory, filesystem capacity, and database-size observations use the slow cadence. A service PID set comes from `cgroup.procs`; MainPID fallback totals are estimated rather than exact. Only a complete authoritative cgroup PID set plus complete fast process snapshots advances process-set transition state. A missing fast snapshot makes process metrics unavailable for that cycle without emitting `pid_replaced`, and non-authoritative MainPID fallback never overwrites the last authoritative identity. Identity-bound socket and slow-process caches are neither read nor replaced when the current identity cannot be confirmed. Inactive historical source-error keys are retained for at most 48 hours and capped at 64 keys, while the global error total remains cumulative.

The deterministic performance suite parses and attributes a synthetic 20,000-row socket snapshot within the five-second cycle budget and verifies that a synthetic 10,000-entry FD directory is enumerated once, not six times, across six five-second cycles.

The query performance fixture uses the accepted production shape directly: 522 fast points per 5-second cycle, 9 full-socket extras per 30 seconds, 52 slow extras per 60 seconds, 583 rollup series, one NGINX service, six GOST services, six tunnels, and three interfaces. It stores 823,420 raw points plus retained-history noise and a realistic 15-minute rollup lag. It verifies 10m/30m/1h/2h results, at most ten SELECT statements per summary, 158,313 maximum rows scanned, 105,843 maximum rows buffered/materialized, and actual `idx_metric_points_time`, `idx_metric_points_lookup`, `idx_minute_rollups_time`, and `idx_events_time` query plans, including the grouped raw-minute export query.

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

The accepted collector core uses `/var/lib/gost-manager/metrics.sqlite3` by default and samples every 5 seconds.  It uses `time.monotonic()` scheduling primitives, explicit SQLite sample transactions, WAL mode, busy timeout, foreign keys, 48-hour raw retention, 30-day one-minute rollup retention, explicit 30-day structured-event retention, and 15-minute maintenance cadence.

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
