#!/usr/bin/env bash
set -Eeuo pipefail

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

is_positive_integer() {
  local value="$1"
  [[ "${value}" =~ ^[1-9][0-9]*$ ]]
}

is_valid_port() {
  local value="$1"
  is_positive_integer "${value}" && [[ "${value}" -ge 1 && "${value}" -le 65535 ]]
}

validate_token() {
  local label="$1"
  local value="$2"
  [[ "${value}" =~ ^[A-Za-z0-9._~-]+$ ]] || die "${label} contains unsupported characters."
}

: "${GOST_USER:?GOST_USER is required}"
: "${GOST_PASS:?GOST_PASS is required}"
: "${TUNNEL_PORT:?TUNNEL_PORT is required}"

validate_token "GOST_USER" "${GOST_USER}"
validate_token "GOST_PASS" "${GOST_PASS}"
is_valid_port "${TUNNEL_PORT}" || die "TUNNEL_PORT must be between 1 and 65535."

exec /usr/local/bin/gost -L "socks5://${GOST_USER}:${GOST_PASS}@:${TUNNEL_PORT}"
