#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=integration-test-lib.sh
source "${ROOT_DIR}/tests/integration-test-lib.sh"

TEST_HOME="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/gost-uninstall-tests.XXXXXX")" && pwd -P)"
cleanup_test_home() {
  local status=$?
  rm -rf "${TEST_HOME}"
  exit "${status}"
}
trap cleanup_test_home EXIT

COMMAND_LOG="${TEST_HOME}/commands.log"
STUB_STATE_DIR="${TEST_HOME}/state"
STUB_BIN="${TEST_HOME}/bin"
mkdir -p "${STUB_STATE_DIR}"
: > "${COMMAND_LOG}"
make_command_stubs "${STUB_BIN}"
export COMMAND_LOG STUB_STATE_DIR
export PATH="${STUB_BIN}:${PATH}"
export REPO_ROOT="${ROOT_DIR}"

cat > "${STUB_BIN}/gost-monitor-admin" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'admin %s\n' "$*" >> "${COMMAND_LOG}"
PYTHONPATH="${REPO_ROOT}" python3 -m monitoring.admin_cli \
  --policy installed --path-root "${GOST_MANAGER_ROOT}" \
  --lock-path "${GOST_MANAGER_ROOT}/run/gost-manager/collector.lock" "$@"
STUB
chmod 755 "${STUB_BIN}/gost-monitor-admin"

create_fixture() {
  local root="$1"
  local gateway_user gateway_pass
  gateway_user="user-$(printf '%s' "${RANDOM}${RANDOM}" | shasum | cut -c1-12)"
  gateway_pass="pass-$(printf '%s' "${RANDOM}${RANDOM}${RANDOM}" | shasum | cut -c1-16)"
  rm -f "${STUB_STATE_DIR}/active" "${STUB_STATE_DIR}/enabled"
  rm -rf "${STUB_STATE_DIR}/wants"
  mkdir -p \
    "${root}/usr/local/sbin" \
    "${root}/usr/local/lib/gost-manager/monitoring" \
    "${root}/usr/local/lib/gost-manager/gateway" \
    "${root}/usr/local/bin" \
    "${root}/etc/systemd/system" \
    "${root}/etc/gost-manager/secrets" \
    "${root}/etc/gost-manager/generated/gateway/exits" \
    "${root}/etc/gost-manager/backups/gateway" \
    "${root}/etc/gost-manager/backups/gateway-runtime" \
    "${root}/etc/gost" \
    "${root}/var/lib/gost-manager"
  printf 'manager\n' > "${root}/usr/local/sbin/gost-manager"
  printf 'monitor\n' > "${root}/usr/local/sbin/gost-monitor"
  printf 'admin\n' > "${root}/usr/local/sbin/gost-monitor-admin"
  printf 'collector\n' > "${root}/usr/local/sbin/gost-monitor-collector"
  printf 'python\n' > "${root}/usr/local/lib/gost-manager/monitoring/__init__.py"
  printf 'gateway\n' > "${root}/usr/local/lib/gost-manager/gateway/__init__.py"
  cp -R "${ROOT_DIR}/gateway/." "${root}/usr/local/lib/gost-manager/gateway/"
  printf 'gateway-cli\n' > "${root}/usr/local/sbin/gost-gateway"
  printf 'gateway-runtime\n' > "${root}/usr/local/sbin/gost-gateway-runtime"
  printf 'gateway-runner\n' > "${root}/usr/local/lib/gost-manager/gost-run-gateway-exit.sh"
  printf 'iran-runner\n' > "${root}/usr/local/lib/gost-manager/gost-run-iran.sh"
  printf 'kharej-runner\n' > "${root}/usr/local/lib/gost-manager/gost-run-kharej.sh"
  printf 'gost\n' > "${root}/usr/local/bin/gost"
  printf '[Unit]\nDescription=monitor\n' > "${root}/etc/systemd/system/gost-monitor-collector.service"
  printf '[Unit]\nDescription=managed\n' > "${root}/etc/systemd/system/gost-iran-1.service"
  printf '[Unit]\nDescription=unmanaged\n' > "${root}/etc/systemd/system/custom-gost.service"
  printf '[Unit]\nDescription=gateway\n' > "${root}/etc/systemd/system/gost-gateway-exit-ee-primary.service"
  printf 'GATEWAY_EXIT_ID=ee-primary\n' > "${root}/etc/gost-manager/generated/gateway/exits/ee-primary.env"
  printf '{"schema_version":1,"services":[]}\n' > "${root}/etc/gost-manager/generated/gateway/runtime.json"
  printf 'state\n' > "${root}/etc/gost-manager/state.json"
  printf '%s\n' \
    '{"schema_version":1,"document_id":"12345678-1234-4234-8234-123456789abc","node_id":"iran-gateway-1","revision":1,"updated_at":"2026-07-12T00:00:00Z","bindings":[{"exit_id":"ee-primary","enabled":true,"listen_address":"127.0.0.1","listen_port":18081,"secret_ref":"secret-ee-primary"}]}' \
    > "${root}/etc/gost-manager/node.json"
  printf 'backup\n' > "${root}/etc/gost-manager/backups/gateway/state.json"
  printf 'runtime-backup\n' > "${root}/etc/gost-manager/backups/gateway-runtime/runtime.json"
  printf 'GOST_USER=%s\nGOST_PASS=%s\n' "${gateway_user}" "${gateway_pass}" \
    > "${root}/etc/gost-manager/secrets/secret-ee-primary.env"
  printf 'MAPPINGS=2052:2052\nPASSWORD=uninstall-secret-canary\n' > "${root}/etc/gost/iran-1.env"
  cp "${ROOT_DIR}/packaging/monitoring.env" "${root}/etc/gost-manager/monitoring.env"
  PYTHONPATH="${ROOT_DIR}" python3 -m monitoring.admin_cli --policy generic \
    migrate --db "${root}/var/lib/gost-manager/metrics.sqlite3" >/dev/null
  PYTHONPATH="${ROOT_DIR}" python3 - "${root}/var/lib/gost-manager/metrics.sqlite3" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("INSERT INTO events(ts,severity,code,message,details_json) VALUES(1,'info','keep','keep','{}')")
conn.commit()
conn.close()
PY
}

