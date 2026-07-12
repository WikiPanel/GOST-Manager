#!/usr/bin/env bash
set -Eeuo pipefail

readonly NGINX_BIN="/usr/sbin/nginx"
readonly NGINX_CONFIG="/etc/gost-manager/generated/gateway/nginx/nginx.conf"

fail() {
    printf 'NGINX Gateway runtime validation failed.\n' >&2
    exit 2
}

[[ "$#" -eq 1 ]] || fail
[[ -f "${NGINX_BIN}" && -x "${NGINX_BIN}" && ! -L "${NGINX_BIN}" ]] || fail
[[ -f "${NGINX_CONFIG}" && ! -L "${NGINX_CONFIG}" ]] || fail
[[ "$(stat -c '%h' -- "${NGINX_BIN}")" == "1" ]] || fail
[[ "$(stat -c '%h' -- "${NGINX_CONFIG}")" == "1" ]] || fail
[[ "$(realpath -e -- "${NGINX_BIN}")" == "${NGINX_BIN}" ]] || fail
[[ "$(realpath -e -- "${NGINX_CONFIG}")" == "${NGINX_CONFIG}" ]] || fail

case "$1" in
    --test)
        arguments=("${NGINX_BIN}" -t -q -p / -c "${NGINX_CONFIG}")
        ;;
    --start)
        arguments=("${NGINX_BIN}" -p / -c "${NGINX_CONFIG}" -g 'daemon off;')
        ;;
    --reload)
        arguments=("${NGINX_BIN}" -p / -c "${NGINX_CONFIG}" -s reload)
        ;;
    --quit)
        arguments=("${NGINX_BIN}" -p / -c "${NGINX_CONFIG}" -s quit)
        ;;
    *)
        fail
        ;;
esac

exec "${arguments[@]}"
