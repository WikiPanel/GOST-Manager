#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUNNER="${SCRIPT_DIR}/../lib/gost-run-gateway-exit.sh"
TEST_HOME="$(mktemp -d)"
trap 'rm -rf -- "${TEST_HOME}"' EXIT

pass=0
fail=0
assert_true() {
    local label="$1"
    shift
    if "$@"; then
        printf 'PASS: %s\n' "${label}"
        ((pass += 1))
    else
        printf 'FAIL: %s\n' "${label}" >&2
        ((fail += 1))
    fi
}

assert_not_contains() {
    local needle="$1"
    local path="$2"
    ! grep -Fq -- "${needle}" "${path}"
}

assert_not_regex() {
    local pattern="$1"
    local path="$2"
    ! grep -Eq -- "${pattern}" "${path}"
}

canary_user="user-$(printf '%s' "${RANDOM}${RANDOM}" | shasum | cut -c1-12)"
canary_pass="pass-$(printf '%s' "${RANDOM}${RANDOM}${RANDOM}" | shasum | cut -c1-16)"
capture="${TEST_HOME}/argv"
env_file="${TEST_HOME}/bash-env"
cat > "${env_file}" <<'EOF'
exec() {
    printf '%s\n' "$@" > "${GATEWAY_TEST_CAPTURE}"
}
EOF

GATEWAY_TEST_CAPTURE="${capture}" BASH_ENV="${env_file}" \
GATEWAY_EXIT_ID=ee-primary \
GATEWAY_LISTEN_ADDRESS=127.0.0.1 GATEWAY_LISTEN_PORT=18081 \
GATEWAY_EXIT_HOST=exit.example.org GATEWAY_SOCKS_PORT=28420 \
GATEWAY_TARGET_ADDRESS=127.0.0.1 GATEWAY_TARGET_PORT=18081 \
GOST_USER="${canary_user}" GOST_PASS="${canary_pass}" \
bash "${RUNNER}" > "${TEST_HOME}/stdout" 2> "${TEST_HOME}/stderr"

assert_true "runner uses fixed GOST binary" grep -Fxq "/usr/local/bin/gost" "${capture}"
assert_true "runner emits exactly one listener flag" test "$(grep -Fxc -- '-L' "${capture}")" -eq 1
assert_true "runner emits exactly one forward flag" test "$(grep -Fxc -- '-F' "${capture}")" -eq 1
assert_true "runner listener is loopback-only" grep -Fxq "tcp://127.0.0.1:18081/127.0.0.1:18081" "${capture}"
assert_true "runner credentials reach only exec argv" grep -Fxq "socks5://${canary_user}:${canary_pass}@exit.example.org:28420" "${capture}"
assert_true "runner stdout contains no username" assert_not_contains "${canary_user}" "${TEST_HOME}/stdout"
assert_true "runner stderr contains no password" assert_not_contains "${canary_pass}" "${TEST_HOME}/stderr"

set +e
GATEWAY_EXIT_ID=ee-primary GATEWAY_LISTEN_ADDRESS=0.0.0.0 \
GATEWAY_LISTEN_PORT=18081 GATEWAY_EXIT_HOST=exit.example.org \
GATEWAY_SOCKS_PORT=28420 GATEWAY_TARGET_ADDRESS=127.0.0.1 \
GATEWAY_TARGET_PORT=18081 GOST_USER="${canary_user}" GOST_PASS="${canary_pass}" \
bash "${RUNNER}" > "${TEST_HOME}/bad.out" 2> "${TEST_HOME}/bad.err"
bad_status=$?
set -e
assert_true "runner rejects public listener" test "${bad_status}" -ne 0
assert_true "runner validation error contains no username" assert_not_contains "${canary_user}" "${TEST_HOME}/bad.err"
assert_true "runner validation error contains no password" assert_not_contains "${canary_pass}" "${TEST_HOME}/bad.err"
assert_true "runner contains no eval" assert_not_contains "eval " "${RUNNER}"
assert_true "runner contains no Direct Mode path" assert_not_regex 'gost-(iran|kharej)|/etc/gost/' "${RUNNER}"

printf '\nResult: %d passed, %d failed\n' "${pass}" "${fail}"
((fail == 0))
