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
```

Examples:

```text
Tunnel side: iran
Tunnel number: 1
```

```text
Tunnel side: kharej
Tunnel number: 1
```

Each numbered tunnel is independent. Deleting `iran-2` does not affect `iran-1`; deleting `kharej-2` does not affect `kharej-1`.

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
