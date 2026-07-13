# Gateway Local Exit Runtime v0.2

## Milestone boundary

This milestone activates only independent local GOST Exit processes on the Iran
gateway. It does not install or configure NGINX, create a public listener,
change firewall rules, render Host + Path routes, synchronize nodes, or expose a
Gateway workflow in the main menu. Native GOST Gateway remains `Coming soon`.

Existing Direct Mode files and services remain outside this runtime's managed
names and paths.

## Data path and service identity

Each enabled Exit with an enabled loopback Binding and valid secret has one
independent process:

```text
127.0.0.1:<binding-port>
  -> gost-gateway-exit-<exit-id>.service
  -> SOCKS5 <exit-host>:<socks-port>
  -> 127.0.0.1:<target-port> on Kharej
```

The exact service pattern is
`gost-gateway-exit-[a-z][a-z0-9-]{0,62}.service`. One Exit failing does not
stop or restart another Exit. The generated unit binds the traffic listener and
target to `127.0.0.1`, uses `LimitNOFILE=200000`, and has no NGINX, Direct Mode,
Monitoring, or `PrivateNetwork` dependency.

## Private secrets

Desired state contains only a validated `secret_ref`. Credentials are stored
separately at `/etc/gost-manager/secrets/<secret-ref>.env`, with directory mode
`0700` and file mode `0600`:

```text
GOST_USER=<URI-unreserved value>
GOST_PASS=<URI-unreserved value>
```

The file is UTF-8, exactly two records in that order, ends with one newline, and
is at most 1 KiB. Username length is 1-128 and password length is 1-256. Values
may contain only ASCII letters, digits, `.`, `_`, `~`, and `-`. Shell quoting,
interpolation, comments, whitespace, duplicate or unknown keys, symlinks, and
hard links are rejected.

Secrets are accepted only by hidden interactive prompts or bounded strict JSON
on stdin:

```bash
sudo gost-gateway-runtime secret set --ref secret-ee-primary
printf '%s' '{"username":"operator","password":"generated-value"}' | \
  sudo gost-gateway-runtime secret set --ref secret-ee-primary --stdin-json
```

Do not put credentials in command arguments. Secret values never enter state,
generated env/unit/manifest files, output, errors, logs, persistent backups, or
PR descriptions. Updating a secret reports affected Exit IDs and whether an
explicit restart is required; it never restarts a service automatically.

## Production paths

```text
/etc/gost-manager/state.json
/etc/gost-manager/node.json
/run/gost-manager/gateway-state.lock
/etc/gost-manager/secrets/<secret-ref>.env
/etc/gost-manager/generated/gateway/exits/<exit-id>.env
/etc/gost-manager/generated/gateway/runtime.json
/etc/systemd/system/gost-gateway-exit-<exit-id>.service
/etc/gost-manager/backups/gateway-runtime/
/run/gost-manager/gateway-runtime.lock
/usr/local/sbin/gost-gateway
/usr/local/sbin/gost-gateway-runtime
/usr/local/lib/gost-manager/gateway/
/usr/local/lib/gost-manager/gost-run-gateway-exit.sh
/usr/local/bin/gost
```

Generated Exit env files contain only the Exit ID, loopback addresses, listen
port, canonical Exit host, SOCKS port, and target port. `runtime.json` is
non-authoritative status metadata with revision numbers, paths, non-secret
content hashes, and secret file `mtime_ns`; it never contains credential bytes
or a credential hash.

## Plan, apply, and conflicts

```bash
sudo gost-gateway-runtime runtime plan
sudo gost-gateway-runtime runtime apply --yes
sudo gost-gateway-runtime runtime status
```

Use `--exit-id <id>` to isolate one Exit. A full apply also reconciles stale
exact managed services; a selected apply never changes unrelated services.
Plan is read-only and reports `create`, `update`, `start`, `restart`, `stop`,
`remove`, `no-op`, or `conflict` with non-secret reasons.

One bounded `ss -H -lntp` snapshot is used for preflight. A port is rejected if
it is owned by an unmanaged process, Direct Mode, NGINX, another Exit, a
wildcard listener, or an unknown owner. An occupied loopback port is accepted
only when its exact PID is the authoritative `MainPID` of the same active Exit
service.

