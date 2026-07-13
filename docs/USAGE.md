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
11) Server Stability
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

The manager suggests the first free Kharej number, accepts an optional safe
label, requires a hidden confirmed password, and accepts one to 64 comma-
separated IPv4/CIDR sources. New candidates store canonical sources in
`ALLOWED_IRAN_SOURCES`; legacy `IRAN_IP` remains supported. Enable the
profile-scoped firewall unless another firewall already limits the port.

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

`Port mappings` is required. The first free Iran number is suggested and an
optional label may be supplied. Before writing files, the manager rejects
empty mappings, invalid formats, non-numeric ports, ports outside `1..65535`,
duplicate Iran listen ports, cross-profile configured conflicts, and busy live
listeners. Password input is hidden and confirmed.

If a listen port is busy, the manager prints a bounded ownership summary from
one `ss -H -lntp` snapshot and aborts without writing env or service files.

## 4) Delete tunnel

Shows a numbered list of existing managed tunnels, including broken or orphaned service/env pairs. Select one item from the list; you do not type `iran` or `kharej` manually.

After confirmation, the manager stops and disables only the selected numbered service. A stop/disable failure preserves its env, unit, and firewall. Firewall rules are removed only after a successful stop; exact files are then removed and systemd is reloaded only when the unit changed.

## 5) Show status

Shows the same numbered tunnel selector, then prints only limited non-command
properties from `systemctl show` and the selected profile's listeners from one
bounded socket snapshot. Raw command lines and credentials are not displayed.

## 6) Show logs

Shows the numbered tunnel selector, then runs `journalctl -u <service> -n 100 --no-pager` for the selected tunnel.

## 7) Restart tunnel

Shows the numbered tunnel selector, restarts the selected service, and prints
the same credential-safe status properties.

## 8) List active GOST services

Shows one numeric, deterministic row per Iran then Kharej profile, including
label fallback, exact service state, local ports, safe endpoint/source count,
firewall state, env/unit presence, and established socket count. Socket count
is `unknown`, never an invented zero, when ownership cannot be proven. One
bounded socket snapshot serves the complete list.

It then opens the Direct Mode profile submenu with list, detail, edit, clone,
restart-selected, and restart-all actions. Profile identity is immutable.
Edit preserves credentials and unknown well-formed env keys, uses a redacted
diff and rollback, and reports `restart required` when restart is declined.
Clone creates a new same-side number and never changes its source. Restart
selection accepts exact IDs such as `iran-1,iran-3,kharej-2`; wildcard service
commands are never used.

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

## 11) Server Stability

Runs the complete host-stability check, applies the managed sysctl profile,
and verifies its live values. Exact existing numbered GOST services receive a
separate `stability.conf` drop-in with the recommended file, task, OOM, and
restart limits. The wizard runs `daemon-reload` only when a drop-in changes;
it never restarts a service or reads an env file. The final report lists any
services whose new limits require a later operator-scheduled restart.
