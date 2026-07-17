#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CURRENT_VERSION="$(< "${ROOT_DIR}/VERSION")"
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
if [[ "${1:-}" == "config" ]]; then
  [[ "${STUB_CONFIG_EXIT:-0}" == "0" ]] || exit "${STUB_CONFIG_EXIT}"
  printf '%s\n' "${STUB_CONFIG_DB}"
  exit 0
fi
exit "${STUB_ADMIN_EXIT:-0}"
STUB
cat > "${STUB_BIN}/gost-watchdog-admin" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'watchdog-admin %s\n' "$*" >> "${COMMAND_LOG}"
if [[ "${1:-}" == "profiles" ]]; then
  printf 'iran-1\t203.0.113.10\tdisabled\n'
elif [[ "${1:-}" == "effective" && "${3:-}" == "--json" ]]; then
  printf '%s\n' "${STUB_WATCHDOG_PROFILE_JSON:-}"
elif [[ "${1:-}" == "effective-global" && "${2:-}" == "--json" ]]; then
  printf '%s\n' "${STUB_WATCHDOG_GLOBAL_JSON:-}"
elif [[ "${1:-}" == "status" && "${2:-}" == "--profile" ]]; then
  printf '%s\n' "${STUB_WATCHDOG_STATUS_JSON:-}"
fi
exit "${STUB_WATCHDOG_EXIT:-0}"
STUB
chmod 755 "${STUB_BIN}/gost-monitor" "${STUB_BIN}/gost-monitor-collector" "${STUB_BIN}/gost-monitor-admin" "${STUB_BIN}/gost-watchdog-admin"

export COMMAND_LOG STUB_STATE_DIR
export PATH="${STUB_BIN}:${PATH}"
export GOST_MANAGER_TESTING=1
export GOST_MONITOR_BIN_TEST="${STUB_BIN}/gost-monitor"
export GOST_MONITOR_COLLECTOR_BIN_TEST="${STUB_BIN}/gost-monitor-collector"
export GOST_MONITOR_ADMIN_BIN_TEST="${STUB_BIN}/gost-monitor-admin"
export GOST_WATCHDOG_ADMIN_BIN_TEST="${STUB_BIN}/gost-watchdog-admin"
export GOST_MONITOR_CONFIG_TEST="${TEST_HOME}/monitoring.env"
export GOST_MONITOR_EXPORT_DIR_TEST="${TEST_HOME}"
export STUB_CONFIG_DB="/var/lib/gost-manager/custom.sqlite3"
export STUB_WATCHDOG_PROFILE_JSON='{"mode":"monitor","check_interval_seconds":7,"ping_timeout_seconds":3,"failure_threshold":17,"success_threshold":19,"recovery_hold_seconds":23,"recovery_jitter_max_seconds":29}'
export STUB_WATCHDOG_GLOBAL_JSON='{"check_mode":"ping","check_interval_seconds":11,"ping_timeout_seconds":4,"failure_threshold":31,"success_threshold":37,"recovery_hold_seconds":41,"recovery_jitter_max_seconds":43}'
export STUB_WATCHDOG_STATUS_JSON='{"errors":[],"profiles":[{"mode":"monitor","stopped_by_watchdog":false,"watchdog_state":"healthy","check_status":"success"}]}'

# shellcheck source=../gost-manager.sh
source "${ROOT_DIR}/gost-manager.sh"

menu_file="${TEST_HOME}/menu.txt"
show_menu > "${menu_file}"
assert_contains "main menu displays release version" "GOST Manager v${CURRENT_VERSION}" "${menu_file}"
assert_eq "version command displays release version" "GOST Manager v${CURRENT_VERSION}" "$(GOST_MANAGER_TESTING=0 bash "${ROOT_DIR}/gost-manager.sh" --version)"
GOST_MANAGER_VERSION_FILE_TEST="${TEST_HOME}/missing-version" assert_eq \
  "missing version has a safe fallback" "GOST Manager version unknown" "$(GOST_MANAGER_VERSION_FILE_TEST="${TEST_HOME}/missing-version" manager_banner)"
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
assert_contains "Server Stability appended as option 11" "11) Server Stability" "${menu_file}"
assert_contains "Upstream Watchdog appended as option 12" "12) Upstream Watchdog" "${menu_file}"
assert_not_contains "Native Gateway placeholder removed" "Coming soon" "${menu_file}"

