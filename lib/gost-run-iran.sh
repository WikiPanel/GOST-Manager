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

validate_host() {
  local label="$1"
  local value="$2"
  [[ "${value}" =~ ^[A-Za-z0-9.-]+$ ]] || die "${label} must be an IPv4 address or DNS name."
}

validate_mappings() {
  local mappings="$1"
  local IFS=','
  local pairs=()
  local pair listen target seen=","

  if [[ -z "${mappings}" || "${mappings}" == *, || "${mappings}" == ,* || "${mappings}" == *,,* ]]; then
    die "MAPPINGS must use listen_port:target_port format."
  fi

  read -r -a pairs <<< "${mappings}"
  for pair in "${pairs[@]}"; do
    [[ "${pair}" =~ ^[0-9]+:[0-9]+$ ]] || die "Invalid mapping: ${pair}"
    listen="${pair%%:*}"
    target="${pair#*:}"
    is_valid_port "${listen}" || die "Invalid listen port: ${listen}"
    is_valid_port "${target}" || die "Invalid target port: ${target}"
    if [[ "${seen}" == *",${listen},"* ]]; then
      die "Duplicate listen port: ${listen}"
    fi
    seen="${seen}${listen},"
  done
}

: "${GOST_USER:?GOST_USER is required}"
: "${GOST_PASS:?GOST_PASS is required}"
: "${KHAREJ_IP:?KHAREJ_IP is required}"
: "${TUNNEL_PORT:?TUNNEL_PORT is required}"
: "${MAPPINGS:?MAPPINGS is required}"

validate_token "GOST_USER" "${GOST_USER}"
validate_token "GOST_PASS" "${GOST_PASS}"
validate_host "KHAREJ_IP" "${KHAREJ_IP}"
is_valid_port "${TUNNEL_PORT}" || die "TUNNEL_PORT must be between 1 and 65535."
validate_mappings "${MAPPINGS}"

cmd=(/usr/local/bin/gost)
IFS=',' read -r -a pairs <<< "${MAPPINGS}"
for pair in "${pairs[@]}"; do
  listen="${pair%%:*}"
  target="${pair#*:}"
  cmd+=(-L "tcp://0.0.0.0:${listen}/127.0.0.1:${target}")
done
cmd+=(-F "socks5://${GOST_USER}:${GOST_PASS}@${KHAREJ_IP}:${TUNNEL_PORT}")

exec "${cmd[@]}"