configure_custom_history() {
  local root="$1"
  local config="${root}/etc/gost-manager/monitoring.env"
  local custom="${root}/var/lib/gost-manager/archive/custom.sqlite3"
  sed 's|^GOST_MONITOR_DB=.*|GOST_MONITOR_DB=/var/lib/gost-manager/archive/custom.sqlite3|' \
    "${config}" > "${config}.new"
  mv "${config}.new" "${config}"
  mkdir -p "${custom%/*}"
  PYTHONPATH="${ROOT_DIR}" python3 -m monitoring.admin_cli --policy generic migrate --db "${custom}" >/dev/null
  PYTHONPATH="${ROOT_DIR}" python3 - "${custom}" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("INSERT INTO events(ts,severity,code,message,details_json) VALUES(2,'info','custom','custom','{}')")
conn.close()
PY
}

run_plan() {
  local root="$1"
  shift
  (
    export GOST_MANAGER_TESTING=1
    export GOST_MANAGER_ROOT="${root}"
    export GOST_MANAGER_SOURCE_ONLY=1
    export STUB_UNIT_PATH="${root}/etc/systemd/system/gost-monitor-collector.service"
    export MONITOR_ADMIN_BIN="${STUB_BIN}/gost-monitor-admin"
    export SYSTEMCTL_BIN=systemctl
    # shellcheck source=../uninstall.sh
    source "${ROOT_DIR}/uninstall.sh"
    require_safe_root
    while [[ "$#" -gt 0 ]]; do
      case "$1" in
        manager) REMOVE_MANAGER=1 ;;
        monitor-service) REMOVE_MONITOR_SERVICE=1 ;;
        monitor-code) REMOVE_MONITOR_CODE=1 ;;
        monitor-config) REMOVE_MONITOR_CONFIG=1 ;;
        history) REMOVE_MONITOR_HISTORY=1 ;;
        traffic) REMOVE_TRAFFIC=1 ;;
        credentials) REMOVE_CREDENTIALS=1 ;;
        binary) REMOVE_GOST_BINARY=1 ;;
        gateway-runtime) REMOVE_GATEWAY_RUNTIME=1 ;;
        gateway-state) REMOVE_GATEWAY_STATE=1 ;;
        gateway-secrets) REMOVE_GATEWAY_SECRETS=1 ;;
        gateway-package) REMOVE_GATEWAY_PACKAGE=1 ;;
        *) return 2 ;;
      esac
      shift
    done
    apply_plan
  )
}

cancel_root="${TEST_HOME}/cancel"
create_fixture "${cancel_root}"
cancel_before="$(tree_digest "${cancel_root}")"
: > "${COMMAND_LOG}"
printf 'n\nn\nn\nn\nn\nn\nn\nn\n' | \
  GOST_MANAGER_TESTING=1 GOST_MANAGER_ROOT="${cancel_root}" \
  SYSTEMCTL_BIN=systemctl MONITOR_ADMIN_BIN="${STUB_BIN}/gost-monitor-admin" \
  bash "${ROOT_DIR}/uninstall.sh" >/dev/null
