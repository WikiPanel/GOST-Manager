"""Deterministic self-contained NGINX Gateway configuration rendering."""

from __future__ import annotations

import hashlib
import re

from gateway.errors import ValidationError
from gateway.models import StatePair
from gateway.nginx_models import (
    MAX_NGINX_BACKENDS,
    MAX_NGINX_CONFIG_BYTES,
    MAX_NGINX_ROUTES,
    NGINX_CONFIG_SCHEMA_VERSION,
    NginxBackend,
    NginxCandidate,
    NginxRoute,
)
from gateway.validation import validate_pair, validate_slug


NGINX_PATH_RE = re.compile(r"^/[A-Za-z0-9/._~-]*$")


def validate_nginx_path(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValidationError("NGINX route path must start with /")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValidationError("NGINX route path must be ASCII") from exc
    if len(encoded) > 512 or not NGINX_PATH_RE.fullmatch(value):
        raise ValidationError("NGINX route path contains an unsupported character")
    if "%" in value:
        raise ValidationError("NGINX route path must not contain percent escapes")
    if "//" in value:
        raise ValidationError("NGINX route path must not contain duplicate slashes")
    segments = value.split("/")[1:]
    if any(item in {".", ".."} for item in segments):
        raise ValidationError("NGINX route path must not contain dot segments")
    return value


def upstream_name(route_id: str) -> str:
    identifier = validate_slug(route_id, "route ID")
    digest = hashlib.sha256(identifier.encode("ascii")).hexdigest()[:20]
    return f"gmgw_route_{digest}"


def build_candidate(
    pair: StatePair,
    ready_ports: dict[str, int],
) -> NginxCandidate:
    validate_pair(pair, runtime_ready=True)
    enabled_routes = tuple(item for item in pair.shared.routes if item.enabled)
    if not 1 <= len(enabled_routes) <= MAX_NGINX_ROUTES:
        raise ValidationError("enabled NGINX routes exceed the supported limit")
    exits = {item.id: item for item in pair.shared.exits}
    bindings = {item.exit_id: item for item in pair.node.bindings}
    routes: list[NginxRoute] = []
    for route in sorted(enabled_routes, key=lambda item: (item.host, item.path, item.id)):
        validate_nginx_path(route.path)
        members: list[NginxBackend] = []
        for exit_id in route.exit_ids:
            exit_node = exits[exit_id]
            binding = bindings.get(exit_id)
            if (
                not exit_node.enabled
                or binding is None
                or not binding.enabled
                or not binding.secret_ref
            ):
                continue
            port = ready_ports.get(exit_id)
            if port is None or port != binding.listen_port:
                raise ValidationError(
                    f"enabled route {route.id} backend {exit_id} is not runtime-ready"
                )
            members.append(NginxBackend(exit_id, "127.0.0.1", port))
        if not 1 <= len(members) <= MAX_NGINX_BACKENDS:
            raise ValidationError(f"enabled route {route.id} has no ready backend")
        routes.append(
            NginxRoute(
                route.id,
                route.host,
                route.path,
                route.strategy.value,
                upstream_name(route.id),
                tuple(members),
            )
        )
    gateway = pair.shared.gateway
    return NginxCandidate(
        pair.shared.document_id,
        pair.shared.revision,
        pair.node.revision,
        gateway.id,
        gateway.listen_address,
        gateway.listen_port,
        "127.0.0.1",
        gateway.status_port,
        tuple(routes),
    )


def _upstream(route: NginxRoute) -> list[str]:
    lines = [f"    upstream {route.upstream_name} {{", f"        zone {route.upstream_name} 64k;"]
    if route.strategy == "active-active":
        lines.append("        least_conn;")
    for index, backend in enumerate(route.backends):
        suffix = " backup" if route.strategy == "active-passive" and index > 0 else ""
        lines.append(
            f"        server {backend.endpoint} max_fails=1 fail_timeout=10s{suffix};"
        )
    lines.extend(("    }", ""))
    return lines


def _location(route: NginxRoute) -> list[str]:
    tries = max(1, len(route.backends))
    return [
        f"        location = {route.path} {{",
        f"            proxy_pass http://{route.upstream_name};",
        "            proxy_http_version 1.1;",
        "            proxy_set_header Host $http_host;",
        "            proxy_set_header Upgrade $http_upgrade;",
        "            proxy_set_header Connection $gost_manager_connection_upgrade;",
        "            proxy_set_header X-Real-IP $remote_addr;",
        "            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        "            proxy_set_header X-Forwarded-Proto $scheme;",
        "            proxy_set_header X-Forwarded-Host $http_host;",
        "            proxy_set_header X-Forwarded-Port $server_port;",
        "            proxy_buffering off;",
        "            proxy_request_buffering off;",
        "            proxy_cache off;",
        "            proxy_socket_keepalive on;",
        "            proxy_connect_timeout 3s;",
        "            proxy_read_timeout 1d;",
        "            proxy_send_timeout 1d;",
        "            send_timeout 1d;",
        "            proxy_next_upstream error timeout invalid_header http_403 http_404 http_500 http_502 http_503 http_504;",
        "            proxy_next_upstream_timeout 15s;",
        f"            proxy_next_upstream_tries {tries};",
        "        }",
        "",
    ]


def render_config(candidate: NginxCandidate, pid_path: str) -> bytes:
    lines = [
        f"# Generated by GOST Manager; config schema {NGINX_CONFIG_SCHEMA_VERSION}.",
        "worker_processes auto;",
        "worker_rlimit_nofile 200000;",
        f"pid {pid_path};",
        "error_log stderr warn;",
        "",
        "events {",
        "    worker_connections 65535;",
        "    multi_accept on;",
        "}",
        "",
        "http {",
        "    access_log off;",
        "    server_tokens off;",
        "    default_type application/octet-stream;",
        "    tcp_nodelay on;",
        "    keepalive_timeout 65s;",
        "    client_header_timeout 15s;",
        "    client_body_timeout 15s;",
        "    reset_timedout_connection on;",
        "",
        "    map $http_upgrade $gost_manager_connection_upgrade {",
        "        default upgrade;",
        "        '' close;",
        "    }",
        "",
    ]
    for route in candidate.routes:
        lines.extend(_upstream(route))
    lines.extend(
        (
            "    server {",
            f"        listen {candidate.listen_address}:{candidate.listen_port} default_server;",
            "        server_name _;",
            "        return 404;",
            "    }",
            "",
        )
    )
    by_host: dict[str, list[NginxRoute]] = {}
    for route in candidate.routes:
        by_host.setdefault(route.host, []).append(route)
    for host in sorted(by_host):
        lines.extend(
            (
                "    server {",
                f"        listen {candidate.listen_address}:{candidate.listen_port};",
                f"        server_name {host};",
                "",
            )
        )
        for route in sorted(by_host[host], key=lambda item: (item.path, item.route_id)):
            lines.extend(_location(route))
        lines.extend(("        location / {", "            return 404;", "        }", "    }", ""))
    lines.extend(
        (
            "    server {",
            f"        listen 127.0.0.1:{candidate.status_port};",
            "        server_name _;",
            "        access_log off;",
            "        location = /nginx_status {",
            "            stub_status;",
            "        }",
            "        location / {",
            "            return 404;",
            "        }",
            "    }",
            "}",
        )
    )
    data = ("\n".join(lines) + "\n").encode("ascii")
    if len(data) > MAX_NGINX_CONFIG_BYTES:
        raise ValidationError("generated NGINX configuration exceeds 4 MiB")
    validate_rendered_config(data, candidate)
    return data


def validate_rendered_config(data: bytes, candidate: NginxCandidate) -> None:
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValidationError("generated NGINX configuration must be ASCII") from exc
    required = (
        "worker_processes auto;",
        "worker_rlimit_nofile 200000;",
        "worker_connections 65535;",
        "multi_accept on;",
        "access_log off;",
        f"listen 127.0.0.1:{candidate.status_port};",
        "location = /nginx_status",
    )
    forbidden = (
        "/etc/nginx",
        "include ",
        "root ",
        "alias ",
        "proxy_pass $",
        "resolver ",
        "ssl_certificate",
    )
    if any(item not in text for item in required) or any(item in text for item in forbidden):
        raise ValidationError("generated NGINX configuration violates its contract")
    if not text.endswith("\n"):
        raise ValidationError("generated NGINX configuration needs a final newline")
