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

Before writing files, the manager validates mapping format, duplicate listen ports, and busy listen ports.

## 4) Delete tunnel

Stops and disables only the selected numbered service, removes its env and service file, reloads systemd, and removes matching Kharej firewall rules by comment.

## 5) Show status

Runs `systemctl status` for the selected tunnel and prints current GOST listening sockets from `ss`.

## 6) Show logs

Runs `journalctl -u <service> -n 100 --no-pager` for the selected tunnel.

## 7) Restart tunnel

Restarts the selected service and prints a short status view.

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