Unchanged active services are not restarted. A restart occurs only when an
effective input changes: endpoint, listen/target port, secret reference or
secret mtime, generated env, or unit content. Shared/node revision changes and
unrelated route or display-name edits do not restart traffic.

Established connections on a restarted Exit reconnect; they do not migrate to
another process automatically.

## Service control and secret rotation

```bash
sudo gost-gateway-runtime service status --exit-id ee-primary
sudo gost-gateway-runtime service start --exit-id ee-primary
sudo gost-gateway-runtime service stop --exit-id ee-primary --yes
sudo gost-gateway-runtime service restart --exit-id ee-primary --yes
```

Start and restart validate exact generated material, the referenced secret, and
port ownership. Stop does not disable desired state, so a later full apply may
start the service again.

To rotate a secret, run `secret set`, inspect `runtime plan`, then explicitly
restart each reported Exit. This keeps rotation visible and prevents an
unexpected interruption of persistent connections.

## Locks and rollback

State CRUD uses only the state lock. Secret writes and service control use only
the runtime lock. Secret deletion, plan, and apply acquire locks in this order:

1. `/run/gost-manager/gateway-state.lock`
2. `/run/gost-manager/gateway-runtime.lock`

Both locks have private parents, mode `0600`, bounded acquisition, stale
unheld-file reuse, and release on normal or exceptional exit.

Apply validates desired bindings, secrets, listener ownership, service state,
and rendered bytes before activation. It snapshots exact prior files and
enabled/active states. An activation failure stops only newly affected Gateway
services, restores env/unit/manifest bytes, reloads systemd if needed, restores
the previous service states, and leaves unrelated Exit, Direct Mode, NGINX,
Monitoring, and firewall state unchanged.

## Installation and removal

The installer uses `packaging/gateway-runtime-manifest.txt` as an exact package
allowlist. It installs the two launchers, Python package, and runner, and creates
private secret/generated/backup directories. It does not initialize desired
state, create secrets or units, start services, install NGINX, or alter Direct
Mode or firewall state. Existing package upgrades participate in atomic
installer rollback.

The uninstaller asks separately, defaulting to No, for runtime/generated files,
desired state/backups, secrets, and package/launchers/runner. Secret removal
requires `DELETE GATEWAY SECRETS`. Package and runner removal is refused while
an exact Gateway Exit service remains. A service-removal failure preserves its
unit, env, secret, runner, and package. No option implicitly deletes another
category.

## Next milestone

Issue #20 will render and validate the public NGINX Gateway and connect Host +
Path routes to these loopback Exit services. NGINX and firewall runtime remain
intentionally unimplemented here.

## Dedicated NGINX Gateway runtime (Issue #20)

The public Gateway HTTP data plane is rendered into a dedicated NGINX instance
owned by `gost-nginx-gateway.service`. The renderer writes only under
`/etc/gost-manager/generated/gateway/nginx/` and never edits `/etc/nginx` or the
distribution `nginx.service` configuration. The unit uses `/usr/sbin/nginx`,
validates the generated config with `nginx -t`, and reloads the dedicated master
with `HUP` for ordinary route changes so established upgraded connections remain
owned by the old worker until they drain.

Routing is exact Host plus exact Path only. Unknown hosts are rejected by the
default server and unknown paths return `404`. Route locations proxy with
HTTP/1.1 WebSocket upgrade headers, preserve the original `Host`, and pass
`$request_uri` so the original URI and query string reach the route-specific
loopback upstream. Active-active routes render all loopback backends in the same
upstream tier. Active-passive routes render the first backend as primary and all
remaining backends as NGINX `backup` servers for passive new-handshake failover.
A loopback-only `/nginx_status` listener is generated for local status checks.

The NGINX layer treats GOST Exit services as independently managed backends. It
does not start, stop, restart, or reload Direct Mode services, Monitoring Lite,
firewall rules, or `gost-gateway-exit-<exit-id>.service` units. Runtime metadata
is a strict non-secret manifest containing hashes and counts only; generated
configuration never includes Secret values or credential-derived hashes.
