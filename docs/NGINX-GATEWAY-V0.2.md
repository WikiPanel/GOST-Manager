# Dedicated NGINX Gateway v0.2

## Architecture and boundary

NGINX Gateway Mode runs a dedicated NGINX instance owned by GOST Manager. It
uses the Ubuntu package binary at `/usr/sbin/nginx`, but does not include, edit,
reload, or depend on the distribution configuration under `/etc/nginx`.

```text
Client / CDN
  -> public Iran address and one HTTP port
  -> gost-nginx-gateway.service
  -> exact Host + exact Path
  -> 127.0.0.1:<binding-port>
  -> gost-gateway-exit-<exit-id>.service
  -> Kharej SOCKS
  -> remote 127.0.0.1:<target-port>
```

Normal Gateway plan, apply, status, test, and service operations never control
`nginx.service`. Existing websites and files under `/etc/nginx` remain outside
the managed boundary. Firewall and multi-Iran allowlists remain Issue #21 work.

## Production identity and paths

```text
Binary:       /usr/sbin/nginx
Service:      gost-nginx-gateway.service
CLI:          /usr/local/sbin/gost-gateway-nginx
Runner:       /usr/local/lib/gost-manager/gost-run-nginx-gateway.sh
Unit:         /etc/systemd/system/gost-nginx-gateway.service
Config:       /etc/gost-manager/generated/gateway/nginx/nginx.conf
Manifest:     /etc/gost-manager/generated/gateway/nginx/runtime.json
Backups:      /etc/gost-manager/backups/nginx-gateway/
Lock:         /run/gost-manager/nginx-gateway.lock
PID:          /run/gost-manager-nginx/nginx.pid
Status:       127.0.0.1:<status-port>/nginx_status
```

Config schema and manifest schema are both version 1. Generated files are
derived runtime output. `state.json` and `node.json` remain authoritative.

## Capacity defaults

The dedicated config and unit own fixed v0.2 defaults:

```text
worker_processes auto
worker_rlimit_nofile 200000
worker_connections 65535 per worker
multi_accept on
LimitNOFILE=200000
TasksMax=4096
```

Access logging is disabled by default. Error logging goes to stderr at warning
level. The config has no static root, cache, arbitrary include, resolver,
credential, Secret reference, or operator-controlled snippet.

## Exact routing

Each enabled Route creates one deterministic upstream and one exact location.
Routes are grouped under exact canonical Host server blocks. One managed
default server returns 404, and every Host block ends with an unmatched 404
location.

```nginx
server_name gateway.example.org;

location = /ee1/api/v1 {
    proxy_pass http://gmgw_route_<stable-hash>;
}
```

`/api/v1` and `/api/v1/` are distinct. Prefix and suffix variations return
404. Query parameters do not change route selection. Because `proxy_pass` has
no URI suffix, the original URI and query are preserved.

The NGINX runtime accepts only ASCII path characters `A-Z`, `a-z`, `0-9`, `/`,
`-`, `.`, `_`, and `~`. Percent escapes, duplicate slashes, dot segments,
queries, fragments, variables, semicolons, braces, parentheses, controls, and
non-ASCII input are rejected before generated files change.

## WebSocket proxy behavior

The upstream connection uses HTTP/1.1 and forwards the incoming Host, Upgrade,
Connection, client address, forwarded protocol/host/port, and original request
URI. Proxy buffering, request buffering, and cache are disabled. Connect
timeout is 3 seconds; read/send timeouts are one day for persistent sessions.

The Gateway is CDN-neutral. A conceptual reserved example is:

```text
gateway.example.org:80/api/v1
gateway.example.org:80/ee1/api/v1
gateway.example.org:80/us1/api/v1
```

The local binding ports do not appear in user links. VLESS or VMess clients use
their own non-production UUID and select the configured Host and Path; this
project does not publish or store that credential.

## Upstream strategies and failover

`active-active` renders all usable ordered Exits as normal upstream members
with `least_conn`. Backend line order remains deterministic.

`active-passive` renders the first usable ordered Exit as primary and all
remaining Exits with the open-source NGINX `backup` flag. Multiple backups form
one backup tier. This is not strict backup-1 then backup-2 sequencing.

Passive retry covers connection, timeout, invalid-header, and selected HTTP
403/404/5xx failures for a new handshake. Tries never exceed the included
backend count. An established TCP/WebSocket session never migrates. If its
backend disappears, that session ends naturally and the client reconnects; a
new handshake may reach another healthy backend.

## Backend readiness and listener ownership

Before rendering an enabled Route, every included local backend must have:

