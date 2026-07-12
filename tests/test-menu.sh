#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=integration-test-lib.sh
source "${ROOT_DIR}/tests/integration-test-lib.sh"

TEST_HOME="$(mktemp -d "${TMPDIR:-/tmp}/gost-menu-tests.XXXXXX")"
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

cat > "${STUB_BIN}/gost-monitor" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'monitor %s\n' "$*" >> "${COMMAND_LOG}"
exit "${STUB_MONITOR_EXIT:-0}"
STUB
cat > "${STUB_BIN}/gost-monitor-collector" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'collector %s\n' "$*" >> "${COMMAND_LOG}"
exit "${STUB_COLLECTOR_EXIT:-0}"
STUB
cat > "${STUB_BIN}/gost-monitor-admin" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'admin %s\n' "$*" >> "${COMMAND_LOG}"
exit "${STUB_ADMIN_EXIT:-0}"
STUB
chmod 755 "${STUB_BIN}/gost-monitor" "${STUB_BIN}/gost-monitor-collector" "${STUB_BIN}/gost-monitor-admin"

export COMMAND_LOG STUB_STATE_DIR
export PATH="${STUB_BIN}:${PATH}"
export GOST_MANAGER_TESTING=1
export GOST_MONITOR_BIN_TEST="${STUB_BIN}/gost-monitor"
export GOST_MONITOR_COLLECTOR_BIN_TEST="${STUB_BIN}/gost-monitor-collector"
export GOST_MONITOR_ADMIN_BIN_TEST="${STUB_BIN}/gost-monitor-admin"
export GOST_MONITOR_CONFIG_TEST="${TEST_HOME}/monitoring.env"
export GOST_MONITOR_DB_TEST="${TEST_HOME}/metrics.sqlite3"
export GOST_MONITOR_EXPORT_DIR_TEST="${TEST_HOME}"

# shellcheck source=../gost-manager.sh
source "${ROOT_DIR}/gost-manager.sh"

menu_file="${TEST_HOME}/menu.txt"
show_menu > "${menu_file}"
for label in \
  "1) Install / Update GOST" \
  "2) Create Kharej tunnel" \
  "3) Create Iran tunnel" \
  "4) Delete tunnel" \
  "5) Show status" \
  "6) Show logs" \
  "7) Restart tunnel" \
  "8) List active GOST services" \
  "9) Clean old/broken GOST configs"; do
  assert_contains "legacy menu label ${label%%)*}" "${label}" "${menu_file}"
done
assert_contains "Monitoring appended as option 10" "10) Monitoring" "${menu_file}"
assert_contains "Native placeholder appended as option 11" "11) Native GOST Gateway (Coming soon)" "${menu_file}"

: > "${COMMAND_LOG}"
export STUB_MONITOR_EXIT=3
failure_output="${TEST_HOME}/failure.out"
monitor_query snapshot > "${failure_output}"
assert_contains "monitor failure returns useful message" "exit code 3" "${failure_output}"
assert_contains "monitor failure called read-only CLI" "monitor --config ${MONITOR_CONFIG} snapshot" "${COMMAND_LOG}"
assert_not_contains "monitor failure never targets traffic" "gost-iran-" "${COMMAND_LOG}"

export STUB_MONITOR_EXIT=130
live_output="${TEST_HOME}/live.out"
monitor_query live > "${live_output}"
assert_contains "Ctrl-C live returns to menu" "Monitoring view closed" "${live_output}"
unset STUB_MONITOR_EXIT

audit_root="${TEST_HOME}/native-audit"
mkdir -p "${audit_root}"
printf 'unchanged\n' > "${audit_root}/canary"
tree_before="$(tree_digest "${audit_root}")"
log_before="$(cksum "${COMMAND_LOG}")"
native_output="${TEST_HOME}/native.out"
native_gost_gateway_coming_soon > "${native_output}"
assert_eq "Native placeholder filesystem no-op" "${tree_before}" "$(tree_digest "${audit_root}")"
assert_eq "Native placeholder command no-op" "${log_before}" "$(cksum "${COMMAND_LOG}")"
assert_contains "Native placeholder prints Coming soon" "Coming soon" "${native_output}"

