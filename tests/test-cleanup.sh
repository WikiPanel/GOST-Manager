#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=integration-test-lib.sh
source "${ROOT_DIR}/tests/integration-test-lib.sh"

TEST_HOME="$(mktemp -d "${TMPDIR:-/tmp}/gost-cleanup-tests.XXXXXX")"
cleanup_test_home() {
  local status=$?
  rm -rf "${TEST_HOME}"
  exit "${status}"
}
trap cleanup_test_home EXIT

ENV_DIR="${TEST_HOME}/etc/gost"
UNIT_DIR="${TEST_HOME}/systemd"
STUB_BIN="${TEST_HOME}/bin"
COMMAND_LOG="${TEST_HOME}/commands.log"
SYSTEMD_STATE_DIR="${TEST_HOME}/systemd-state"
mkdir -p "${ENV_DIR}" "${UNIT_DIR}" "${STUB_BIN}" "${SYSTEMD_STATE_DIR}"

# State per unit: LoadState|UnitFileState|ActiveState|MainPID
cat > "${STUB_BIN}/systemctl" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'systemctl %s\n' "$*" >> "${COMMAND_LOG}"
action="${1:-}"
service=""
for value in "$@"; do
  [[ "${value}" == *.service ]] && service="${value}"
done
state_path="${SYSTEMD_STATE_DIR}/${service}.state"
if [[ -n "${service}" && ! -f "${state_path}" ]]; then
  printf 'not-found|none|inactive|0\n' > "${state_path}"
fi
if [[ -n "${service}" ]]; then
  IFS='|' read -r load_state unit_state active_state main_pid < "${state_path}"
fi
write_state() { printf '%s|%s|%s|%s\n' "${load_state}" "${unit_state}" "${active_state}" "${main_pid}" > "${state_path}"; }
case "${action}" in
  show)
    printf 'LoadState=%s\nUnitFileState=%s\nActiveState=%s\nMainPID=%s\n' "${load_state}" "${unit_state}" "${active_state}" "${main_pid}"
    ;;
  is-active)
    printf '%s\n' "${active_state}"
    [[ "${active_state}" == "active" ]]
    ;;
  is-enabled)
    printf '%s\n' "${unit_state}"
    [[ "${unit_state}" == "enabled" ]]
    ;;
  disable)
    [[ "${STUB_FAIL_DISABLE:-0}" != "1" ]] || exit 1
    unit_state="disabled"
    if [[ " $* " == *" --now "* && "${active_state}" != "failed" ]]; then active_state="inactive"; main_pid=0; fi
    write_state
    ;;
  reset-failed)
    [[ -n "${service}" ]] || { printf 'global reset-failed is forbidden\n' >&2; exit 1; }
    if [[ "${active_state}" == "failed" ]]; then active_state="inactive"; main_pid=0; fi
    write_state
    ;;
  daemon-reload) ;;
  *) exit 1 ;;
esac
STUB
chmod 755 "${STUB_BIN}/systemctl"

export COMMAND_LOG SYSTEMD_STATE_DIR
export PATH="${STUB_BIN}:${PATH}"
export GOST_MANAGER_TESTING=1
export GOST_ETC_DIR_TEST="${ENV_DIR}"
export GOST_SYSTEMD_DIR_TEST="${UNIT_DIR}"
# shellcheck source=../gost-manager.sh
source "${ROOT_DIR}/gost-manager.sh"
require_root() { return 0; }

write_profile() {
  local side="$1" number="$2" state="$3"
  printf 'GOST_USER=user-%s\nGOST_PASS=cleanup-canary-%s\nTUNNEL_PORT=%s\n' "${number}" "${number}" "$((28000 + number))" > "${ENV_DIR}/${side}-${number}.env"
  chmod 600 "${ENV_DIR}/${side}-${number}.env"
  printf '[Service]\n' > "${UNIT_DIR}/gost-${side}-${number}.service"
  printf '%s\n' "${state}" > "${SYSTEMD_STATE_DIR}/gost-${side}-${number}.service.state"
}

write_unit_only() {
  local side="$1" number="$2" state="$3"
  printf '[Service]\n' > "${UNIT_DIR}/gost-${side}-${number}.service"
  printf '%s\n' "${state}" > "${SYSTEMD_STATE_DIR}/gost-${side}-${number}.service.state"
}

run_cleanup() {
  local output="$1"
  local answer="${2:-y}"
  local status=0
  : > "${COMMAND_LOG}"
  (clean_old_broken_configs <<< "${answer}") > "${output}" 2>&1 || status=$?
  grep -E 'systemctl (disable|reset-failed|stop|start|restart)' "${COMMAND_LOG}" > "${COMMAND_LOG}.lifecycle" || true
  return "${status}"
}