- an enabled Exit and enabled loopback Binding;
- a valid private Secret;
- exact current generated env and unit files;
- a matching GOST runtime manifest entry and Secret generation;
- loaded, enabled, active exact service state;
- positive authoritative cgroup PID membership;
- exact `127.0.0.1:<binding-port>` listener ownership by that PID set;
- no effective GOST runtime change pending.

One bounded `ss -H -lntp` snapshot is shared by readiness and port-conflict
checks. NGINX apply never starts, stops, restarts, reloads, or repairs a GOST
Exit service.

The dedicated NGINX service is multi-process. Its master PID is reported
separately, while listener ownership uses all authoritative PIDs in
`cgroup.procs`, including workers. A process named `nginx` is not sufficient
proof.

## Plan and apply

```bash
sudo gost-gateway-nginx dependency status
sudo gost-gateway-nginx plan
sudo gost-gateway-nginx apply --yes
sudo gost-gateway-nginx status
sudo gost-gateway-nginx test
```

The global lock order is always:

1. Gateway desired-state lock;
2. GOST runtime/Secret lock;
3. NGINX Gateway lock.

Plan is read-only apart from private advisory lock-file use. It reports one of
`dependency-missing`, `conflict`, `create`, `start`, `reload`,
`metadata-update`, `stop-remove`, or `no-op`.

First activation privately renders and tests the candidate, atomically installs
the config, tests the installed bytes, enables/starts only the dedicated
service, proves cgroup listener ownership and loopback status, and writes the
manifest last.

An ordinary effective config change runs exactly one graceful reload. NGINX is
tested before the signal. The master PID must remain unchanged. Existing
WebSockets remain on old workers until they close; new connections use the new
configuration. No ordinary action runs restart.

A display-name, revision, or other non-effective change updates only manifest
metadata. Complete no-op apply performs no lifecycle command, file rewrite,
backup, or signal.

Disabling the Gateway warns about disconnection, stops/disables only the
dedicated service, and removes only the managed NGINX config and manifest.
Desired state, Exits, Bindings, Secrets, GOST services, Monitoring Lite,
firewall, and `/etc/nginx` remain.

## Atomic rollback

Every mutating apply snapshots exact managed bytes and prior service state in a
private non-secret transaction backup. Candidate or installed `nginx -t`,
reload, listener ownership, status probe, manifest write, fsync, or final
verification failure restores exact previous files and enabled/active state.

For failed reload, the previous config is restored and tested, then gracefully
reloaded and reverified. A backup is removed only after service, listeners,
status, bytes, and fsync are proven. If recovery cannot be proven, the existing
backup path is retained and reported.

## Dependency installation

Normal manager installation does not install NGINX. Explicit opt-in is:

```bash
sudo gost-gateway-nginx dependency install --yes
```

It uses only Ubuntu/Debian `apt-get` and package `nginx`. If the fixed binary
already exists, the action is a no-op and never changes `nginx.service`. If a
new package installation auto-starts its previously absent distro service, the
dependency workflow stops and disables only that newly created service. It does
not apply Gateway config or start the dedicated service.

## Service control

```bash
sudo gost-gateway-nginx service status
sudo gost-gateway-nginx service start
sudo gost-gateway-nginx service stop --yes
sudo gost-gateway-nginx service reload --yes
sudo gost-gateway-nginx service restart --yes --acknowledge-disconnect
```

Start/restart revalidate desired state, backend readiness, installed bytes, and
port safety. Restart is never used for normal config changes and explicitly
acknowledges that established connections may disconnect.

## Installation, menu, and removal

Manager install adds the CLI, runner, Python modules, static unit, and private
directories. It does not install the package, generate config, initialize
state, open a port, or start either NGINX service.

Main menu option 11 opens the NGINX Gateway workflow. Option 12 is Native GOST
Gateway and remains a print-only `Coming soon` no-op.

The uninstaller has an independent default-No NGINX Gateway choice. It removes
only the dedicated unit, managed config/manifest, and selected managed backups.
It never uninstalls the Ubuntu NGINX package or removes `/etc/nginx`. Remove the
package manually only after confirming that no other site or application uses
it.

## Security and compatibility

Generated config, manifest, backups, output, and errors contain no GOST
username/password or Secret reference. Direct Mode files under `/etc/gost`,
unmanaged NGINX, Monitoring Lite, and firewall rules remain isolated. Native
GOST runtime, TLS, HTTP/2/3, firewall allowlists, synchronization, active health
agents, and automatic route rewriting are outside this milestone.
