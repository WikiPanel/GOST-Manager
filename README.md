# GOST Manager

GOST Manager is a menu-based Bash project for installing GOST v3 and managing numbered Iran/Kharej Direct Mode tunnels with systemd on Ubuntu servers.

Direct Mode is the only supported traffic mode in v0.2. The project installs the official `go-gost/gost` release artifact unchanged; GOST Manager is only an installer, configuration, and service wrapper and does not alter upstream protocol behavior. Multiple independent Iran and Kharej profiles are supported.

NGINX Gateway and Native GOST Gateway are cancelled. There is no placeholder, hidden command, route runtime, controller, failover layer, or NGINX dependency. Direct Mode profile management supports safe create, inspect, edit, clone, restart, and delete operations without changing the independent-process traffic architecture.

## Supported OS

- Ubuntu 22.04 LTS
- Ubuntu 24.04 LTS

## Supported Architectures

- `x86_64` / `amd64`
- `aarch64` / `arm64`

## Installation

Clone the repository on the server and run:

```bash
sudo bash install.sh
sudo gost-manager
```

The installer copies:

- `gost-manager.sh` to `/usr/local/sbin/gost-manager`
- `lib/gost-run-iran.sh` to `/usr/local/lib/gost-manager/gost-run-iran.sh`
- `lib/gost-run-kharej.sh` to `/usr/local/lib/gost-manager/gost-run-kharej.sh`
- the complete monitoring package to `/usr/local/lib/gost-manager/monitoring`
- `gost-monitor`, `gost-monitor-collector`, and `gost-monitor-admin` to `/usr/local/sbin`
- `/etc/gost-manager/monitoring.env`
- `/etc/systemd/system/gost-monitor-collector.service`
- monitoring history to `/var/lib/gost-manager/metrics.sqlite3`

The monitoring collector is enabled and started on a fresh install. Upgrades preserve a valid operator-modified monitoring config, monitoring history, and the collector's enabled/active state. Existing `/etc/gost/iran-*.env`, `/etc/gost/kharej-*.env`, tunnel units, and traffic service state are not changed.

The installer copies only modules listed in `packaging/monitoring-runtime-manifest.txt`. Existing shared `/usr/local/sbin` and `/etc/systemd/system` metadata is preserved, as is existing `/etc/gost` ownership, mode, content, and file metadata. Private manager directories are enforced and their prior metadata is restored if a later installation phase fails.

Direct run also works:

```bash
sudo bash gost-manager.sh
```

## Install or Update GOST

Choose:

```text
1) Install / Update GOST
```

The manager downloads from official `go-gost/gost` GitHub Releases only. It ignores prereleases, detects `amd64` or `arm64`, avoids `amd64v3`, downloads a checksum file when available, verifies SHA256 when possible, backs up an existing binary as `/usr/local/bin/gost.bak.<timestamp>`, installs `/usr/local/bin/gost`, and prints:

```bash
/usr/local/bin/gost -V
```

## Kharej Tunnel Example

On the Kharej server, choose:

```text
2) Create Kharej tunnel
```

Example inputs:

```text
Kharej profile number [1]:
Profile label (optional): kharej-edge
SOCKS listen port: 28420
GOST username: maya
GOST password: [hidden and confirmed]
Allowed Iran IPv4/CIDRs: 198.51.100.10,198.51.100.11/32
Apply profile-scoped iptables firewall rules? yes
```

This creates:

```text
/etc/gost/kharej-1.env
/etc/systemd/system/gost-kharej-1.service
```

The service runs a SOCKS5 listener on `0.0.0.0:28420`.

## Iran Tunnel Example: 2052

On the Iran server, choose:

```text
3) Create Iran tunnel
```

Example inputs:

```text
Iran profile number [1]:
Profile label (optional): iran-edge
Kharej IP: 203.0.113.20
Kharej SOCKS port: 28420
GOST username: maya
GOST password: [hidden matching value]
Port mappings: 2052:2052
```

`Port mappings` is required for every Iran tunnel. Empty values, invalid formats, invalid ports, and duplicate Iran listen ports are rejected before any files are written.

Traffic flow:

```text
Iran :2052 -> gost-iran-1 -> Kharej :28420 SOCKS5 -> Kharej 127.0.0.1:2052
```

## Iran Tunnel Example: 80/8080/8880

Use:

```text
Port mappings: 80:80,8080:8080,8880:8880
```

Traffic flow:

```text
Iran :80   -> Kharej 127.0.0.1:80
Iran :8080 -> Kharej 127.0.0.1:8080
Iran :8880 -> Kharej 127.0.0.1:8880
```

GOST listens directly on the public Iran ports. Nginx is not placed in the tunnel path.

## Operations

Run:

```bash
sudo gost-manager
```

Use the menu:

```text
1) Install / Update GOST
2) Create Kharej tunnel
3) Create Iran tunnel
4) Delete tunnel
5) Show status
6) Show logs
7) Restart tunnel
8) List active GOST services
9) Clean old/broken GOST configs
10) Monitoring
11) Server Stability
0) Exit
```

