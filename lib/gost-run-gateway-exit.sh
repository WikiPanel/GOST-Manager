#!/usr/bin/env bash
set -Eeuo pipefail

readonly GOST_BIN="/usr/local/bin/gost"

fail() {
    printf 'Gateway Exit runtime validation failed.\n' >&2
    exit 2
}

required_vars=(
    GATEWAY_EXIT_ID GATEWAY_LISTEN_ADDRESS GATEWAY_LISTEN_PORT
    GATEWAY_EXIT_HOST GATEWAY_SOCKS_PORT GATEWAY_TARGET_ADDRESS
    GATEWAY_TARGET_PORT GOST_USER GOST_PASS
)
for variable in "${required_vars[@]}"; do
    [[ -n "${!variable-}" ]] || fail
done

[[ "${GATEWAY_EXIT_ID}" =~ ^[a-z][a-z0-9-]{0,62}$ ]] || fail
[[ "${GATEWAY_LISTEN_ADDRESS}" == "127.0.0.1" ]] || fail
[[ "${GATEWAY_TARGET_ADDRESS}" == "127.0.0.1" ]] || fail
if [[ "${GATEWAY_EXIT_HOST}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    IFS='.' read -r -a host_octets <<< "${GATEWAY_EXIT_HOST}"
    for octet in "${host_octets[@]}"; do
        ((10#${octet} <= 255)) || fail
        [[ "${octet}" == "0" || "${octet}" != 0* ]] || fail
    done
else
    ((${#GATEWAY_EXIT_HOST} <= 253)) || fail
    [[ "${GATEWAY_EXIT_HOST}" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || fail
fi
[[ "${GOST_USER}" =~ ^[-A-Za-z0-9._~]+$ ]] || fail
[[ "${GOST_PASS}" =~ ^[-A-Za-z0-9._~]+$ ]] || fail
(( ${#GOST_USER} >= 1 && ${#GOST_USER} <= 128 )) || fail
(( ${#GOST_PASS} >= 1 && ${#GOST_PASS} <= 256 )) || fail

for port in "${GATEWAY_LISTEN_PORT}" "${GATEWAY_SOCKS_PORT}" "${GATEWAY_TARGET_PORT}"; do
    [[ "${port}" =~ ^[0-9]{1,5}$ ]] || fail
    ((10#${port} >= 1 && 10#${port} <= 65535)) || fail
done
((10#${GATEWAY_LISTEN_PORT} >= 1024)) || fail

listener="tcp://127.0.0.1:${GATEWAY_LISTEN_PORT}/127.0.0.1:${GATEWAY_TARGET_PORT}"
forward="socks5://${GOST_USER}:${GOST_PASS}@${GATEWAY_EXIT_HOST}:${GATEWAY_SOCKS_PORT}"
arguments=("${GOST_BIN}" -L "${listener}" -F "${forward}")
exec "${arguments[@]}"