assert_eq "cancel everything changes nothing" "${cancel_before}" "$(tree_digest "${cancel_root}")"
assert_eq "cancel everything calls no commands" "0" "$(wc -l < "${COMMAND_LOG}" | tr -d ' ')"

gateway_runtime_root="${TEST_HOME}/gateway-runtime"
create_fixture "${gateway_runtime_root}"
run_plan "${gateway_runtime_root}" gateway-runtime >/dev/null
assert_absent "gateway runtime-only removes exact unit" "${gateway_runtime_root}/etc/systemd/system/gost-gateway-exit-ee-primary.service"
assert_absent "gateway runtime-only removes exact env" "${gateway_runtime_root}/etc/gost-manager/generated/gateway/exits/ee-primary.env"
assert_file "gateway runtime-only keeps state" "${gateway_runtime_root}/etc/gost-manager/state.json"
assert_file "gateway runtime-only keeps secret" "${gateway_runtime_root}/etc/gost-manager/secrets/secret-ee-primary.env"
assert_file "gateway runtime-only keeps package" "${gateway_runtime_root}/usr/local/lib/gost-manager/gateway/__init__.py"
assert_file "gateway runtime-only keeps Direct unit" "${gateway_runtime_root}/etc/systemd/system/gost-iran-1.service"
assert_file "gateway runtime-only keeps monitoring" "${gateway_runtime_root}/usr/local/sbin/gost-monitor"

gateway_state_root="${TEST_HOME}/gateway-state"
create_fixture "${gateway_state_root}"
run_plan "${gateway_state_root}" gateway-state >/dev/null
assert_absent "gateway state-only removes shared state" "${gateway_state_root}/etc/gost-manager/state.json"
assert_absent "gateway state-only removes node state" "${gateway_state_root}/etc/gost-manager/node.json"
assert_absent "gateway state-only removes state backups" "${gateway_state_root}/etc/gost-manager/backups/gateway"
assert_file "gateway state-only keeps secret" "${gateway_state_root}/etc/gost-manager/secrets/secret-ee-primary.env"
assert_file "gateway state-only keeps runtime unit" "${gateway_state_root}/etc/systemd/system/gost-gateway-exit-ee-primary.service"

gateway_secret_refusal_root="${TEST_HOME}/gateway-secret-refusal"
create_fixture "${gateway_secret_refusal_root}"
if run_plan "${gateway_secret_refusal_root}" gateway-secrets >/dev/null 2>&1; then
  fail "gateway referenced secret removal refused"
else
  pass "gateway referenced secret removal refused"
fi
assert_file "gateway referenced secret preserved" "${gateway_secret_refusal_root}/etc/gost-manager/secrets/secret-ee-primary.env"

gateway_secret_root="${TEST_HOME}/gateway-secret"
create_fixture "${gateway_secret_root}"
printf '%s\n' \
  '{"schema_version":1,"document_id":"12345678-1234-4234-8234-123456789abc","node_id":"iran-gateway-1","revision":1,"updated_at":"2026-07-12T00:00:00Z","bindings":[]}' \
  > "${gateway_secret_root}/etc/gost-manager/node.json"
rm -f "${gateway_secret_root}/etc/systemd/system/gost-gateway-exit-ee-primary.service" \
  "${gateway_secret_root}/etc/gost-manager/generated/gateway/exits/ee-primary.env"
run_plan "${gateway_secret_root}" gateway-secrets >/dev/null
assert_absent "gateway unreferenced secrets removed" "${gateway_secret_root}/etc/gost-manager/secrets/secret-ee-primary.env"
assert_file "gateway secret-only keeps state" "${gateway_secret_root}/etc/gost-manager/state.json"

malformed_gateway_state_root="${TEST_HOME}/gateway-malformed-state"
create_fixture "${malformed_gateway_state_root}"
printf '{malformed\n' > "${malformed_gateway_state_root}/etc/gost-manager/node.json"
if run_plan "${malformed_gateway_state_root}" gateway-secrets >/dev/null 2>&1; then
  fail "malformed gateway state refuses secret deletion"
else
  pass "malformed gateway state refuses secret deletion"
fi
assert_file "malformed gateway state preserves secret" "${malformed_gateway_state_root}/etc/gost-manager/secrets/secret-ee-primary.env"

