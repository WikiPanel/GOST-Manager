# GOST Manager

GOST Manager is a menu-based Bash project for installing GOST v3 and managing numbered Iran/Kharej tunnels with systemd on Ubuntu servers.

It installs or updates the official `go-gost/gost` binary, creates independent numbered tunnel services, stores each tunnel configuration under `/etc/gost`, and provides menu actions for status, logs, restart, delete, listing, and safe cleanup.

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
Tunnel number: 1
SOCKS listen port: 28420
GOST username: maya
GOST password: leave empty to generate
Iran IP allowed: YOUR_IRAN_SERVER_IP
Apply iptables firewall rule? yes
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
Tunnel number: 1
Kharej IP: YOUR_KHAREJ_SERVER_IP
Kharej SOCKS port: 28420
GOST username: maya
GOST password: value_from_kharej
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
4) Delete tunnel
5) Show status
6) Show logs
7) Restart tunnel
8) List active GOST services
9) Clean old/broken GOST configs
10) Monitoring
11) Native GOST Gateway (Coming soon)
```

For delete, status, logs, and restart, the manager now shows a numbered tunnel selector. You no longer need to type `iran` or `kharej` manually.

```text
Available GOST tunnels:

1) gost-iran-1.service      active/running    /etc/gost/iran-1.env
2) gost-kharej-1.service    active/running    /etc/gost/kharej-1.env

Select tunnel number:
```

Each numbered tunnel is independent. Deleting `iran-2` does not affect `iran-1`; deleting `kharej-2` does not affect `kharej-1`.

## Local Monitoring

Option `10` opens snapshot/live views, 10-minute/30-minute/1-hour/custom summaries, host/network/service/tunnel/collector details, events, JSON/CSV export, collector controls, one-shot diagnostics, maintenance, and explicit history deletion. Monitoring command failures return to the manager menu and never trigger traffic service actions.

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
GOST_MONITOR_SAMPLE_INTERVAL=5
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

Default retention is 48 hours of raw points, 30 days of minute rollups, and 30 days of structured events. Reserve at least 12 GiB for the representative one-NGINX-plus-six-GOST profile. `gost-monitor-admin maintenance` runs rollup/retention in one transaction and checkpoints after commit.

The daemon, one-shot collector, and destructive history purge share the private advisory lock `/run/gost-manager/collector.lock`. A second collector or a direct purge while collection is active returns exit code `4`. The manager asks before temporarily stopping an active collector for one-shot diagnostics and restores it after success, failure, or interrupt. History deletion requires the exact phrase `DELETE MONITORING HISTORY`, resolves and displays the configured database, checkpoints WAL, refuses a busy checkpoint, creates same-directory hard-link recovery anchors, performs one atomic canonical replacement, fsyncs durability boundaries, and restores the original DB and sidecars after an injected failure. It does not touch traffic or `/etc/gost`.

The collector service has bounded restart behavior, low CPU/I/O priority, private state permissions, and no `Requires=`, `PartOf=`, `BindsTo=`, stop, restart, or reload relationship with NGINX or GOST tunnel services. Collector failure, corrupt history, maintenance failure, or monitoring removal cannot stop Direct Mode traffic.

Option `11`, `Native GOST Gateway (Coming soon)`, only prints a message and returns. It performs no dependency, package, filesystem, service, database, firewall, NGINX, or GOST action.

## Safe Uninstall

Run `sudo bash uninstall.sh`. Every component defaults to No and is confirmed independently: manager CLI, monitoring service, monitoring code, monitoring config, monitoring history, managed traffic services, `/etc/gost` credentials/backups, and the GOST binary. A final plan is shown before changes.

Removing monitoring only leaves tunnel units, active traffic, runners, `/etc/gost`, the GOST binary, firewall state, and NGINX unchanged. History and config are separate choices. Monitoring code cannot be removed while its service remains; runners are retained while managed traffic units remain. If history/config are retained, a later `sudo bash install.sh` restores the monitoring code and validates/migrates the retained database.

Removal decisions are rechecked against actual post-action state. If any exact managed traffic unit survives, both runners, `/etc/gost`, and `/usr/local/bin/gost` are preserved even when deletion was selected. If the collector is active, enabled, or loaded despite a missing unit file, removal still attempts to stop/disable it; a failure preserves monitoring code, launchers, config, and history. History removal uses the configured DB captured before optional config deletion and never guesses the default path.

## Linux systemd verification

`tests/test-systemd-linux.sh` uses the host's real `systemd-analyze verify` environment and temporary executable/config paths; it does not use an incomplete synthetic `--root`. It skips only off Linux or when the real binary is unavailable. `.github/workflows/monitoring-integration.yml` runs `make check`, the temporary-root installer, and real systemd verification on Ubuntu 22.04 and 24.04.

If installer service-state rollback cannot be verified, it retains collision-resistant backup directories and prints exact `rm`, `cp -a`, `systemctl daemon-reload`, enable/disable, start/stop, and status commands for restoring the recorded collector state. Do not delete those backups until the printed status check succeeds.

## Firewall Notes

The Kharej SOCKS5 listener must not be public. Enable the optional firewall rule so only the Iran server IP can reach the SOCKS port.

The manager uses iptables comments:

```text
gost-manager:kharej-<number>:allow
gost-manager:kharej-<number>:drop
```

These comments let deletion remove only the matching managed rules.

Warning: iptables rules are not persistent by default. They may be lost after reboot unless saved with `netfilter-persistent` or your server firewall system.

## Security Notes

- Real passwords belong only in `/etc/gost/*.env`.
- Env files are installed with permission `600`.
- `/etc/gost` is installed with permission `700`.
- Do not commit real passwords, production IPs, tokens, or private credentials.
- The manager does not use `eval`.
- Keep the Kharej SOCKS port firewalled.

## Troubleshooting

- If an Iran tunnel cannot be created because a port is busy, the manager prints the `ss -lntp` owner for each busy port and does not create files.
- If a systemd service fails, use menu option `5` for status and option `6` for logs.
- If a tunnel was partially removed, use menu option `9` to find managed orphan env files, service files, failed services, and old backups.
- If GOST install fails, verify outbound HTTPS access to `github.com` and `api.github.com`.
