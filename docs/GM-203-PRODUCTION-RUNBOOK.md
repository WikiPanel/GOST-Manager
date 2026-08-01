# GM-203 production rollout

This runbook is for a human operator after the v2.0.3 release is published. It
does not authorize Codex or an automated job to change production. Run it first
on `mserver2` (v2.0.2), then on `mserver1` (v2.0.1), during an approved window.

GM-203 does not enable BBR, change qdisc/sysctl/firewall rules, add resource
limits, or alter GOST mappings, ports, credentials, env files, or traffic
units. Its collector improvements do not make approximately 2 GiB RAM adequate
for 8,000 concurrent connections. Add RAM or limit traffic before rollout if
memory headroom is still unsafe.

## 1. Preflight

Confirm that RAM has been increased or traffic is limited. Do not continue when
`MemAvailable` is persistently below 10%, swap is actively thrashing, or the
host is already missing collector deadlines.

From a reviewed v2.0.3 source tree, create a private snapshot:

```bash
sudo scripts/gm-203-production-check.sh preflight \
  /root/gm-203-preflight-$(date -u +%Y%m%dT%H%M%SZ)
```

The script is read-only except for its mode-0700 output directory. It records:

- exact GOST `MainPID`, `NRestarts`, active state, env hashes, and unit hashes;
- firewall output from available nftables/iptables tools;
- Watchdog profile mode, check mode, maintenance, ownership, and manual override;
- a mode-0600 Monitoring config backup;
- manager version, memory/load, and Monitoring/Watchdog database identity/size.

Store the baseline path. Snapshot output can contain production topology and
must remain private. Review `gost-watchdog-admin status --json` separately and
confirm all automatic ownership and pending actions are understood.

## 2. Pinned upgrade

Install only the published pinned release:

```bash
GOST_MANAGER_VERSION=v2.0.3 \
bash <(curl -fsSL https://raw.githubusercontent.com/WikiPanel/GOST-Manager/main/setup.sh)
```

The installer may manage the central collector and Watchdog services. It must
not start, stop, or restart any `gost-iran-N.service` or
`gost-kharej-N.service`, and it must preserve existing Monitoring and Watchdog
configuration and history.

## 3. Zero-traffic verification

Compare the new state with the exact preflight directory:

```bash
sudo scripts/gm-203-production-check.sh verify \
  /root/gm-203-preflight-YYYYmmddTHHMMSSZ \
  /root/gm-203-postflight-$(date -u +%Y%m%dT%H%M%SZ)
```

Stop if any GOST PID, restart count, env hash, unit hash, firewall snapshot,
Watchdog policy/manual override, or Monitoring config changed. The installed
version must be exactly `2.0.3`. Also confirm existing history is readable:

```bash
gost-monitor snapshot
gost-watchdog-admin status
```

## 4. Monitoring preset

Display the current values first:

```bash
sudo gost-monitor-admin config
```

Apply the atomic preset and explicitly request a restart of only the collector:

```bash
sudo gost-monitor-admin apply-preset high-traffic --restart-collector
```

Expected intervals are `20 / 60 / 120 / 1800` seconds. The result prints the
previous values, updated values, and backup path. Confirm that only
`gost-monitor-collector.service` restarted and no GOST `MainPID` or `NRestarts`
changed. Reapplying the preset is a no-op and performs no restart.

## 5. Watchdog rollout

TCP check proves only that `KHAREJ_IP:TUNNEL_PORT` accepts a TCP connection. It
does not prove SOCKS authentication or end-to-end forwarding. Existing profiles
remain on Ping after upgrade; Auto Protect and manual override remain unchanged.

For `iran-1` through `iran-6`, validate the target and test before changing it:

```bash
sudo gost-watchdog-admin effective iran-1
sudo gost-watchdog-admin set-check-mode iran-1 tcp
sudo gost-watchdog-admin probe iran-1
sudo gost-watchdog-admin status --profile iran-1
```

Repeat deliberately for profiles 2 through 6. Leave `iran-7` Disabled. Changing
check mode does not restart GOST and does not clear manual override. To roll a
profile back to ICMP reachability:

```bash
sudo gost-watchdog-admin set-check-mode iran-1 ping
```

For `iran-1`, re-arm only when the service is active, the TCP probe succeeds,
maintenance is off, pending action is empty, and Watchdog ownership is false:

```bash
sudo gost-watchdog-admin status --profile iran-1 --json
sudo gost-watchdog-admin rearm iran-1
```

Re-arm is always explicit. A `probe_error` indicates a local execution/config
problem; it does not increment upstream failure/success counters and cannot
start or stop traffic.

## 6. Controlled test A: ICMP-only failure

In an approved isolated test window, have the network operator block ICMP only
to one selected Kharej while keeping its GOST TCP port open. Do not alter GOST
Manager firewall rules. Confirm the TCP-mode profile remains healthy and that
no Iran service PID, restart count, or Watchdog action changes. Restore ICMP and
verify no action was required.

## 7. Controlled test B: real TCP outage

In an approved Kharej maintenance window, stop only the selected remote GOST
port/service. Confirm that after the failure threshold only the matching Iran
profile stops; all other profiles remain active. Restore the remote endpoint.
After success threshold, hold, and jitter, confirm the Watchdog-owned Iran
service starts exactly once and no action loop occurs.

## 8. One-hour soak

Observe for at least one hour with reduced traffic or after RAM expansion:

```bash
watch -n 20 'gost-monitor snapshot'
gost-monitor summary --window 1h
gost-watchdog-admin events --limit 200
gost-watchdog-admin summary
```

Track host available/used memory, swap activity, load1/5/15, collector duration,
missed deadlines, overrun count, load-shedding state, database transaction
duration, DB/WAL growth, per-service RSS/FD count, TCP established, and reset
rate. Accept only when there is no GOST PID replacement, unexpected restart,
Watchdog action on ICMP-only failure, persistent `probe_error`, repeated
`collection_overrun`, sustained `MemAvailable` below 10%, unbounded DB/WAL
growth, or service action loop.

## 9. Rollback

Watchdog rollback is profile-local. Set the profile to Monitor Only or Disabled
as appropriate, do not clear manual ownership, and return its check to Ping:

```bash
sudo gost-watchdog-admin set-check-mode iran-1 ping
```

Do not restart a healthy GOST service as part of Watchdog rollback.

For Monitoring, restore the exact backup path printed by `apply-preset`, keep
mode `0600`, validate it, then restart only the collector:

```bash
sudo install -m 0600 /etc/gost-manager/monitoring.env.backup.TIMESTAMP \
  /etc/gost-manager/monitoring.env
sudo gost-monitor-admin validate-config
sudo systemctl restart gost-monitor-collector.service
```

Do not downgrade the manager unless that downgrade has a separately reviewed
and tested state/schema plan. Never remove or rewrite Monitoring/Watchdog
databases, traffic env files, or traffic units during rollback.