profile_menu_file="${TEST_HOME}/profile-menu.txt"
show_direct_profiles_menu > "${profile_menu_file}"
for label in \
  "1) List all profiles" \
  "2) Show profile detail" \
  "3) Edit a profile" \
  "4) Clone a profile" \
  "5) Restart selected profiles" \
  "6) Restart all profiles" \
  "0) Back"; do
  assert_contains "Direct profile menu ${label%%)*}" "${label}" "${profile_menu_file}"
done
assert_not_contains "Direct profile menu has no Gateway action" "Gateway" "${profile_menu_file}"

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

: > "${COMMAND_LOG}"
status_output="${TEST_HOME}/status.out"
monitoring_service_status > "${status_output}"
assert_contains "configured database shown in status" "History: ${STUB_CONFIG_DB}" "${status_output}"
assert_contains "status resolves database through admin config" "admin config --format value --field database_path --config ${MONITOR_CONFIG}" "${COMMAND_LOG}"

require_root() { return 0; }
touch "${STUB_STATE_DIR}/active"
: > "${COMMAND_LOG}"
printf 'y\n' | monitor_one_shot >/dev/null
assert_contains "one-shot stops only active collector" "systemctl stop gost-monitor-collector.service" "${COMMAND_LOG}"
assert_contains "one-shot invokes collector once" "collector --once" "${COMMAND_LOG}"
assert_contains "one-shot restores active collector" "systemctl start gost-monitor-collector.service" "${COMMAND_LOG}"
assert_not_contains "one-shot never targets traffic" "gost-iran-" "${COMMAND_LOG}"
rm -f "${STUB_STATE_DIR}/active"

touch "${STUB_STATE_DIR}/active"
export STUB_COLLECTOR_EXIT=1
: > "${COMMAND_LOG}"
printf 'y\n' | monitor_one_shot >/dev/null
assert_contains "failed one-shot still restores collector" "systemctl start gost-monitor-collector.service" "${COMMAND_LOG}"
assert_not_contains "failed one-shot still avoids traffic" "gost-kharej-" "${COMMAND_LOG}"
unset STUB_COLLECTOR_EXIT
rm -f "${STUB_STATE_DIR}/active"

touch "${STUB_STATE_DIR}/active"
export STUB_COLLECTOR_EXIT=130
: > "${COMMAND_LOG}"
printf 'y\n' | monitor_one_shot >/dev/null
assert_contains "interrupted one-shot restores collector" "systemctl start gost-monitor-collector.service" "${COMMAND_LOG}"
assert_not_contains "interrupted one-shot avoids traffic" "gost-iran-" "${COMMAND_LOG}"
unset STUB_COLLECTOR_EXIT
rm -f "${STUB_STATE_DIR}/active"

: > "${COMMAND_LOG}"
purge_output="${TEST_HOME}/configured-purge.out"
printf 'DELETE MONITORING HISTORY\n' | monitor_purge_history > "${purge_output}"
assert_contains "purge displays exact configured database" "${STUB_CONFIG_DB}" "${purge_output}"
assert_contains "purge uses strict config resolution" "admin purge-history --yes --config ${MONITOR_CONFIG}" "${COMMAND_LOG}"
assert_not_contains "configured purge never targets traffic" "gost-iran-" "${COMMAND_LOG}"

export STUB_CONFIG_EXIT=2
: > "${COMMAND_LOG}"
invalid_output="${TEST_HOME}/invalid-config.out"
printf 'DELETE MONITORING HISTORY\n' | monitor_purge_history > "${invalid_output}" 2>&1
assert_contains "invalid config refuses purge" "no database action was taken" "${invalid_output}"
assert_not_contains "invalid config runs no purge" "purge-history" "${COMMAND_LOG}"
unset STUB_CONFIG_EXIT

: > "${COMMAND_LOG}"
printf 'CANCEL\n' | monitor_purge_history >/dev/null
assert_eq "purge cancellation resolves config only" "1" "$(wc -l < "${COMMAND_LOG}" | tr -d ' ')"
assert_not_contains "purge cancellation calls no service" "systemctl" "${COMMAND_LOG}"
assert_not_contains "purge cancellation runs no purge" "purge-history" "${COMMAND_LOG}"

