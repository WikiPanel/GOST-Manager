# Security

## Env Permissions

Tunnel secrets are stored in:

```text
/etc/gost/*.env
```

The manager writes env files with permission `600` and keeps `/etc/gost` at permission `700`.

## Password Handling

Passwords must not be committed to GitHub. Keep real credentials only on the server in `/etc/gost/*.env`.

The systemd service uses `EnvironmentFile` so secrets are not embedded directly in the service unit file.

## Firewall Design

The Kharej service exposes a SOCKS5 listener. That listener must be restricted to the Iran server IP.

When enabled, the manager adds:

```text
allow: gost-manager:kharej-<number>:allow
drop:  gost-manager:kharej-<number>:drop
```

The comments make removal safe for a selected numbered tunnel.

## iptables Persistence

iptables rules are not persistent by default. After reboot, they may disappear unless saved with `netfilter-persistent` or managed by the server firewall platform.

## Why SOCKS Must Not Be Public

An unrestricted SOCKS5 port can be abused as an open proxy. Always limit it to the Iran server IP with the manager firewall option or an equivalent upstream firewall.

## Backup File Notes

Before overwriting managed env, service, or GOST binary files, the manager creates timestamped backups. These backups may contain secrets when they are env backups. Treat them like sensitive files and clean old backups with menu option `9` when they are no longer needed.
