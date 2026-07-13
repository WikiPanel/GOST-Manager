#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$(uname -s)" != "Linux" ]]; then
  printf 'SKIP: real NGINX integration requires Linux\n'
  exit 0
fi
if [[ ! -x /usr/sbin/nginx ]]; then
  printf 'SKIP: /usr/sbin/nginx is unavailable\n'
  exit 0
fi

ulimit -n 8192
PYTHONPATH="${ROOT_DIR}" PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/gost-nginx-pycache" \
  python3 "${ROOT_DIR}/tests/nginx_gateway_integration.py"
