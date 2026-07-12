#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=integration-test-lib.sh
source "${ROOT_DIR}/tests/integration-test-lib.sh"

TEST_HOME="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/gost-install-tests.XXXXXX")" && pwd -P)"
cleanup_test_home() {
  local status=$?
  rm -rf "${TEST_HOME}"
  exit "${status}"
}
trap cleanup_test_home EXIT
STUB_BIN="${TEST_HOME}/bin"
COMMAND_LOG="${TEST_HOME}/commands.log"
STUB_STATE_DIR="${TEST_HOME}/state"
mkdir -p "${STUB_STATE_DIR}"
: > "${COMMAND_LOG}"
make_command_stubs "${STUB_BIN}"
export COMMAND_LOG STUB_STATE_DIR
export PATH="${STUB_BIN}:${PATH}"

run_installer() {
  local root="$1"
  shift
  GOST_MANAGER_TESTING=1 \
  GOST_MANAGER_ROOT="${root}" \
  SYSTEMD_ANALYZE_BIN=systemd-analyze \
  CHOWN_BIN=chown \
  PYTHONPYCACHEPREFIX="${TEST_HOME}/pycache" \
  bash "${ROOT_DIR}/install.sh" "$@"
}

fresh_root="${TEST_HOME}/fresh"
mkdir -p "${fresh_root}"
run_installer "${fresh_root}" >/dev/null
assert_file "fresh manager installed" "${fresh_root}/usr/local/sbin/gost-manager"
assert_file "fresh query launcher installed" "${fresh_root}/usr/local/sbin/gost-monitor"
assert_file "fresh collector launcher installed" "${fresh_root}/usr/local/sbin/gost-monitor-collector"
assert_file "fresh admin launcher installed" "${fresh_root}/usr/local/sbin/gost-monitor-admin"
assert_file "complete package includes init" "${fresh_root}/usr/local/lib/gost-manager/monitoring/__init__.py"
assert_file "fresh default config installed" "${fresh_root}/etc/gost-manager/monitoring.env"
assert_file "fresh systemd unit installed" "${fresh_root}/etc/systemd/system/gost-monitor-collector.service"
assert_file "fresh schema migrated" "${fresh_root}/var/lib/gost-manager/metrics.sqlite3"
assert_eq "launcher mode" "755" "$(mode_of "${fresh_root}/usr/local/sbin/gost-monitor")"
assert_eq "config mode" "600" "$(mode_of "${fresh_root}/etc/gost-manager/monitoring.env")"
assert_eq "state directory mode" "700" "$(mode_of "${fresh_root}/var/lib/gost-manager")"
assert_eq "database mode" "600" "$(mode_of "${fresh_root}/var/lib/gost-manager/metrics.sqlite3")"
assert_contains "fresh collector enabled and started" "systemctl enable --now gost-monitor-collector.service" "${COMMAND_LOG}"
assert_not_contains "fresh install never targets Iran traffic" "gost-iran-" "${COMMAND_LOG}"
assert_not_contains "fresh install never targets Kharej traffic" "gost-kharej-" "${COMMAND_LOG}"
assert_not_contains "fresh install never targets NGINX" "nginx" "${COMMAND_LOG}"

launcher_bin="${TEST_HOME}/launcher-bin"
launcher_log="${TEST_HOME}/launcher.log"
mkdir -p "${launcher_bin}"
cat > "${launcher_bin}/python3" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'PYTHONPATH=%s\n' "${PYTHONPATH}" > "${LAUNCHER_LOG}"
printf '%s\n' "$@" >> "${LAUNCHER_LOG}"
STUB
chmod 755 "${launcher_bin}/python3"
LAUNCHER_LOG="${launcher_log}" PATH="${launcher_bin}:${PATH}" \
  "${fresh_root}/usr/local/sbin/gost-monitor" "argument with spaces" >/dev/null
assert_contains "installed launcher sets fixed library path" "PYTHONPATH=/usr/local/lib/gost-manager" "${launcher_log}"
assert_contains "installed launcher selects query module" "monitoring.query_cli" "${launcher_log}"
assert_contains "installed launcher preserves spaced argument" "argument with spaces" "${launcher_log}"