DISPATCH_LOG="${TEST_HOME}/dispatch.log"
: > "${DISPATCH_LOG}"
dispatch_stubs="${TEST_HOME}/dispatch-stubs.sh"
cat > "${dispatch_stubs}" <<'STUB'
install_or_update_gost() { printf 'install\n' >> "${DISPATCH_LOG}"; }
create_kharej_tunnel() { printf 'kharej\n' >> "${DISPATCH_LOG}"; }
create_iran_tunnel() { printf 'iran\n' >> "${DISPATCH_LOG}"; }
delete_tunnel() { printf 'delete\n' >> "${DISPATCH_LOG}"; }
show_status() { printf 'status\n' >> "${DISPATCH_LOG}"; }
show_logs() { printf 'logs\n' >> "${DISPATCH_LOG}"; }
restart_tunnel() { printf 'restart\n' >> "${DISPATCH_LOG}"; }
list_active_gost_services() { printf 'list\n' >> "${DISPATCH_LOG}"; }
clean_old_broken_configs() { printf 'cleanup\n' >> "${DISPATCH_LOG}"; }
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
server_stability_wizard() { printf 'stability\n' >> "${DISPATCH_LOG}"; }
STUB
# shellcheck source=/dev/null
source "${dispatch_stubs}"
(
  monitoring_menu() { printf 'monitoring\n' >> "${DISPATCH_LOG}"; }
  watchdog_menu() { printf 'watchdog\n' >> "${DISPATCH_LOG}"; }
  main_menu <<< $'1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n0' >/dev/null
)
assert_eq "main dispatch order 1 through 12 exact" $'install\nkharej\niran\ndelete\nstatus\nlogs\nrestart\nlist\ncleanup\nmonitoring\nstability\nwatchdog' "$(sed -n '1,12p' "${DISPATCH_LOG}")"
assert_contains "main option 10 dispatches Monitoring" '10) monitoring_menu ;;' "${ROOT_DIR}/gost-manager.sh"
assert_contains "main option 11 dispatches Server Stability" '11) server_stability_wizard ;;' "${ROOT_DIR}/gost-manager.sh"
assert_contains "main option 12 dispatches Upstream Watchdog" '12) watchdog_menu ;;' "${ROOT_DIR}/gost-manager.sh"
assert_not_contains "main dispatch has no Native option" 'native_gost_gateway_coming_soon' "${ROOT_DIR}/gost-manager.sh"

: > "${DISPATCH_LOG}"
stability_return_output="${TEST_HOME}/stability-return.out"
(
  main_menu <<< $'11\n0'
) > "${stability_return_output}"
assert_eq "Server Stability returns to the main menu" "2" \
  "$(grep -c "^GOST Manager v${CURRENT_VERSION}$" "${stability_return_output}")"
assert_contains "Server Stability dispatch completes before return" "stability" "${DISPATCH_LOG}"

watchdog_menu_file="${TEST_HOME}/watchdog-menu.txt"
show_watchdog_menu > "${watchdog_menu_file}"
for label in \
  "1) Show all profile status" \
  "2) Enable or change profile mode" \
  "3) Disable watchdog for profile" \
  "4) Configure profile overrides" \
  "5) Reset profile overrides to global defaults" \
  "6) Test profile ping" \
  "7) Maintenance mode" \
  "8) Show last 24-hour events" \
  "9) Show 24-hour outage summary" \
  "10) Configure global defaults" \
  "11) Show watchdog service status" \
  "12) Restart watchdog service" \
  "13) Re-arm manual override" \
  "14) Back"; do
  assert_contains "Watchdog menu ${label}" "${label}" "${watchdog_menu_file}"
done
assert_contains "Watchdog menu displays exact default interval" "Default Ping interval: 2 seconds" "${watchdog_menu_file}"

: > "${COMMAND_LOG}"
watchdog_configure_profile <<< $'iran-1\n\n\n\n\n\n\n' >/dev/null
assert_contains "profile menu reads machine-readable current values" \
  "watchdog-admin effective iran-1 --json" "${COMMAND_LOG}"
assert_not_contains "Enter preserves all custom profile values without a write" \
  "configure-profile" "${COMMAND_LOG}"

: > "${COMMAND_LOG}"
watchdog_configure_profile <<< $'iran-1\n8\n\n\n\n\n\ny' >/dev/null
assert_contains "changing one profile value writes only that override" \
  "watchdog-admin configure-profile iran-1 --check-interval 8" "${COMMAND_LOG}"