For delete, status, logs, and restart, the manager now shows a numbered tunnel selector. You no longer need to type `iran` or `kharej` manually.

```text
Available GOST tunnels:

1) gost-iran-1.service      active/running    /etc/gost/iran-1.env
2) gost-kharej-1.service    active/running    /etc/gost/kharej-1.env

Select tunnel number:
```

Each numbered tunnel is independent. Deleting `iran-2` does not affect `iran-1`; deleting `kharej-2` does not affect `kharej-1`.

Option `8` first renders every discovered profile and then opens:

```text
Direct Mode profiles
====================

1) List all profiles
2) Show profile detail
3) Edit a profile
4) Clone a profile
5) Restart selected profiles
6) Restart all profiles
0) Back
```

Iran and Kharej use independent positive-number spaces. Creation and clone suggest the first gap found across both env and unit files, so an orphaned file is never overwritten. `PROFILE_LABEL` is optional display metadata; the stable identity, filename, and service remain `side-number`. Existing unlabeled profiles remain valid.

Create, edit, and clone validate configured local ports across both sides and take one live `ss` snapshot before activating a new port. Edit preserves unknown well-formed env keys and existing credentials unless explicitly replaced, shows a redacted diff, and performs no write or restart for a no-op. Clone never changes its source. Selected restart accepts exact comma-separated IDs, deduplicates them, and never uses a wildcard service command.

## Server Stability

Option `11` runs one automatic operational wizard. It reports current kernel
values, installs the managed file
`/etc/sysctl.d/99-gost-stability.conf`, applies it with `sysctl --system`, and
verifies every recommended value. The managed settings cover file capacity,
socket backlog, the local port range, SYN backlog, FIN timeout, TCP keepalive,
and slow-start-after-idle. The wizard intentionally does not set
`net.ipv4.tcp_tw_reuse`.

For each exact existing `gost-iran-N.service` or
`gost-kharej-N.service`, it installs
`/etc/systemd/system/<service>.d/stability.conf` with
`LimitNOFILE=1048576`, `TasksMax=infinity`, `OOMScoreAdjust=-500`,
`Restart=always`, and `RestartSec=3`. Unrelated services and the original
units/env files are not changed. At most one `systemctl daemon-reload` is run
when drop-ins change, and no GOST service is restarted. New process limits
therefore apply after the operator's next normal service restart.

The wizard is idempotent: an already optimized host receives no unnecessary
file replacement, sysctl apply, or daemon reload. Symlinked or conflicting
unmanaged destinations are rejected rather than overwritten. Existing managed
stability files are backed up before an update.

## Local Monitoring

Option `10` opens the compact Monitoring Lite workflow:

```text
1) Live resources
2) Last 10 minutes
3) Last 30 minutes
4) Last 1 hour
5) Services and tunnels
6) Collector status
7) Advanced tools
0) Back
```

The normal live view focuses on host, network, TCP connections, Direct Mode GOST services, tunnels, and collector health. Monitoring is local and optional, has no NGINX dependency, and never enters the traffic path. Existing snapshot/detail/event/export/maintenance/service-control commands remain available under `Advanced tools`. Monitoring command failures return to the manager menu and never trigger traffic service actions.

Direct commands are also available:

```bash
systemctl status gost-monitor-collector.service
systemctl start gost-monitor-collector.service
systemctl stop gost-monitor-collector.service
systemctl restart gost-monitor-collector.service

gost-monitor snapshot
gost-monitor live
gost-monitor summary --window 10m
gost-monitor-admin status
gost-monitor-admin maintenance
```

The strict root-owned mode-`0600` config contains only:

```text
GOST_MONITOR_DB=/var/lib/gost-manager/metrics.sqlite3
GOST_ENV_DIR=/etc/gost
GOST_MONITOR_SAMPLE_INTERVAL=10
GOST_MONITOR_TCP_INTERVAL=30
GOST_MONITOR_SLOW_INTERVAL=60
GOST_MONITOR_MAINTENANCE_INTERVAL=900
```

Bounds are sample 5..60 seconds; TCP 10..300 and not below sample; slow 30..900 and not below sample; maintenance 300..86400 and not below slow. The file is parsed as strict `KEY=VALUE` data and is never sourced or executed. Unknown/duplicate keys, relative paths, shell substitutions, unsafe quoting, and invalid cadence combinations are rejected before collection starts.

The generic parser used by library tests accepts safe absolute paths. The installed service has a narrower policy: `GOST_MONITOR_DB` must name a file below `/var/lib/gost-manager` and `GOST_ENV_DIR` must be `/etc/gost` or a descendant. Alternate names such as `/var/lib/gost-manager/custom.sqlite3` and nested paths such as `/var/lib/gost-manager/archive/current.sqlite3` are supported; `/srv`, `/root`, `/tmp`, prefix lookalikes, and symlink traversal are rejected. Inspect only the validated non-secret fields with:

```bash
gost-monitor-admin config --format json
gost-monitor-admin config --format value --field database_path
```