symlink_gateway_state_root="${TEST_HOME}/gateway-symlink-state"
create_fixture "${symlink_gateway_state_root}"
printf 'outside state\n' > "${TEST_HOME}/outside-node.json"
rm -f "${symlink_gateway_state_root}/etc/gost-manager/node.json"
ln -s "${TEST_HOME}/outside-node.json" "${symlink_gateway_state_root}/etc/gost-manager/node.json"
if run_plan "${symlink_gateway_state_root}" gateway-secrets >/dev/null 2>&1; then
  fail "symlinked gateway state refuses secret deletion"
else
  pass "symlinked gateway state refuses secret deletion"
fi
assert_file "symlinked gateway state preserves secret" "${symlink_gateway_state_root}/etc/gost-manager/secrets/secret-ee-primary.env"

state_with_runtime_root="${TEST_HOME}/gateway-state-runtime-remains"
create_fixture "${state_with_runtime_root}"
if run_plan "${state_with_runtime_root}" gateway-state gateway-secrets >/dev/null 2>&1; then
  fail "gateway state deletion keeps secret while runtime remains"
else
  pass "gateway state deletion keeps secret while runtime remains"
fi
assert_absent "state deletion removes node with runtime remaining" "${state_with_runtime_root}/etc/gost-manager/node.json"
assert_file "state deletion preserves secret with runtime remaining" "${state_with_runtime_root}/etc/gost-manager/secrets/secret-ee-primary.env"

fully_removed_invalid_state_root="${TEST_HOME}/gateway-invalid-full-removal"
create_fixture "${fully_removed_invalid_state_root}"
printf '{invalid\n' > "${fully_removed_invalid_state_root}/etc/gost-manager/node.json"
run_plan "${fully_removed_invalid_state_root}" gateway-runtime gateway-state gateway-secrets >/dev/null
assert_absent "fully removed invalid state permits secret deletion" "${fully_removed_invalid_state_root}/etc/gost-manager/secrets/secret-ee-primary.env"

systemd_only_root="${TEST_HOME}/gateway-systemd-only"
create_fixture "${systemd_only_root}"
rm -f \
  "${systemd_only_root}/etc/systemd/system/gost-gateway-exit-ee-primary.service" \
  "${systemd_only_root}/etc/gost-manager/generated/gateway/exits/ee-primary.env" \
  "${systemd_only_root}/etc/gost-manager/generated/gateway/runtime.json"
(
  export STUB_GATEWAY_LOADED_SERVICES=gost-gateway-exit-systemd-only.service
  export STUB_GATEWAY_ACTIVE_SERVICES=gost-gateway-exit-systemd-only.service
  run_plan "${systemd_only_root}" gateway-runtime gateway-state gateway-secrets gateway-package >/dev/null
)
assert_absent "systemd-only active gateway service is removed" "${systemd_only_root}/etc/gost-manager/secrets/secret-ee-primary.env"
assert_absent "systemd-only gateway package is removed" "${systemd_only_root}/usr/local/lib/gost-manager/gateway"

gateway_package_refusal_root="${TEST_HOME}/gateway-package-refusal"
create_fixture "${gateway_package_refusal_root}"
if run_plan "${gateway_package_refusal_root}" gateway-package >/dev/null 2>&1; then
  fail "gateway package removal refused while service remains"
else
  pass "gateway package removal refused while service remains"
fi
assert_file "gateway package refusal preserves runner" "${gateway_package_refusal_root}/usr/local/lib/gost-manager/gost-run-gateway-exit.sh"

gateway_full_root="${TEST_HOME}/gateway-full"
create_fixture "${gateway_full_root}"
run_plan "${gateway_full_root}" gateway-runtime gateway-state gateway-secrets gateway-package >/dev/null
assert_absent "full gateway removal removes runtime unit" "${gateway_full_root}/etc/systemd/system/gost-gateway-exit-ee-primary.service"
assert_absent "full gateway removal removes state" "${gateway_full_root}/etc/gost-manager/state.json"
assert_absent "full gateway removal removes secret" "${gateway_full_root}/etc/gost-manager/secrets/secret-ee-primary.env"
assert_absent "full gateway removal removes package" "${gateway_full_root}/usr/local/lib/gost-manager/gateway"
assert_file "full gateway removal keeps Direct Mode" "${gateway_full_root}/etc/systemd/system/gost-iran-1.service"
assert_file "full gateway removal keeps monitoring" "${gateway_full_root}/usr/local/lib/gost-manager/monitoring/__init__.py"
assert_file "full gateway removal keeps unmanaged unit" "${gateway_full_root}/etc/systemd/system/custom-gost.service"