require_root() { return 0; }
: > "${COMMAND_LOG}"
printf 'CANCEL\n' | monitor_purge_history >/dev/null
assert_eq "purge cancellation calls no service" "0" "$(wc -l < "${COMMAND_LOG}" | tr -d ' ')"

DISPATCH_LOG="${TEST_HOME}/dispatch.log"
: > "${DISPATCH_LOG}"
install_or_update_gost() { printf 'install\n' >> "${DISPATCH_LOG}"; }
create_kharej_tunnel() { printf 'kharej\n' >> "${DISPATCH_LOG}"; }
create_iran_tunnel() { printf 'iran\n' >> "${DISPATCH_LOG}"; }
delete_tunnel() { printf 'delete\n' >> "${DISPATCH_LOG}"; }
show_status() { printf 'status\n' >> "${DISPATCH_LOG}"; }
show_logs() { printf 'logs\n' >> "${DISPATCH_LOG}"; }
restart_tunnel() { printf 'restart\n' >> "${DISPATCH_LOG}"; }
list_active_gost_services() { printf 'list\n' >> "${DISPATCH_LOG}"; }
clean_old_broken_configs() { printf 'cleanup\n' >> "${DISPATCH_LOG}"; }
native_gost_gateway_coming_soon() { printf 'native\n' >> "${DISPATCH_LOG}"; }
(main_menu <<< $'1\n2\n3\n4\n5\n6\n7\n8\n9\n0' >/dev/null)
assert_eq "legacy dispatch order unchanged" $'install\nkharej\niran\ndelete\nstatus\nlogs\nrestart\nlist\ncleanup' "$(sed -n '1,9p' "${DISPATCH_LOG}")"
assert_contains "main option 10 dispatches Monitoring" '10) monitoring_menu ;;' "${ROOT_DIR}/gost-manager.sh"
assert_contains "main option 11 dispatches Native placeholder" '11) native_gost_gateway_coming_soon ;;' "${ROOT_DIR}/gost-manager.sh"

: > "${DISPATCH_LOG}"
monitor_query() { printf 'query %s\n' "$*" >> "${DISPATCH_LOG}"; }
monitor_custom_summary() { printf 'custom\n' >> "${DISPATCH_LOG}"; }
monitor_service_detail() { printf 'service-detail\n' >> "${DISPATCH_LOG}"; }
monitor_tunnel_detail() { printf 'tunnel-detail\n' >> "${DISPATCH_LOG}"; }
monitor_recent_events() { printf 'events\n' >> "${DISPATCH_LOG}"; }
monitor_export() { printf 'export-%s\n' "$1" >> "${DISPATCH_LOG}"; }
monitoring_service_status() { printf 'service-status\n' >> "${DISPATCH_LOG}"; }
monitoring_service_action() { printf 'service-%s\n' "$1" >> "${DISPATCH_LOG}"; }
monitor_one_shot() { printf 'diagnostic\n' >> "${DISPATCH_LOG}"; }
monitor_maintenance() { printf 'maintenance\n' >> "${DISPATCH_LOG}"; }
monitor_purge_history() { printf 'purge\n' >> "${DISPATCH_LOG}"; }
monitoring_menu <<< $'1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n20\n21\n0' >/dev/null
assert_eq "Monitoring submenu dispatch count" "21" "$(wc -l < "${DISPATCH_LOG}" | tr -d ' ')"
for marker in \
  "query snapshot" "query live" "query summary --window 10m" \
  "query summary --window 30m" "query summary --window 1h" "custom" \
  "query host --window 30m" "query network --window 30m" "service-detail" \
  "tunnel-detail" "query collector --window 1h" "events" "export-json" \
  "export-csv" "service-status" "service-start" "service-stop" \
  "service-restart" "diagnostic" "maintenance" "purge"; do
  assert_contains "Monitoring dispatch ${marker}" "${marker}" "${DISPATCH_LOG}"
done

finish_suite
