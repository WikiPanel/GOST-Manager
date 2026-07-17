# Operations Runbook

## Fresh Server Setup

1. Use Ubuntu 22.04 or Ubuntu 24.04.
2. Run the checksum-verified public setup from a root shell:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/WikiPanel/GOST-Manager/main/setup.sh)
```

3. Confirm the version and start the menu:

```bash
gost-manager --version
gost-manager
```

4. Choose `1) Install / Update GOST`.

Rerun the same setup command for an upgrade. Use
`GOST_MANAGER_VERSION=v2.0.0` before the command when an exact release must be
pinned. The verified release source remains at `/opt/GOST-Manager`; manual Git
clone plus `bash install.sh --install-dependencies` remains the offline/local
fallback.

## Kharej Setup

On the Kharej server:

```bash
sudo gost-manager
```

Choose `2) Create Kharej tunnel`.

Recommended values:

```text
Kharej profile number [1]:
Profile label (optional): kharej-edge
SOCKS listen port: 28420
GOST username: maya
GOST password: hidden explicit value plus confirmation
Allowed Iran IPv4/CIDRs: 198.51.100.10,198.51.100.11/32
Apply profile-scoped iptables firewall rules? yes
```

Use the same password on the matching Iran profile. The manager never prints
it after input; store it in an approved secret manager, never Git.

## Iran Setup

On the Iran server:

```bash
sudo gost-manager
```

Choose `3) Create Iran tunnel`.

Single-port example:

```text
Iran profile number [1]:
Profile label (optional): iran-edge
Kharej IP: 203.0.113.20
Kharej SOCKS port: 28420
GOST username: maya
GOST password: value_from_kharej
Port mappings: 2052:2052
```

The `Port mappings` prompt is required. Use `Iran listen port:Kharej local target port`.

Multi-port example:

```text
Iran profile number [2]:
Profile label (optional): iran-web
Kharej IP: 203.0.113.20
Kharej SOCKS port: 28420
GOST username: maya
GOST password: value_from_kharej
Port mappings: 80:80,8080:8080,8880:8880
```

## Local Test

On the Iran server:

```bash
curl -v --max-time 10 http://127.0.0.1:2052/
curl -v --max-time 10 http://127.0.0.1:80/
curl -v --max-time 10 http://127.0.0.1:8080/
curl -v --max-time 10 http://127.0.0.1:8880/
```

Use only the ports you mapped.

## Public/CDN Test

From a client or CDN edge path:

```bash
curl -v --max-time 10 http://YOUR_DOMAIN_OR_IP:2052/
curl -v --max-time 10 http://YOUR_DOMAIN_OR_IP:80/
```

## Restart

Use menu option `7) Restart tunnel`. The manager shows a numbered selector, so choose the tunnel from the list instead of typing `iran` or `kharej`.

```text
Available GOST tunnels:

1) gost-iran-1.service      active/running    /etc/gost/iran-1.env
2) gost-kharej-1.service    active/running    /etc/gost/kharej-1.env

Select tunnel number:
```

For several exact profiles, option `8` opens the profile submenu. Choose
`Restart selected profiles` and enter IDs such as
`iran-1,iran-2,kharej-1`. Review the exact service list before confirming.
Use `Restart all profiles` only when every independent Direct Mode process may
be interrupted.

The same submenu provides safe detail, edit, and clone actions. A blank edit
password retains the secret, a no-op writes nothing, and declining restart
prints `restart required`. Clone requires new conflict-free local ports and
never changes the source profile.

## Server Stability

Choose `11) Server Stability` to inspect and apply the bounded host stability
profile. The wizard shows the current and recommended kernel values, manages
`/etc/sysctl.d/99-gost-stability.conf`, applies kernel changes with
`sysctl --system`, and verifies every key.

For every exact numbered Iran/Kharej unit it manages only the separate
`stability.conf` drop-in. It does not change the original unit or env file and
does not restart GOST. Review the final `Restart required` list and schedule a
normal service restart during an approved maintenance window if the new
process limits must take effect immediately.

Running the wizard again is safe. When all live values and managed files are
already correct, it performs no file replacement, sysctl apply, or daemon
reload. If it reports a symlink or unmanaged-file conflict, inspect that exact
path manually; the wizard will not overwrite it.

## Upstream Watchdog

Choose `12) Upstream Watchdog`. Confirm every profile is `Disabled` after
install, then follow the Monitor Only canary and controlled rollout in
`docs/WATCHDOG-V1.md`. Auto Protect uses the exact 2-second default interval,
stops one affected Iran profile only after 10 consecutive failures, and starts
only a verified Watchdog-owned stop after recovery qualification.

Use maintenance before an operator-planned service action. A manual state
mismatch suspends automatic actions until `Re-arm manual override`; do not
re-arm until the operator's intended service state is confirmed. History and
the outage summary cover the last 24 hours only.

## Recovery If Service Fails

1. Use menu option `5) Show status` and select the tunnel from the numbered list.
2. Use menu option `6) Show logs` and select the tunnel from the numbered list.
3. Verify `/etc/gost/<side>-<number>.env` exists and is permission `600`.
4. Verify `/usr/local/bin/gost -V` works.
5. On Iran, check whether public listen ports are already owned by another process.
6. On Kharej, check whether the profile-scoped firewall rules allow every
   canonical Iran source before the final managed DROP.
7. Use menu option `9) Clean old/broken GOST configs` only after reviewing the candidate list.