gateway_partial_root="${TEST_HOME}/gateway-partial"
create_fixture "${gateway_partial_root}"
printf '[Unit]\nDescription=gateway backup\n' > "${gateway_partial_root}/etc/systemd/system/gost-gateway-exit-de-backup.service"
printf 'GATEWAY_EXIT_ID=de-backup\n' > "${gateway_partial_root}/etc/gost-manager/generated/gateway/exits/de-backup.env"
printf 'GOST_USER=%s\nGOST_PASS=%s\n' \
  "user-$(printf '%s' "${RANDOM}${RANDOM}" | shasum | cut -c1-12)" \
  "pass-$(printf '%s' "${RANDOM}${RANDOM}${RANDOM}" | shasum | cut -c1-16)" \
  > "${gateway_partial_root}/etc/gost-manager/secrets/secret-de-backup.env"
if STUB_FAIL_SYSTEMCTL_ACTION=disable \
  STUB_FAIL_SYSTEMCTL_UNIT=gost-gateway-exit-ee-primary.service \
  run_plan "${gateway_partial_root}" gateway-runtime gateway-state gateway-secrets gateway-package >/dev/null 2>&1; then
  fail "partial gateway service removal reports failure"
else
  pass "partial gateway service removal reports failure"
fi
assert_absent "partial gateway removal removes successful service" "${gateway_partial_root}/etc/systemd/system/gost-gateway-exit-de-backup.service"
assert_file "partial gateway removal preserves failed service" "${gateway_partial_root}/etc/systemd/system/gost-gateway-exit-ee-primary.service"
assert_file "partial gateway removal preserves failed env" "${gateway_partial_root}/etc/gost-manager/generated/gateway/exits/ee-primary.env"
assert_file "partial gateway removal preserves referenced secret" "${gateway_partial_root}/etc/gost-manager/secrets/secret-ee-primary.env"
assert_file "partial gateway removal preserves state" "${gateway_partial_root}/etc/gost-manager/state.json"
assert_file "partial gateway removal preserves package" "${gateway_partial_root}/usr/local/lib/gost-manager/gateway/__init__.py"
assert_file "partial gateway removal preserves runner" "${gateway_partial_root}/usr/local/lib/gost-manager/gost-run-gateway-exit.sh"

manager_root="${TEST_HOME}/manager"
create_fixture "${manager_root}"
run_plan "${manager_root}" manager >/dev/null
assert_absent "manager-only removes CLI" "${manager_root}/usr/local/sbin/gost-manager"
assert_file "manager-only keeps traffic unit" "${manager_root}/etc/systemd/system/gost-iran-1.service"
assert_file "manager-only keeps monitoring" "${manager_root}/usr/local/sbin/gost-monitor"

monitor_root="${TEST_HOME}/monitor-only"
create_fixture "${monitor_root}"
: > "${COMMAND_LOG}"
run_plan "${monitor_root}" monitor-service monitor-code >/dev/null
assert_absent "monitor-only removes unit" "${monitor_root}/etc/systemd/system/gost-monitor-collector.service"
assert_absent "monitor-only removes Python code" "${monitor_root}/usr/local/lib/gost-manager/monitoring"
assert_file "monitor-only retains history" "${monitor_root}/var/lib/gost-manager/metrics.sqlite3"
assert_file "monitor-only retains config" "${monitor_root}/etc/gost-manager/monitoring.env"
assert_file "monitor-only retains traffic unit" "${monitor_root}/etc/systemd/system/gost-iran-1.service"
assert_file "monitor-only retains credentials" "${monitor_root}/etc/gost/iran-1.env"
assert_not_contains "monitor-only never stops traffic" "gost-iran-1.service" "${COMMAND_LOG}"

history_root="${TEST_HOME}/history"
create_fixture "${history_root}"
touch "${STUB_STATE_DIR}/active"
: > "${COMMAND_LOG}"
run_plan "${history_root}" history >/dev/null
assert_file "history-only keeps valid database" "${history_root}/var/lib/gost-manager/metrics.sqlite3"
history_rows="$(PYTHONPATH="${ROOT_DIR}" python3 - "${history_root}/var/lib/gost-manager/metrics.sqlite3" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
print(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
conn.close()
PY
)"
assert_eq "history-only purges rows" "0" "${history_rows}"
assert_file "history-only keeps config" "${history_root}/etc/gost-manager/monitoring.env"
assert_file "history-only keeps traffic" "${history_root}/etc/systemd/system/gost-iran-1.service"
assert_contains "history-only stops collector" "systemctl stop gost-monitor-collector.service" "${COMMAND_LOG}"
assert_contains "history-only restores active collector" "systemctl start gost-monitor-collector.service" "${COMMAND_LOG}"
rm -f "${STUB_STATE_DIR}/active"