assert_not_contains "single profile change does not expand inherited timeout" \
  "--ping-timeout" "${COMMAND_LOG}"
assert_not_contains "single profile change does not reset custom threshold" \
  "--failure-threshold" "${COMMAND_LOG}"

: > "${COMMAND_LOG}"
watchdog_configure_global <<< $'\n\n\n\n\n\n' >/dev/null
assert_contains "global menu reads machine-readable current values" \
  "watchdog-admin effective-global --json" "${COMMAND_LOG}"
assert_not_contains "Enter preserves all custom global values without a write" \
  "set-global" "${COMMAND_LOG}"

: > "${COMMAND_LOG}"
watchdog_configure_global <<< $'\n\n32\n\n\n\ny' >/dev/null
assert_contains "changing one global value writes only that field" \
  "watchdog-admin set-global --failure-threshold 32" "${COMMAND_LOG}"
assert_not_contains "single global change does not reset custom interval" \
  "--check-interval" "${COMMAND_LOG}"

: > "${COMMAND_LOG}"
export STUB_WATCHDOG_PROFILE_JSON='{'
watchdog_configure_profile <<< $'iran-1' >/dev/null 2>&1 || true
assert_not_contains "invalid profile JSON causes no configuration write" \
  "configure-profile" "${COMMAND_LOG}"
export STUB_WATCHDOG_PROFILE_JSON='{"mode":"monitor","check_interval_seconds":7,"ping_timeout_seconds":3,"failure_threshold":17,"success_threshold":19,"recovery_hold_seconds":23,"recovery_jitter_max_seconds":29}'

: > "${COMMAND_LOG}"
export STUB_WATCHDOG_GLOBAL_JSON='{"check_mode":"ping"}'
watchdog_configure_global </dev/null >/dev/null 2>&1 || true
assert_not_contains "incomplete global JSON causes no configuration write" \
  "set-global" "${COMMAND_LOG}"
export STUB_WATCHDOG_GLOBAL_JSON='{"check_mode":"ping","check_interval_seconds":11,"ping_timeout_seconds":4,"failure_threshold":31,"success_threshold":37,"recovery_hold_seconds":41,"recovery_jitter_max_seconds":43}'

: > "${COMMAND_LOG}"
export STUB_WATCHDOG_STATUS_JSON='{"errors":[],"profiles":[{"mode":"auto","stopped_by_watchdog":true,"watchdog_state":"healthy","check_status":"success"}]}'
watchdog_apply_mode_change iran-1 monitor <<< $'1\ny' >/dev/null
assert_contains "mode change can explicitly keep Watchdog-owned stop" \
  "watchdog-admin set-mode iran-1 monitor --owned-action keep-stopped" "${COMMAND_LOG}"

: > "${COMMAND_LOG}"
watchdog_apply_mode_change iran-1 disabled <<< $'2\ny' >/dev/null
assert_contains "mode change can explicitly start a healthy owned stop" \
  "watchdog-admin set-mode iran-1 disabled --owned-action start-if-healthy" "${COMMAND_LOG}"

: > "${COMMAND_LOG}"
watchdog_apply_mode_change iran-1 disabled <<< $'3' >/dev/null
assert_not_contains "mode-change cancellation performs no write or service action" \
  "set-mode" "${COMMAND_LOG}"
export STUB_WATCHDOG_STATUS_JSON='{"errors":[],"profiles":[{"mode":"monitor","stopped_by_watchdog":false,"watchdog_state":"healthy","check_status":"success"}]}'