# Valid profiles in every non-running state are kept with their credentials.
write_profile iran 1 'loaded|enabled|active|101'
write_profile iran 2 'loaded|enabled|failed|0'
write_profile kharej 3 'loaded|disabled|inactive|0'
write_profile kharej 4 'loaded|disabled|failed|0'
# Names that only look managed are not ours and must survive.
printf '[Service]\n' > "${UNIT_DIR}/gost-kharej-edge.service"
printf '[Service]\n' > "${UNIT_DIR}/gost-iran-0.service"
printf '[Service]\n' > "${UNIT_DIR}/gost.service"
printf 'GOST_USER=x\n' > "${ENV_DIR}/iran-backup.env"
printf 'services: []\n' > "${ENV_DIR}/gost.yaml.bak.1"
# True orphans and managed backups.
write_unit_only iran 5 'loaded|enabled|inactive|0'
write_unit_only kharej 6 'loaded|enabled|failed|0'
write_unit_only iran 7 'loaded|enabled|active|107'
printf 'GOST_USER=orphan\nGOST_PASS=orphan-canary\n' > "${ENV_DIR}/kharej-8.env"
printf 'old\n' > "${ENV_DIR}/iran-1.env.bak.20250101120000"
printf 'old\n' > "${UNIT_DIR}/gost-iran-1.service.bak.20250101120000"

declined="${TEST_HOME}/declined.out"
if run_cleanup "${declined}" n; then fail "declined cleanup returns nonzero"; else pass "declined cleanup returns nonzero"; fi
assert_file "declined cleanup keeps orphan unit" "${UNIT_DIR}/gost-iran-5.service"
assert_file "declined cleanup keeps orphan env" "${ENV_DIR}/kharej-8.env"
assert_not_contains "declined cleanup sends no lifecycle command" "disable" "${COMMAND_LOG}"

output="${TEST_HOME}/cleanup.out"
if run_cleanup "${output}" y; then pass "cleanup succeeds"; else fail "cleanup succeeds"; cat "${output}"; fi

for profile in iran-1 iran-2 kharej-3 kharej-4; do
  assert_file "cleanup keeps ${profile} env" "${ENV_DIR}/${profile}.env"
  assert_file "cleanup keeps ${profile} unit" "${UNIT_DIR}/gost-${profile}.service"
  assert_not_contains "cleanup sends no lifecycle command to ${profile}" "gost-${profile}.service" "${COMMAND_LOG}.lifecycle"
done
assert_contains "failed profile is reported for attention" "failed profile: ${UNIT_DIR}/gost-iran-2.service" "${output}"
assert_contains "disabled profile is reported for attention" "disabled and stopped profile: ${UNIT_DIR}/gost-kharej-3.service" "${output}"

assert_file "lookalike unit name is untouched" "${UNIT_DIR}/gost-kharej-edge.service"
assert_file "invalid profile number unit is untouched" "${UNIT_DIR}/gost-iran-0.service"
assert_file "unrelated gost.service is untouched" "${UNIT_DIR}/gost.service"
assert_file "lookalike env name is untouched" "${ENV_DIR}/iran-backup.env"
assert_file "unrelated upstream GOST backup is untouched" "${ENV_DIR}/gost.yaml.bak.1"

assert_absent "stopped orphan unit is removed" "${UNIT_DIR}/gost-iran-5.service"
assert_absent "failed orphan unit is removed" "${UNIT_DIR}/gost-kharej-6.service"
assert_contains "orphan unit is disabled before removal" "systemctl disable --now gost-iran-5.service" "${COMMAND_LOG}"
assert_contains "failed orphan has only its own failed state reset" "systemctl reset-failed gost-kharej-6.service" "${COMMAND_LOG}"
assert_file "running orphan unit is kept" "${UNIT_DIR}/gost-iran-7.service"
assert_not_contains "running orphan unit is never stopped" "gost-iran-7.service" "${COMMAND_LOG}.lifecycle"
assert_contains "running orphan unit is reported for attention" "running service whose env is missing: ${UNIT_DIR}/gost-iran-7.service" "${output}"
assert_absent "env without unit is removed" "${ENV_DIR}/kharej-8.env"
assert_absent "managed env backup is removed" "${ENV_DIR}/iran-1.env.bak.20250101120000"
assert_absent "managed unit backup is removed" "${UNIT_DIR}/gost-iran-1.service.bak.20250101120000"
if grep -Eq '^systemctl reset-failed *$' "${COMMAND_LOG}"; then fail "cleanup never resets every failed unit"; else pass "cleanup never resets every failed unit"; fi
assert_contains "cleanup reloads systemd after removing units" "systemctl daemon-reload" "${COMMAND_LOG}"

# Nothing deletable: report only, no prompt, no systemd changes.
attention_only="${TEST_HOME}/attention-only.out"
if run_cleanup "${attention_only}" n; then pass "attention-only cleanup succeeds without confirmation"; else fail "attention-only cleanup succeeds without confirmation"; fi
assert_contains "attention-only cleanup says nothing will be deleted" "Nothing to delete." "${attention_only}"
assert_not_contains "attention-only cleanup sends no daemon-reload" "daemon-reload" "${COMMAND_LOG}"

# An orphan that cannot be stopped keeps its unit and the run reports failure.
write_unit_only iran 9 'loaded|enabled|inactive|0'
blocked="${TEST_HOME}/blocked.out"
export STUB_FAIL_DISABLE=1
if run_cleanup "${blocked}" y; then fail "unstoppable orphan returns nonzero"; else pass "unstoppable orphan returns nonzero"; fi
unset STUB_FAIL_DISABLE
assert_file "unstoppable orphan keeps its unit" "${UNIT_DIR}/gost-iran-9.service"
assert_contains "unstoppable orphan is explained" "Could not stop/disable gost-iran-9.service" "${blocked}"
assert_not_contains "unit-free run sends no daemon-reload" "daemon-reload" "${COMMAND_LOG}"

finish_suite