config_root="${TEST_HOME}/config"
create_fixture "${config_root}"
rm -f "${config_root}/etc/systemd/system/gost-monitor-collector.service"
run_plan "${config_root}" monitor-config >/dev/null
assert_absent "config-only removes monitoring config" "${config_root}/etc/gost-manager/monitoring.env"
assert_file "config-only keeps history" "${config_root}/var/lib/gost-manager/metrics.sqlite3"
assert_file "config-only keeps traffic" "${config_root}/etc/systemd/system/gost-iran-1.service"

complete_monitor_root="${TEST_HOME}/complete-monitor"
create_fixture "${complete_monitor_root}"
run_plan "${complete_monitor_root}" monitor-service monitor-code monitor-config history >/dev/null
assert_absent "complete monitoring removal removes code" "${complete_monitor_root}/usr/local/lib/gost-manager/monitoring"
assert_absent "complete monitoring removal removes config" "${complete_monitor_root}/etc/gost-manager/monitoring.env"
assert_absent "complete monitoring removal removes history" "${complete_monitor_root}/var/lib/gost-manager"
assert_file "complete monitoring removal keeps traffic" "${complete_monitor_root}/etc/systemd/system/gost-iran-1.service"
assert_file "complete monitoring removal keeps runners" "${complete_monitor_root}/usr/local/lib/gost-manager/gost-run-iran.sh"

traffic_root="${TEST_HOME}/traffic"
create_fixture "${traffic_root}"
run_plan "${traffic_root}" traffic >/dev/null
assert_absent "traffic removal removes exact managed unit" "${traffic_root}/etc/systemd/system/gost-iran-1.service"
assert_file "traffic removal keeps credentials" "${traffic_root}/etc/gost/iran-1.env"
assert_file "traffic removal keeps unmanaged unit" "${traffic_root}/etc/systemd/system/custom-gost.service"
assert_absent "traffic removal removes now-unused runner" "${traffic_root}/usr/local/lib/gost-manager/gost-run-iran.sh"

credentials_root="${TEST_HOME}/credentials"
create_fixture "${credentials_root}"
run_plan "${credentials_root}" traffic credentials >/dev/null
assert_absent "traffic+credentials removes env tree" "${credentials_root}/etc/gost"
assert_file "traffic+credentials keeps monitoring" "${credentials_root}/usr/local/sbin/gost-monitor"
assert_file "traffic+credentials keeps unmanaged unit" "${credentials_root}/etc/systemd/system/custom-gost.service"

full_root="${TEST_HOME}/full"
create_fixture "${full_root}"
run_plan "${full_root}" manager monitor-service monitor-code monitor-config history traffic credentials binary >/dev/null
assert_absent "full selection removes manager" "${full_root}/usr/local/sbin/gost-manager"
assert_absent "full selection removes monitoring" "${full_root}/usr/local/sbin/gost-monitor"
assert_absent "full selection removes history" "${full_root}/var/lib/gost-manager"
assert_absent "full selection removes credentials" "${full_root}/etc/gost"
assert_absent "full selection removes GOST binary" "${full_root}/usr/local/bin/gost"
assert_file "full selection keeps unmanaged unit" "${full_root}/etc/systemd/system/custom-gost.service"

dependency_root="${TEST_HOME}/dependency"
create_fixture "${dependency_root}"
if run_plan "${dependency_root}" monitor-code >/dev/null 2>&1; then
  fail "monitoring code removal refuses while service remains"
else
  pass "monitoring code removal refuses while service remains"
fi
assert_file "dependency refusal keeps monitoring code" "${dependency_root}/usr/local/lib/gost-manager/monitoring/__init__.py"

failure_root="${TEST_HOME}/failure"
create_fixture "${failure_root}"
: > "${COMMAND_LOG}"
export STUB_FAIL_SYSTEMCTL_ACTION=disable
if run_plan "${failure_root}" traffic >/dev/null; then
  fail "traffic systemctl failure reports partial failure"
else
  pass "traffic systemctl failure reports partial failure"