SQLite remains the dependency-free, restart-safe local history store. Monitoring Lite retains 6 hours of raw points, 24 hours of minute rollups, and 24 hours of structured events. The conservative estimate for six GOST services is about 0.451 GiB, including indexes, reusable pages, WAL, and operational headroom; reserve 1 GiB. `gost-monitor-admin maintenance` runs rollup/retention in one transaction and checkpoints after commit.

The deterministic 1,000-user fixture validates monitoring parsing, attribution, storage, and scheduling overhead; it is not a network-capacity claim. Production throughput still depends on CPU, kernel, NIC, encryption, GOST, RTT, CDN, and the server provider.

The daemon, one-shot collector, and destructive history purge share the private advisory lock `/run/gost-manager/collector.lock`. A second collector or a direct purge while collection is active returns exit code `4`. The manager asks before temporarily stopping an active collector for one-shot diagnostics and restores it after success, failure, or interrupt. History deletion requires the exact phrase `DELETE MONITORING HISTORY`, resolves and displays the configured database, checkpoints WAL, refuses a busy checkpoint, creates same-directory hard-link recovery anchors, performs one atomic canonical replacement, fsyncs durability boundaries, and restores the original DB and sidecars after an injected failure. It does not touch traffic or `/etc/gost`.

The collector service has bounded restart behavior, low CPU/I/O priority, private state permissions, and no `Requires=`, `PartOf=`, `BindsTo=`, stop, restart, or reload relationship with GOST tunnel services. Collector failure, corrupt history, maintenance failure, or monitoring removal cannot stop Direct Mode traffic.

## Safe Uninstall

Run `sudo bash uninstall.sh`. Every component defaults to No and is confirmed independently: manager CLI, monitoring service, monitoring code, monitoring config, monitoring history, managed traffic services, `/etc/gost` credentials/backups, and the GOST binary. A final plan is shown before changes.

Removing monitoring only leaves tunnel units, active traffic, runners, `/etc/gost`, the GOST binary, firewall state, and unrelated host services unchanged. History and config are separate choices. Monitoring code cannot be removed while its service remains; runners are retained while managed traffic units remain. If history/config are retained, a later `sudo bash install.sh` restores the monitoring code and validates/migrates the retained database.

Removal decisions are rechecked against actual post-action state. If any exact managed traffic unit survives, both runners, `/etc/gost`, and `/usr/local/bin/gost` are preserved even when deletion was selected. If the collector is active, enabled, or loaded despite a missing unit file, removal still attempts to stop/disable it; a failure preserves monitoring code, launchers, config, and history. History removal uses the configured DB captured before optional config deletion and never guesses the default path.

## Linux systemd verification

`tests/test-systemd-linux.sh` uses the host's real `systemd-analyze verify` environment and temporary executable/config paths; it does not use an incomplete synthetic `--root`. It skips only off Linux or when the real binary is unavailable. `.github/workflows/monitoring-integration.yml` runs `make check`, the temporary-root installer, and real systemd verification on Ubuntu 22.04 and 24.04.

If installer service-state rollback cannot be verified, it retains collision-resistant backup directories and prints exact `rm`, `cp -a`, `systemctl daemon-reload`, enable/disable, start/stop, and status commands for restoring the recorded collector state. Do not delete those backups until the printed status check succeeds.

## Firewall Notes

The Kharej SOCKS5 listener must not be public. Enable the optional firewall rule so only the configured Iran IPv4 sources can reach the SOCKS port. New profiles use `ALLOWED_IRAN_SOURCES` with up to 64 canonical IPv4 `/8` through `/32` networks; plain addresses become `/32`. Legacy `IRAN_IP` profiles remain valid and are not rewritten during listing or upgrade.

The manager uses iptables comments:

```text
gost-manager:kharej-<number>:allow
gost-manager:kharej-<number>:drop
```

Each canonical source receives one ACCEPT rule before the profile's final DROP rule. These comments let edit, rollback, and deletion mutate only the matching profile rules; unrelated rules and other profiles remain untouched.

Warning: iptables rules are not persistent by default. They may be lost after reboot unless saved with `netfilter-persistent` or your server firewall system.

## Security Notes

- Real passwords belong only in `/etc/gost/*.env`.
- Password input is hidden and confirmed; list, detail, status, summaries, and failures never print credentials.
- Env files are installed with permission `600`.
- Env replacement uses a private same-directory temporary file and atomic replacement; unit files remain `0644`.
- `/etc/gost` is installed with permission `700`.
- Do not commit real passwords, production IPs, tokens, or private credentials.
- The manager does not use `eval`.
- Keep the Kharej SOCKS port firewalled.

## Troubleshooting

- If a local port is configured by another profile or occupied live, the manager prints a bounded profile/PID ownership summary and does not create files. Unknown ownership is treated as a conflict.
- If a systemd service fails, use menu option `5` for status and option `6` for logs.
- If a tunnel was partially removed, use menu option `9` to find managed orphan env files, service files, failed services, and old backups.
- If GOST install fails, verify outbound HTTPS access to `github.com` and `api.github.com`.
