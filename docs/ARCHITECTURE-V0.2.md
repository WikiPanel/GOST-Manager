# GOST Manager v0.2 Architecture

## Status

This document defines the target architecture for the v0.2 development series. It is a design contract, not a claim that every feature is already implemented.

## Product decisions

1. Existing Direct Mode remains supported and backward compatible.
2. NGINX Gateway Mode is the first production gateway implementation.
3. Native GOST Gateway appears in the menu as `Coming soon` and performs no runtime changes in v0.2.
4. Monitoring is a first-class subsystem, not an afterthought.
5. Multiple Iran gateways and multiple Kharej exits must be representable without a central runtime dependency.
6. Desired state, secrets, generated configuration, monitoring history, and backups are separate concerns.

## Data paths

### Existing Direct Mode

```text
Client
  ↓
Iran public GOST listener
  ↓ SOCKS5
Kharej GOST
  ↓
127.0.0.1:<target-port>
```

Existing numbered `gost-iran-*.service` and `gost-kharej-*.service` units remain valid.

### NGINX Gateway Mode

```text
Client / CDN
      ↓
Iran NGINX public port
      ↓ exact Host + Path routing
route upstream group
      ├── 127.0.0.1:<primary tunnel port>
      └── 127.0.0.1:<backup tunnel port>
      ↓
independent GOST Iran tunnel
      ↓ SOCKS5
Kharej GOST
      ↓
Xray inbound
```

NGINX owns the public port. Every gateway-mode GOST listener binds to loopback.

### Multiple Iran gateways

```text
CDN / load balancer
      ├── Iran Gateway A ──┬── Kharej 1
      │                    └── Kharej 2
      └── Iran Gateway B ──┬── Kharej 1
                           └── Kharej 2
```

Each Iran gateway is independently runnable. Synchronization distributes desired configuration but is never in the live traffic path.

## v0.2 menu contract

To avoid surprising existing users, the current menu numbers remain unchanged. New entries are appended:

```text
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
11) Native GOST Gateway (Coming soon)
0) Exit
```

Selecting Native GOST Gateway prints a clear `Coming soon` message and returns to the menu. It must not install packages or write files.

NGINX Gateway runtime and route CRUD are not part of the monitoring integration milestone and remain deferred.

## State layout

The current `/etc/gost` env files remain supported. New v0.2 state uses a separate managed tree:

```text
/etc/gost-manager/
├── state.json                 # non-secret desired state
├── secrets/                   # local credentials, mode 0700/0600
├── generated/                 # rendered NGINX and service files
├── backups/                   # bounded rollback copies
└── node.json                  # identity and local-only overrides

/var/lib/gost-manager/
└── metrics.sqlite3            # local monitoring history
```

JSON is used because Python 3 can validate it with the standard library. Generated files are never read back as authoritative desired state.

Gateway milestone 1/6 implements only the versioned `state.json` and
`node.json` desired-state layer. Its CLI performs locked, revision-aware,
atomic CRUD and validation without rendering or activating runtime
configuration. The exact implemented schema and commands are documented in
`docs/GATEWAY-STATE-V0.2.md`.

## State model

The initial schema contains these concepts:

### Gateway

- stable gateway ID;
- public listen port;
- one or more accepted server names;
- local status endpoint settings;
- enabled/disabled state.

### Route

- stable route ID;
- display name;
- exact Host;
- exact WebSocket Path;
- enabled/disabled state;
- upstream strategy;
- ordered tunnel membership;

The pair `Host + Path` must be globally unique among enabled routes on one public listener.

### Exit and local binding

- stable Exit ID;
- Kharej address, SOCKS port, and target port in shared state;
- Iran loopback binding fixed to `127.0.0.1` in node-local state;
- secret reference only, never a secret value;
- enabled/disabled state.

The current state-only validator keeps internal ports unique and separate from
the declared public and status ports. Live unmanaged-listener checks belong to
the runtime rendering milestone.

### Kharej allowlist

- a list of IPv4 addresses or CIDRs;
- migration support for legacy `IRAN_IP`;
- generated firewall rules scoped to one managed tunnel;
- all Iran gateway addresses required for that exit.

