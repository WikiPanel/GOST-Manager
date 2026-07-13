# Usage

Run the manager:

```bash
sudo gost-manager
```

Or run directly from the repository:

```bash
sudo bash gost-manager.sh
```

## Menu

```text
GOST Manager
============

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
0) Exit
```

## 1) Install / Update GOST

Installs `/usr/local/bin/gost` from official `go-gost/gost` GitHub Releases. Existing binaries are backed up before overwrite.

## 2) Create Kharej tunnel

Creates:

```text
/etc/gost/kharej-<number>.env
/etc/systemd/system/gost-kharej-<number>.service
```

The service starts a SOCKS5 listener:

```text
0.0.0.0:<TUNNEL_PORT>
```

Enable the firewall option unless another firewall already limits the port to the Iran server IP.

## 3) Create Iran tunnel

Creates:

```text
/etc/gost/iran-<number>.env
/etc/systemd/system/gost-iran-<number>.service
```

Mapping format:

```text
listen_port:kharej_local_target_port
```

Examples:

```text
2052:2052
80:80,8080:8080,8880:8880
```

`Port mappings` is required. Before writing files, the manager rejects empty mappings, invalid formats, non-numeric ports, ports outside `1..65535`, duplicate Iran listen ports, and busy listen ports.

If a listen port is busy, the manager prints the owning process from `ss -lntp` and aborts without writing env or service files.

## 4) Delete tunnel

Shows a numbered list of existing managed tunnels, including broken or orphaned service/env pairs. Select one item from the list; you do not type `iran` or `kharej` manually.

After confirmation, the manager stops and disables only the selected numbered service, removes its env and service file, reloads systemd, and removes matching Kharej firewall rules by comment.

## 5) Show status

Shows the same numbered tunnel selector, then runs `systemctl status` for the selected tunnel and prints related listening ports from `ss` when possible.

## 6) Show logs

Shows the numbered tunnel selector, then runs `journalctl -u <service> -n 100 --no-pager` for the selected tunnel.

## 7) Restart tunnel

Shows the numbered tunnel selector, restarts the selected service, and prints a short status view.

## 8) List active GOST services

Shows systemd units matching `gost-*` and summarizes managed env files in `/etc/gost`.

## 9) Clean old/broken GOST configs

Detects only managed files:

```text
/etc/systemd/system/gost-iran-*.service
/etc/systemd/system/gost-kharej-*.service
/etc/gost/iran-*.env
/etc/gost/kharej-*.env
```

It reports service/env mismatches, failed services, disabled orphan services, and old backup files. Nothing is deleted until you confirm.

## 10) Monitoring

Opens local optional Monitoring Lite with live, 10-minute, 30-minute, and
1-hour views for the host, network, TCP connections, Direct Mode GOST services,
tunnels, and collector. Monitoring has no traffic-path dependency and cannot
change a tunnel service.