watchdog_dispatch_stubs="${TEST_HOME}/watchdog-dispatch-stubs.sh"
cat > "${watchdog_dispatch_stubs}" <<'STUB'
run_watchdog_command() { printf 'watchdog-command %s\n' "$*" >> "${DISPATCH_LOG}"; }
watchdog_change_mode() { printf 'watchdog-mode\n' >> "${DISPATCH_LOG}"; }
watchdog_disable_profile() { printf 'watchdog-disable\n' >> "${DISPATCH_LOG}"; }
watchdog_configure_profile() { printf 'watchdog-profile-config\n' >> "${DISPATCH_LOG}"; }
watchdog_reset_profile() { printf 'watchdog-reset\n' >> "${DISPATCH_LOG}"; }
watchdog_test_ping() { printf 'watchdog-ping\n' >> "${DISPATCH_LOG}"; }
watchdog_maintenance_menu() { printf 'watchdog-maintenance\n' >> "${DISPATCH_LOG}"; }
watchdog_configure_global() { printf 'watchdog-global-config\n' >> "${DISPATCH_LOG}"; }
watchdog_service_status() { printf 'watchdog-service-status\n' >> "${DISPATCH_LOG}"; }
watchdog_restart_service() { printf 'watchdog-restart\n' >> "${DISPATCH_LOG}"; }
watchdog_rearm_profile() { printf 'watchdog-rearm\n' >> "${DISPATCH_LOG}"; }
STUB
# shellcheck source=/dev/null
source "${watchdog_dispatch_stubs}"
: > "${DISPATCH_LOG}"
watchdog_menu <<< $'1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14' >/dev/null
assert_eq "Watchdog submenu dispatch count" "13" "$(wc -l < "${DISPATCH_LOG}" | tr -d ' ')"
for marker in \
  "watchdog-command status" "watchdog-mode" "watchdog-disable" \
  "watchdog-profile-config" "watchdog-reset" "watchdog-ping" \
  "watchdog-maintenance" "watchdog-command events --limit 200" \
  "watchdog-command summary" "watchdog-global-config" \
  "watchdog-service-status" "watchdog-restart" "watchdog-rearm"; do
  assert_contains "Watchdog dispatch ${marker}" "${marker}" "${DISPATCH_LOG}"
done

lite_menu_file="${TEST_HOME}/monitoring-lite-menu.txt"
show_monitoring_menu > "${lite_menu_file}"
for label in \
  "1) Live resources" \
  "2) Last 10 minutes" \
  "3) Last 30 minutes" \
  "4) Last 1 hour" \
  "5) Services and tunnels" \
  "6) Collector status" \
  "7) Advanced tools" \
  "0) Back"; do
  assert_contains "Monitoring Lite menu ${label}" "${label}" "${lite_menu_file}"
done
assert_not_contains "Monitoring Lite menu hides exports" "Export JSON" "${lite_menu_file}"
assert_not_contains "Monitoring Lite menu hides maintenance" "maintenance" "${lite_menu_file}"

: > "${DISPATCH_LOG}"
normal_menu_output="${TEST_HOME}/normal-monitoring-menu.out"
monitoring_menu <<< $'1\n2\n3\n4\n5\n0\n6\n7\n0\n0' > "${normal_menu_output}"
assert_eq "Monitoring Lite command dispatch count" "5" "$(wc -l < "${DISPATCH_LOG}" | tr -d ' ')"
for marker in \
  "query live" "query summary --window 10m" \
  "query summary --window 30m" "query summary --window 1h" \
  "service-status"; do
  assert_contains "Monitoring Lite dispatch ${marker}" "${marker}" "${DISPATCH_LOG}"
done
assert_contains "Monitoring Lite opens services submenu" "Services and tunnels" "${normal_menu_output}"
assert_contains "Monitoring Lite opens advanced submenu" "Advanced tools" "${normal_menu_output}"

: > "${DISPATCH_LOG}"
services_and_tunnels_menu <<< $'1\n2\n3\n4\n0' >/dev/null
assert_eq "Services and tunnels dispatch count" "4" "$(wc -l < "${DISPATCH_LOG}" | tr -d ' ')"
for marker in "query services --window 10m" "service-detail" \
  "query tunnels --window 10m" "tunnel-detail"; do
  assert_contains "Services and tunnels dispatch ${marker}" "${marker}" "${DISPATCH_LOG}"
done

: > "${DISPATCH_LOG}"
monitoring_advanced_menu <<< $'1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n13\n14\n0' >/dev/null
assert_eq "Advanced tools dispatch count" "14" "$(wc -l < "${DISPATCH_LOG}" | tr -d ' ')"
for marker in \
  "query snapshot" "query host --window 30m" "query network --window 30m" \
  "query collector --window 1h" "events" "custom" "export-json" \
  "export-csv" "diagnostic" "maintenance" "purge" "service-start" \
  "service-stop" "service-restart"; do
  assert_contains "Advanced tools dispatch ${marker}" "${marker}" "${DISPATCH_LOG}"
done

finish_suite