## Secrets

Credentials are local and excluded from exported shared route state by default. Exports may contain secret references, never secret values, unless the operator explicitly requests an encrypted future format.

Example separation:

```text
Shared:
  route IDs, Host, Path, tunnel IDs, endpoint addresses, roles

Local:
  SOCKS username/password, node-specific overrides
```

## NGINX generation

The manager owns only clearly named managed files, for example:

```text
/etc/nginx/conf.d/gost-manager-gateway.conf
/etc/nginx/snippets/gost-manager-websocket.conf
```

Generation lifecycle:

```text
load desired state
  ↓
validate schema and conflicts
  ↓
render to temporary directory
  ↓
run static validation
  ↓
install candidate managed files
  ↓
nginx -t
  ├── success → graceful reload and retain rollback copy
  └── failure → restore previous files and verify previous config
```

Ordinary route changes use reload, not restart.

The generated WebSocket proxy configuration must:

- use HTTP/1.1 upstream connections;
- pass Upgrade and Connection headers;
- disable proxy buffering and request buffering;
- use long read/send timeouts appropriate for persistent connections;
- preserve the original URI unless a route explicitly requests a rewrite;
- reject unmatched routes;
- expose NGINX basic status only on loopback.

## Route failover in NGINX Mode

A route can contain one or more local GOST tunnel backends:

```text
route-estonia
  ├── ee-primary  127.0.0.1:18081
  └── ee-backup   127.0.0.1:18082
```

NGINX may perform passive failover for new handshakes when a local backend connection or handshake fails. An established TCP/WebSocket connection cannot migrate; clients must reconnect.

## Local Exit runtime boundary

Before NGINX route rendering, each enabled Exit Binding is activated as one
independent `gost-gateway-exit-<exit-id>.service`. Its listener and target are
both loopback-only. Credentials are private runtime inputs referenced by stable
slug, while generated env, unit, and runtime manifest files remain non-secret
and non-authoritative. State/runtime operations acquire the state lock before
the runtime lock. Planning uses authoritative systemd `MainPID` ownership and a
bounded listener snapshot; apply restarts only Exits whose effective runtime
inputs changed and rolls back exact files plus enabled/active service states.

The full contract is in `docs/GATEWAY-RUNTIME-V0.2.md`. This layer has no NGINX,
firewall, route-rendering, failover-controller, or monitoring lifecycle hook.

The manager must distinguish:

- `active-active`: more than one normal upstream member;
- `active-passive`: one or more normal members plus backup members.

Active health management beyond open-source NGINX passive checks is a later hardening item. Monitoring may mark a route degraded without rewriting traffic configuration automatically in the first implementation.

## Synchronization and import/export

Exports are versioned, validated, non-secret desired-state documents. Import follows the same candidate/validation/rollback lifecycle as local edits.

Synchronization may be implemented with explicit operator actions such as export/copy/import. No gateway may require another gateway, GitHub, a database server, or a controller to continue serving its last valid configuration.

## Monitoring boundary

Monitoring is local to each node. It observes the host, NGINX, GOST services, listeners, routes, and selected network counters. It does not sit in the traffic path and it does not own traffic service lifecycle.

Detailed metric definitions are in `docs/MONITORING-V0.2.md`.

## Compatibility requirements

- Existing Direct Mode env files continue to load.
- Existing systemd service names continue to work.
- Delete and cleanup operations remain scoped to managed naming patterns.
- New state migration is explicit and reversible.
- Installation on Ubuntu 22.04 and 24.04 supports amd64 and arm64.
- Uninstall asks separately before deleting state, metrics history, NGINX managed files, GOST binary, or credentials.

## Planned delivery sequence

1. Foundation and repository guidance.
2. Monitoring collector, database, live view, and historical summaries.
3. Gateway state and route/tunnel CRUD.
4. Independent loopback GOST Exit runtime and private secrets.
5. Atomic NGINX renderer, validation, reload, and rollback.
6. Multiple Iran allowlist and firewall migration.
7. Route-aware monitoring, import/export, synchronization helpers, HA hardening, and release documentation.

Each milestone is delivered in a focused pull request with tests and a rollback plan.