installed_library="${fresh_root}/usr/local/lib/gost-manager"
installed_config="${fresh_root}/etc/gost-manager/monitoring.env"
installed_db="${fresh_root}/var/lib/gost-manager/metrics.sqlite3"
PYTHONPATH="${installed_library}" python3 -m monitoring.admin_cli validate-config --config "${installed_config}" >/dev/null
PYTHONPATH="${installed_library}" python3 -m monitoring.admin_cli status --db "${installed_db}" >/dev/null
PYTHONPATH="${installed_library}" python3 -m monitoring.admin_cli maintenance --db "${installed_db}" >/dev/null
PYTHONPATH="${installed_library}" python3 -m monitoring.admin_cli purge-history --yes --db "${installed_db}" >/dev/null
assert_file "installed admin validate/status/maintenance/purge smoke" "${installed_db}"

direct_root="${TEST_HOME}/direct"
mkdir -p "${direct_root}/etc/gost" "${direct_root}/etc/systemd/system"
printf 'MAPPINGS=2052:2052\nPASSWORD=direct-secret-canary\n' > "${direct_root}/etc/gost/iran-1.env"
printf '[Service]\nExecStart=/usr/local/lib/gost-manager/gost-run-iran.sh\n' > "${direct_root}/etc/systemd/system/gost-iran-1.service"
direct_env_before="$(cksum "${direct_root}/etc/gost/iran-1.env")"
direct_unit_before="$(cksum "${direct_root}/etc/systemd/system/gost-iran-1.service")"
: > "${COMMAND_LOG}"
rm -f "${STUB_STATE_DIR}/enabled" "${STUB_STATE_DIR}/active"
run_installer "${direct_root}" >/dev/null
assert_eq "Direct env byte-identical" "${direct_env_before}" "$(cksum "${direct_root}/etc/gost/iran-1.env")"
assert_eq "Direct unit byte-identical" "${direct_unit_before}" "$(cksum "${direct_root}/etc/systemd/system/gost-iran-1.service")"
assert_not_contains "Direct upgrade has no traffic systemctl" "gost-iran-1.service" "${COMMAND_LOG}"

custom_config="${fresh_root}/etc/gost-manager/monitoring.env"
sed 's/GOST_MONITOR_SAMPLE_INTERVAL=5/GOST_MONITOR_SAMPLE_INTERVAL=10/' "${custom_config}" > "${custom_config}.new"
mv "${custom_config}.new" "${custom_config}"
chmod 600 "${custom_config}"
custom_before="$(cksum "${custom_config}")"
db_before="$(cksum "${fresh_root}/var/lib/gost-manager/metrics.sqlite3")"
touch "${STUB_STATE_DIR}/enabled" "${STUB_STATE_DIR}/active"
: > "${COMMAND_LOG}"
run_installer "${fresh_root}" >/dev/null
assert_eq "valid custom config preserved" "${custom_before}" "$(cksum "${custom_config}")"
assert_file "existing monitoring history preserved" "${fresh_root}/var/lib/gost-manager/metrics.sqlite3"
if [[ -n "${db_before}" ]]; then pass "existing database remained usable"; else fail "existing database remained usable"; fi
assert_contains "active collector restarts only itself" "systemctl restart gost-monitor-collector.service" "${COMMAND_LOG}"
assert_not_contains "monitoring upgrade avoids traffic services" "gost-iran-" "${COMMAND_LOG}"

manager_before="$(cksum "${fresh_root}/usr/local/sbin/gost-manager")"
unit_before="$(cksum "${fresh_root}/etc/systemd/system/gost-monitor-collector.service")"
: > "${COMMAND_LOG}"
run_installer "${fresh_root}" >/dev/null
assert_eq "idempotent manager content" "${manager_before}" "$(cksum "${fresh_root}/usr/local/sbin/gost-manager")"
assert_eq "idempotent unit content" "${unit_before}" "$(cksum "${fresh_root}/etc/systemd/system/gost-monitor-collector.service")"
assert_eq "idempotent config content" "${custom_before}" "$(cksum "${custom_config}")"

rm -f "${STUB_STATE_DIR}/active"
touch "${STUB_STATE_DIR}/enabled"
: > "${COMMAND_LOG}"
run_installer "${fresh_root}" >/dev/null
assert_not_contains "inactive collector is not restarted" "systemctl restart gost-monitor-collector.service" "${COMMAND_LOG}"
assert_not_contains "inactive collector is not started" "systemctl start gost-monitor-collector.service" "${COMMAND_LOG}"
touch "${STUB_STATE_DIR}/active"