fi
unset STUB_FAIL_SYSTEMCTL_ACTION
assert_file "traffic systemctl failure keeps unit" "${failure_root}/etc/systemd/system/gost-iran-1.service"
assert_file "traffic systemctl failure keeps runner" "${failure_root}/usr/local/lib/gost-manager/gost-run-iran.sh"
assert_file "traffic systemctl failure keeps unmanaged unit" "${failure_root}/etc/systemd/system/custom-gost.service"

binary_refusal_root="${TEST_HOME}/binary-refusal"
create_fixture "${binary_refusal_root}"
if run_plan "${binary_refusal_root}" binary >/dev/null 2>&1; then
  fail "binary-only removal refuses while traffic remains"
else
  pass "binary-only removal refuses while traffic remains"
fi
assert_file "binary-only refusal preserves GOST binary" "${binary_refusal_root}/usr/local/bin/gost"

credentials_refusal_root="${TEST_HOME}/credentials-refusal"
create_fixture "${credentials_refusal_root}"
if run_plan "${credentials_refusal_root}" credentials >/dev/null 2>&1; then
  fail "credentials-only removal refuses while traffic remains"
else
  pass "credentials-only removal refuses while traffic remains"
fi
assert_file "credentials-only refusal preserves env" "${credentials_refusal_root}/etc/gost/iran-1.env"

combined_failure_root="${TEST_HOME}/combined-traffic-failure"
create_fixture "${combined_failure_root}"
printf '[Unit]\nDescription=managed second\n' > "${combined_failure_root}/etc/systemd/system/gost-kharej-2.service"
printf 'MAPPINGS=2053:2053\nPASSWORD=combined-canary\n' > "${combined_failure_root}/etc/gost/kharej-2.env"
: > "${COMMAND_LOG}"
if STUB_FAIL_SYSTEMCTL_ACTION=disable STUB_FAIL_SYSTEMCTL_UNIT=gost-kharej-2.service \
  run_plan "${combined_failure_root}" traffic credentials binary > "${TEST_HOME}/combined-failure.out"; then
  fail "combined traffic dependency failure returns non-zero"
else
  pass "combined traffic dependency failure returns non-zero"
fi
assert_absent "combined failure removes successfully disabled unit" "${combined_failure_root}/etc/systemd/system/gost-iran-1.service"
assert_file "combined failure keeps surviving managed unit" "${combined_failure_root}/etc/systemd/system/gost-kharej-2.service"
assert_dir "combined failure preserves all credentials" "${combined_failure_root}/etc/gost"
assert_file "combined failure preserves GOST binary" "${combined_failure_root}/usr/local/bin/gost"
assert_file "combined failure preserves Iran runner" "${combined_failure_root}/usr/local/lib/gost-manager/gost-run-iran.sh"
assert_file "combined failure preserves Kharej runner" "${combined_failure_root}/usr/local/lib/gost-manager/gost-run-kharej.sh"
assert_file "combined failure keeps unrelated unit" "${combined_failure_root}/etc/systemd/system/custom-gost.service"
assert_contains "combined failure reports exact survivor" "gost-kharej-2.service" "${TEST_HOME}/combined-failure.out"

all_traffic_root="${TEST_HOME}/all-traffic-success"
create_fixture "${all_traffic_root}"
printf '[Unit]\nDescription=managed second\n' > "${all_traffic_root}/etc/systemd/system/gost-kharej-2.service"
run_plan "${all_traffic_root}" traffic credentials binary >/dev/null
assert_absent "all-traffic success removes credentials" "${all_traffic_root}/etc/gost"
assert_absent "all-traffic success removes GOST binary" "${all_traffic_root}/usr/local/bin/gost"
assert_absent "all-traffic success removes Iran runner" "${all_traffic_root}/usr/local/lib/gost-manager/gost-run-iran.sh"
assert_absent "all-traffic success removes Kharej runner" "${all_traffic_root}/usr/local/lib/gost-manager/gost-run-kharej.sh"

collector_missing_root="${TEST_HOME}/collector-unit-missing"
create_fixture "${collector_missing_root}"
rm -f "${collector_missing_root}/etc/systemd/system/gost-monitor-collector.service"
touch "${STUB_STATE_DIR}/active"
: > "${COMMAND_LOG}"
run_plan "${collector_missing_root}" monitor-service monitor-code monitor-config history >/dev/null
assert_contains "unit-missing active collector still stopped" "systemctl disable --now gost-monitor-collector.service" "${COMMAND_LOG}"
assert_absent "unit-missing collector permits code removal after stop" "${collector_missing_root}/usr/local/lib/gost-manager/monitoring"
assert_absent "unit-missing collector permits config removal after stop" "${collector_missing_root}/etc/gost-manager/monitoring.env"
assert_absent "unit-missing collector permits configured history removal" "${collector_missing_root}/var/lib/gost-manager/metrics.sqlite3"

