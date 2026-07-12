#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=integration-test-lib.sh
source "${ROOT_DIR}/tests/integration-test-lib.sh"

TEST_HOME="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/gost-nginx-runner.XXXXXX")" && pwd -P)"
cleanup() {
  local status=$?
  rm -rf "${TEST_HOME}"
  exit "${status}"
}
trap cleanup EXIT

mkdir -p "${TEST_HOME}/bin" "${TEST_HOME}/generated"
COMMAND_LOG="${TEST_HOME}/commands.log"
: > "${COMMAND_LOG}"
NGINX_BIN="${TEST_HOME}/bin/nginx"
NGINX_CONFIG="${TEST_HOME}/generated/nginx.conf"
RUNNER="${TEST_HOME}/runner.sh"

cat > "${NGINX_BIN}" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'nginx' >> "${COMMAND_LOG}"
printf ' %q' "$@" >> "${COMMAND_LOG}"
printf '\n' >> "${COMMAND_LOG}"
STUB
chmod 755 "${NGINX_BIN}"
printf 'events {}\n' > "${NGINX_CONFIG}"
chmod 600 "${NGINX_CONFIG}"

sed \
  -e "s|/usr/sbin/nginx|${NGINX_BIN}|" \
  -e "s|/etc/gost-manager/generated/gateway/nginx/nginx.conf|${NGINX_CONFIG}|" \
  "${ROOT_DIR}/lib/gost-run-nginx-gateway.sh" > "${RUNNER}"
chmod 755 "${RUNNER}"

cat > "${TEST_HOME}/bin/stat" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
printf '%s\n' "${STUB_LINK_COUNT:-1}"
STUB
cat > "${TEST_HOME}/bin/realpath" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
printf '%s\n' "${@: -1}"
STUB
chmod 755 "${TEST_HOME}/bin/stat" "${TEST_HOME}/bin/realpath"
export PATH="${TEST_HOME}/bin:${PATH}" COMMAND_LOG

for mode in test start reload quit; do
  : > "${COMMAND_LOG}"
  "${RUNNER}" "--${mode}"
  assert_contains "runner ${mode} uses fixed binary" "nginx" "${COMMAND_LOG}"
  assert_contains "runner ${mode} uses fixed config" "-c ${NGINX_CONFIG}" "${COMMAND_LOG}"
done

: > "${COMMAND_LOG}"
"${RUNNER}" --test
assert_contains "test mode uses nginx -t" "-t -q -p /" "${COMMAND_LOG}"

: > "${COMMAND_LOG}"
"${RUNNER}" --start
assert_contains "start mode remains foreground" "daemon\\ off" "${COMMAND_LOG}"

: > "${COMMAND_LOG}"
"${RUNNER}" --reload
assert_contains "reload mode sends reload signal" "-s reload" "${COMMAND_LOG}"

: > "${COMMAND_LOG}"
"${RUNNER}" --quit
assert_contains "quit mode sends graceful quit" "-s quit" "${COMMAND_LOG}"

if "${RUNNER}" --restart >/dev/null 2>&1; then
  fail "runner rejects unsupported mode"
else
  pass "runner rejects unsupported mode"
fi
if "${RUNNER}" >/dev/null 2>&1; then
  fail "runner rejects missing mode"
else
  pass "runner rejects missing mode"
fi
if "${RUNNER}" --test extra >/dev/null 2>&1; then
  fail "runner rejects extra arguments"
else
  pass "runner rejects extra arguments"
fi

STUB_LINK_COUNT=2
export STUB_LINK_COUNT
if "${RUNNER}" --test >/dev/null 2>&1; then
  fail "runner rejects hard-linked dependency"
else
  pass "runner rejects hard-linked dependency"
fi
unset STUB_LINK_COUNT

rm -f "${NGINX_CONFIG}"
ln -s "${TEST_HOME}/outside.conf" "${NGINX_CONFIG}"
printf 'events {}\n' > "${TEST_HOME}/outside.conf"
if "${RUNNER}" --test >/dev/null 2>&1; then
  fail "runner rejects symlinked config"
else
  pass "runner rejects symlinked config"
fi

assert_not_contains "runner contains no eval" "eval" "${ROOT_DIR}/lib/gost-run-nginx-gateway.sh"
assert_not_contains "runner contains no Direct Mode path" "/etc/gost/" "${ROOT_DIR}/lib/gost-run-nginx-gateway.sh"
assert_not_contains "runner contains no GOST Exit lifecycle" "gost-gateway-exit-" "${ROOT_DIR}/lib/gost-run-nginx-gateway.sh"
assert_not_contains "runner contains no firewall command" "iptables" "${ROOT_DIR}/lib/gost-run-nginx-gateway.sh"
assert_not_contains "runner contains no environment override" "\${NGINX_BIN:-" "${ROOT_DIR}/lib/gost-run-nginx-gateway.sh"

finish_suite