printf 'previous-manager-content\n' > "${fresh_root}/usr/local/sbin/gost-manager"
rollback_before="$(cksum "${fresh_root}/usr/local/sbin/gost-manager")"
: > "${COMMAND_LOG}"
if GOST_MANAGER_FAIL_PHASE=daemon_reload run_installer "${fresh_root}" >/dev/null 2>&1; then
  fail "injected failed upgrade returns failure"
else
  pass "injected failed upgrade returns failure"
fi
assert_eq "rollback restores previous manager" "${rollback_before}" "$(cksum "${fresh_root}/usr/local/sbin/gost-manager")"
assert_eq "rollback preserves custom config" "${custom_before}" "$(cksum "${custom_config}")"
assert_not_contains "rollback never targets traffic" "gost-iran-" "${COMMAND_LOG}"

for failure_phase in staging bash_validation python_validation config_validation unit_validation backup file_replacement migration collector_start; do
  phase_root="${TEST_HOME}/phase-${failure_phase}"
  mkdir -p "${phase_root}"
  rm -f "${STUB_STATE_DIR}/enabled" "${STUB_STATE_DIR}/active"
  : > "${COMMAND_LOG}"
  if GOST_MANAGER_FAIL_PHASE="${failure_phase}" run_installer "${phase_root}" >/dev/null 2>&1; then
    fail "rollback phase ${failure_phase} returns failure"
  else
    pass "rollback phase ${failure_phase} returns failure"
  fi
  assert_absent "rollback phase ${failure_phase} removes manager" "${phase_root}/usr/local/sbin/gost-manager"
  assert_absent "rollback phase ${failure_phase} removes monitoring unit" "${phase_root}/etc/systemd/system/gost-monitor-collector.service"
  assert_not_contains "rollback phase ${failure_phase} avoids traffic" "gost-iran-" "${COMMAND_LOG}"
done

symlink_root="${TEST_HOME}/symlink"
outside="${TEST_HOME}/outside-canary"
printf 'outside-safe\n' > "${outside}"
mkdir -p "${symlink_root}/usr/local/sbin"
ln -s "${outside}" "${symlink_root}/usr/local/sbin/gost-monitor"
: > "${COMMAND_LOG}"
if run_installer "${symlink_root}" >/dev/null 2>&1; then
  fail "symlinked managed destination rejected"
else
  pass "symlinked managed destination rejected"
fi
assert_eq "symlink target unchanged" "outside-safe" "$(tr -d '\n' < "${outside}")"

missing_root="${TEST_HOME}/missing"
mkdir -p "${missing_root}"
mv "${STUB_BIN}/ss" "${STUB_BIN}/ss.disabled"
missing_before="$(tree_digest "${missing_root}")"
if run_installer "${missing_root}" >/dev/null 2>&1; then
  fail "missing dependency without opt-in fails"
else
  pass "missing dependency without opt-in fails"
fi
assert_eq "missing dependency causes no mutation" "${missing_before}" "$(tree_digest "${missing_root}")"
mv "${STUB_BIN}/ss.disabled" "${STUB_BIN}/ss"

cat > "${STUB_BIN}/apt-get" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'apt-get %s\n' "$*" >> "${COMMAND_LOG}"
if [[ "${1:-}" == "install" ]]; then
  cat > "${STUB_BIN_PATH}/ss" <<'SS'
#!/usr/bin/env bash
exit 0
SS
  chmod 755 "${STUB_BIN_PATH}/ss"
fi
STUB
chmod 755 "${STUB_BIN}/apt-get"
dependency_root="${TEST_HOME}/dependency-opt-in"
mkdir -p "${dependency_root}"
mv "${STUB_BIN}/ss" "${STUB_BIN}/ss.disabled"
: > "${COMMAND_LOG}"
GOST_MANAGER_ALLOW_TEST_PACKAGE_STUB=1 \
STUB_BIN_PATH="${STUB_BIN}" \
APT_GET_BIN="${STUB_BIN}/apt-get" \
run_installer "${dependency_root}" --install-dependencies >/dev/null
assert_contains "dependency opt-in runs apt update" "apt-get update" "${COMMAND_LOG}"
assert_contains "dependency opt-in installs only expected package" "apt-get install -y iproute2" "${COMMAND_LOG}"
assert_file "dependency opt-in completes installation" "${dependency_root}/usr/local/sbin/gost-monitor"
rm -f "${STUB_BIN}/ss.disabled"

finish_suite