collector_failure_root="${TEST_HOME}/collector-removal-failure"
create_fixture "${collector_failure_root}"
rm -f "${collector_failure_root}/etc/systemd/system/gost-monitor-collector.service"
touch "${STUB_STATE_DIR}/active"
if STUB_FAIL_SYSTEMCTL_ACTION=disable STUB_FAIL_SYSTEMCTL_UNIT=gost-monitor-collector.service \
  run_plan "${collector_failure_root}" monitor-service monitor-code monitor-config history >/dev/null; then
  fail "collector removal failure returns non-zero"
else
  pass "collector removal failure returns non-zero"
fi
assert_file "collector failure preserves query launcher" "${collector_failure_root}/usr/local/sbin/gost-monitor"
assert_file "collector failure preserves admin launcher" "${collector_failure_root}/usr/local/sbin/gost-monitor-admin"
assert_file "collector failure preserves collector launcher" "${collector_failure_root}/usr/local/sbin/gost-monitor-collector"
assert_dir "collector failure preserves Python code" "${collector_failure_root}/usr/local/lib/gost-manager/monitoring"
assert_file "collector failure preserves config" "${collector_failure_root}/etc/gost-manager/monitoring.env"
assert_file "collector failure preserves history" "${collector_failure_root}/var/lib/gost-manager/metrics.sqlite3"
assert_file "collector failure leaves traffic untouched" "${collector_failure_root}/etc/systemd/system/gost-iran-1.service"

custom_history_root="${TEST_HOME}/custom-history"
create_fixture "${custom_history_root}"
configure_custom_history "${custom_history_root}"
run_plan "${custom_history_root}" history >/dev/null
custom_rows="$(PYTHONPATH="${ROOT_DIR}" python3 - "${custom_history_root}/var/lib/gost-manager/archive/custom.sqlite3" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
print(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
conn.close()
PY
)"
default_rows="$(PYTHONPATH="${ROOT_DIR}" python3 - "${custom_history_root}/var/lib/gost-manager/metrics.sqlite3" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
print(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
conn.close()
PY
)"
assert_eq "history-only purges alternate configured database" "0" "${custom_rows}"
assert_eq "history-only does not purge wrong default database" "1" "${default_rows}"

captured_history_root="${TEST_HOME}/captured-history"
create_fixture "${captured_history_root}"
configure_custom_history "${captured_history_root}"
run_plan "${captured_history_root}" monitor-service history monitor-config >/dev/null
assert_absent "config removal plus history uses captured custom path" "${captured_history_root}/var/lib/gost-manager/archive/custom.sqlite3"
assert_absent "config removal removes config after path capture" "${captured_history_root}/etc/gost-manager/monitoring.env"
assert_file "captured custom removal leaves wrong default database" "${captured_history_root}/var/lib/gost-manager/metrics.sqlite3"

invalid_history_root="${TEST_HOME}/invalid-history-config"
create_fixture "${invalid_history_root}"
printf 'invalid config\n' > "${invalid_history_root}/etc/gost-manager/monitoring.env"
invalid_history_before="$(tree_digest "${invalid_history_root}/var/lib/gost-manager")"
if run_plan "${invalid_history_root}" history >/dev/null 2>&1; then
  fail "invalid config refuses history deletion"
else
  pass "invalid config refuses history deletion"
fi
assert_eq "invalid config never deletes guessed default history" "${invalid_history_before}" "$(tree_digest "${invalid_history_root}/var/lib/gost-manager")"

symlink_root="${TEST_HOME}/symlink"
create_fixture "${symlink_root}"
outside="${TEST_HOME}/outside-monitoring"
printf 'outside-safe\n' > "${outside}"
rm -rf "${symlink_root}/usr/local/lib/gost-manager/monitoring"
ln -s "${outside}" "${symlink_root}/usr/local/lib/gost-manager/monitoring"
rm -f "${symlink_root}/etc/systemd/system/gost-monitor-collector.service"
if run_plan "${symlink_root}" monitor-code >/dev/null 2>&1; then
  fail "uninstall rejects symlinked monitoring code"
else
  pass "uninstall rejects symlinked monitoring code"
fi
assert_eq "uninstall symlink target unchanged" "outside-safe" "$(tr -d '\n' < "${outside}")"

finish_suite
