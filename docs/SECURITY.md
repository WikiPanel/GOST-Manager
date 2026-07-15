# Security

## Release Installation Trust

The public `setup.sh` entrypoint downloads only HTTPS assets from official
`WikiPanel/GOST-Manager` GitHub Releases. It saves both the release archive and
its SHA256 record to a private temporary directory, validates the checksum
record, verifies the archive with `sha256sum`, rejects unsafe archive members,
atomically activates the verified source at `/opt/GOST-Manager`, and only then
runs that source's local `install.sh`. The installer verifies the installed
VERSION and executable before committing. Setup retains and prints exact
private recovery paths if rollback or prior-source backup cleanup cannot be
verified. It does not stream an unverified archive into a shell or tar process.
Pinned versions never fall back silently to latest.

The verified source copy at `/opt/GOST-Manager` is replaceable application
content. Tunnel credentials, operator monitoring configuration, monitoring
history, Direct Mode units/drop-ins, and stability configuration remain in
their separate preserved paths.

## Env Permissions

Tunnel secrets are stored in:

```text
/etc/gost/*.env
```

The manager writes env files with permission `600` and keeps `/etc/gost` at permission `700`.

## Password Handling

Passwords must not be committed to GitHub. Keep real credentials only on the server in `/etc/gost/*.env`.

The systemd service uses `EnvironmentFile` so secrets are not embedded directly in the service unit file.

The monitoring collector parses only the Direct Mode fields needed for topology and listener checks. It may store a remote `host:port` endpoint, but it never stores env usernames, passwords, tokens, or other credentials in metrics, events, entity metadata, collector state, or exports.

The Upstream Watchdog parses only `KHAREJ_IP` from exact managed Iran env files.
It does not load credential fields. Its argv-based Ping executor never invokes
a shell, its systemd controller accepts only exact `gost-iran-N.service` names,
and its dedicated 24-hour event store has fixed safe columns. Config/state
paths are private and reject symlinks.

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
