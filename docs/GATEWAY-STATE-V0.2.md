# Gateway Desired State v0.2

## Scope

This document describes the state-only foundation delivered by Gateway milestone
1/6. It can create, validate, inspect, and safely edit desired state for one Iran
NGINX gateway, multiple Kharej exits, local loopback bindings, and Host + Path
routes.

The package does not install, render, start, stop, restart, or reload NGINX,
GOST, systemd, or firewall configuration. It does not alter Direct Mode or the
monitoring runtime. A state document that passes runtime-readiness validation is
still only desired state; no traffic changes occur.

The implementation uses only the Python 3 standard library and is available as:

```bash
python3 -m gateway.cli --help
```

The same state CLI is now consumed by the option 11 NGINX Gateway menu; Bash
does not write either JSON document directly.

## State separation

Gateway state is split into two independently revisioned documents:

```text
/etc/gost-manager/state.json
  Shared, non-secret gateway, exit, and route desired state

/etc/gost-manager/node.json
  Node identity and local loopback bindings with secret references
```

Both documents carry the same `document_id`. Shared and node revisions advance
independently. Secret values are not represented in either schema. A
`secret_ref` is only a stable identifier for the later secret-management
milestone.

Default supporting paths are:

```text
/etc/gost-manager/backups/gateway/
/run/gost-manager/gateway-state.lock
```

Tests and development commands must override all four paths to a dedicated
temporary directory. Paths must be absolute and normalized and may not traverse
symlinks.

## Shared schema version 1

The shared document has exactly these top-level fields:

```json
{
  "schema_version": 1,
  "document_id": "00000000-0000-4000-8000-000000000001",
  "revision": 1,
  "updated_at": "2026-07-12T00:00:00Z",
  "gateway": {
    "id": "gateway-main",
    "enabled": false,
    "listen_address": "0.0.0.0",
    "listen_port": 80,
    "server_names": [
      "gateway.example.org"
    ],
    "status_port": 18000
  },
  "exits": [
    {
      "id": "ee-primary",
      "display_name": "Estonia primary",
      "enabled": true,
      "host": "192.0.2.10",
      "socks_port": 28420,
      "target_port": 18081
    }
  ],
  "routes": [
    {
      "id": "route-estonia",
      "display_name": "Estonia",
      "enabled": false,
      "host": "gateway.example.org",
      "path": "/ee1/api/v1",
      "strategy": "active-passive",
      "exit_ids": [
        "ee-primary"
      ]
    }
  ]
}
```

The shared document is limited to 1 MiB, 256 exits, and 256 routes.

### Gateway fields

- `id` is an immutable lowercase slug matching
  `^[a-z][a-z0-9-]{0,62}$`.
- `enabled` is a strict JSON boolean. Initialization sets it to `false`.
- `listen_address` is canonical IPv4. IPv6, CIDR, hostnames, and embedded ports
  are rejected.
- `listen_port` is an integer from 1 through 65535.
- `server_names` contains 1 through 32 canonical lowercase exact DNS names or
  IPv4 literals. Scheme, path, port, wildcard, whitespace, trailing dot, and
  canonical duplicates are rejected. Input order is preserved.
- `status_port` is an integer from 1024 through 65535 and must differ from the
  public port and all binding ports. Its conceptual address is fixed to
  `127.0.0.1`.

### Exit fields

- `id` is an immutable globally unique lowercase slug.
- `display_name` is a trimmed non-empty Unicode string of at most 100
  characters with no control characters.
- `enabled` is a strict boolean and defaults to `true` for `exit add`.
- `host` is canonical IPv4 or lowercase DNS without a scheme, path, port,
  wildcard, credentials, whitespace, or trailing dot.
- `socks_port` and `target_port` are integers from 1 through 65535.

Username, password, token, secret, credential, and authorization fields are not
part of the schema.

### Route fields

- `id` is an immutable globally unique lowercase slug.
- `display_name` follows the Exit display-name rules.
- `enabled` is a strict boolean and defaults to `false` for `route add`.
- `host` is canonical and must be present in `gateway.server_names`.
- `path` is preserved exactly, begins with `/`, and is at most 512 UTF-8 bytes.
  Whitespace, controls, query strings, fragments, backslashes, quotes, and
  malformed percent escapes are rejected. `/api/v1` and `/api/v1/` are
  intentionally distinct.
- `strategy` is exactly `active-passive` or `active-active`.
- `exit_ids` is an ordered list of 1 through 32 unique existing Exit IDs.

Among enabled routes, canonical `Host + Path` is unique. Disabled conflicting
routes may be prepared, but enabling a conflict is refused and identifies both
safe route IDs.

For `active-passive`, the first usable enabled Exit is primary and later usable
Exits are backups. For `active-active`, all usable enabled Exits are ordinary
members. This milestone validates and stores ordering only; it does not render
an upstream configuration.

## Node schema version 1

The node-local document has exactly these fields:

```json
{
  "schema_version": 1,
  "document_id": "00000000-0000-4000-8000-000000000001",
  "node_id": "iran-gateway-1",
  "revision": 1,
  "updated_at": "2026-07-12T00:00:00Z",
  "bindings": [
    {
      "exit_id": "ee-primary",
      "enabled": true,
      "listen_address": "127.0.0.1",
      "listen_port": 18081,
      "secret_ref": "secret-ee-primary"
    }
  ]
}
```

The node document is limited to 512 KiB and 256 bindings.

- `node_id` is an immutable lowercase slug.
- `exit_id` references exactly one shared Exit and is unique among bindings.
- `enabled` is a strict boolean.
- `listen_address` is fixed to `127.0.0.1` and cannot be set through the CLI.
- `listen_port` is a unique integer from 1024 through 65535 and cannot equal
  the gateway public or status port.
- `secret_ref` is empty only for a disabled binding. Otherwise it is a required
  lowercase slug of at most 64 characters. It never contains a secret value.

## Validation levels

Structural validation reads both documents under the gateway-state lock and
checks schemas, strict field sets, duplicate JSON keys, UUID and revision
fields, canonical values, entity limits, global IDs, references, ports, enabled
route backends, and Host + Path conflicts:

```bash
python3 -m gateway.cli validate
```

Runtime-readiness adds these requirements:

- the Gateway is enabled;
- at least one Route is enabled;
- each enabled Route has at least one enabled Exit with an enabled local
  Binding and non-empty `secret_ref`;
- all listener ports remain conflict-free.

```bash
python3 -m gateway.cli validate --runtime-ready
```

Neither validation level reads secret values, probes live ports, invokes a
service manager, calls NGINX, or changes traffic.

## Revision control

Every shared mutation compares and advances only the shared revision. Binding
mutations compare and advance only the node revision. Successful no-op edits do
not advance a revision or create a backup.

Mutating CRUD commands accept optional optimistic concurrency control:

```bash
python3 -m gateway.cli gateway set --enable --expect-revision 4
python3 -m gateway.cli binding set \
  --exit-id ee-primary \
  --listen-port 18081 \
  --secret-ref secret-ee-primary \
  --enable \
  --expect-revision 2
```

A mismatch returns conflict exit code 4 and leaves both documents unchanged.
All writers re-read the current pair while holding the same advisory lock, even
when no expected revision is supplied.

## Atomic writes and backups

Mutations use a bounded exclusive `fcntl.flock`. The default timeout is five
seconds. A stale, unheld lock file is reusable. Lock files are mode `0600`.

For a changed document, the store:

1. Validates a candidate in memory.
2. Acquires the gateway-state lock and re-reads both current documents.
3. Verifies the optional expected revision and re-applies validation.
4. Serializes deterministic UTF-8 JSON with a final newline.
5. Creates a collision-resistant same-directory temporary file at mode `0600`.
6. Writes all bytes, flushes, and fsyncs the temporary file.
7. Creates a private backup of the previous valid revision.
8. Uses `os.replace` and fsyncs the parent directory.
9. Reopens and validates the installed pair.
10. Retains at most ten managed backups per document.

Backup names contain the document type, prior revision, and a random suffix.
Only exact managed backup names are pruned. Symlinks are never followed and
unmanaged files are not recursively removed. Any failed mutation restores and
revalidates the previous active state.

`init` creates both documents with one UUID and one timestamp. If the second
write or final pair validation fails, neither document remains. There is no
force-overwrite option.

## CLI commands

Global options may be placed before or after commands:

```text
--state-file PATH
--node-file PATH
--backup-dir PATH
--lock-file PATH
--format human|json
--debug
```

Available commands are:

```text
init
show
validate [--runtime-ready]
gateway show
gateway set
exit add | edit | delete | list
binding set | remove | list
route add | edit | delete | list
```

Example state-only sequence:

```bash
python3 -m gateway.cli init \
  --gateway-id gateway-main \
  --node-id iran-gateway-1 \
  --listen-address 0.0.0.0 \
  --listen-port 80 \
  --server-name gateway.example.org

python3 -m gateway.cli exit add \
  --id ee-primary \
  --display-name "Estonia primary" \
  --host 192.0.2.10 \
  --socks-port 28420 \
  --target-port 18081

python3 -m gateway.cli binding set \
  --exit-id ee-primary \
  --listen-port 18081 \
  --secret-ref secret-ee-primary \
  --enable

python3 -m gateway.cli route add \
  --id route-estonia \
  --display-name Estonia \
  --host gateway.example.org \
  --path /ee1/api/v1 \
  --strategy active-passive \
  --exit-id ee-primary
```

The stable exit-code contract is:

```text
0  success
1  unexpected operational failure
2  invalid input or validation failure
3  missing, corrupt, or unsupported state
4  lock, revision, dependency, or mutation conflict
```

Human output is the default. JSON output is stable and suitable for operators
and later integration work. Errors never echo raw unknown JSON or unsafe raw
argument values.

## State-layer boundary

This state layer itself does not create or resolve secret values, install
NGINX, render NGINX or GOST files, run `nginx -t`, reload services, edit
firewall rules, monitor routes, synchronize nodes, or import/export state.
Issue #19 and Issue #20 consume the validated documents through separate
runtime layers. Generated files remain derived output, and runtime activation
retains independent validation and rollback.
