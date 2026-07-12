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
  GOST_MANAGER_SOURCE_ROOT="${GOST_MANAGER_SOURCE_ROOT_TEST:-${ROOT_DIR}}" \
  STUB_UNIT_PATH="${root}/etc/systemd/system/gost-monitor-collector.service" \
  SYSTEMD_ANALYZE_BIN=systemd-analyze \
  CHOWN_BIN=chown \
  PYTHONPYCACHEPREFIX="${TEST_HOME}/pycache" \
  bash "${ROOT_DIR}/install.sh" "$@"
}

create_source_fixture() {
  local destination="$1"
  mkdir -p "${destination}/lib" "${destination}/packaging"
  cp "${ROOT_DIR}/gost-manager.sh" "${destination}/gost-manager.sh"
  cp "${ROOT_DIR}/lib/gost-run-iran.sh" "${ROOT_DIR}/lib/gost-run-kharej.sh" "${destination}/lib/"
  cp -R "${ROOT_DIR}/monitoring" "${destination}/monitoring"
  cp "${ROOT_DIR}"/packaging/* "${destination}/packaging/"
}

set_config_value() {
  local config="$1"
  local key="$2"
  local value="$3"
  sed "s|^${key}=.*|${key}=${value}|" "${config}" > "${config}.new"
  mv "${config}.new" "${config}"
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
assert_eq "config directory mode" "700" "$(mode_of "${fresh_root}/etc/gost-manager")"
assert_eq "private library directory mode" "755" "$(mode_of "${fresh_root}/usr/local/lib/gost-manager")"
assert_eq "database mode" "600" "$(mode_of "${fresh_root}/var/lib/gost-manager/metrics.sqlite3")"
assert_contains "fresh collector enabled" "systemctl enable gost-monitor-collector.service" "${COMMAND_LOG}"
assert_contains "fresh collector started" "systemctl start gost-monitor-collector.service" "${COMMAND_LOG}"
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
PYTHONPATH="${installed_library}" python3 -m monitoring.admin_cli \
  --policy installed --path-root "${fresh_root}" validate-config --config "${installed_config}" >/dev/null
PYTHONPATH="${installed_library}" python3 -m monitoring.admin_cli \
  --policy installed --path-root "${fresh_root}" status --config "${installed_config}" >/dev/null
PYTHONPATH="${installed_library}" python3 -m monitoring.admin_cli \
  --policy installed --path-root "${fresh_root}" maintenance --config "${installed_config}" >/dev/null
PYTHONPATH="${installed_library}" python3 -m monitoring.admin_cli \
  --policy installed --path-root "${fresh_root}" --lock-path "${fresh_root}/run/collector.lock" \
  purge-history --yes --config "${installed_config}" >/dev/null
assert_file "installed admin validate/status/maintenance/purge smoke" "${installed_db}"

direct_root="${TEST_HOME}/direct"
mkdir -p "${direct_root}/etc/gost" "${direct_root}/etc/systemd/system" "${direct_root}/usr/local/sbin"
chmod 750 "${direct_root}/etc/gost"
chmod 711 "${direct_root}/etc/systemd/system"
chmod 751 "${direct_root}/usr/local/sbin"
printf 'MAPPINGS=2052:2052\nPASSWORD=direct-secret-canary\n' > "${direct_root}/etc/gost/iran-1.env"
chmod 640 "${direct_root}/etc/gost/iran-1.env"
printf '[Service]\nExecStart=/usr/local/lib/gost-manager/gost-run-iran.sh\n' > "${direct_root}/etc/systemd/system/gost-iran-1.service"
chmod 640 "${direct_root}/etc/systemd/system/gost-iran-1.service"
direct_env_before="$(cksum "${direct_root}/etc/gost/iran-1.env")"
direct_unit_before="$(cksum "${direct_root}/etc/systemd/system/gost-iran-1.service")"
direct_env_mode_before="$(mode_of "${direct_root}/etc/gost/iran-1.env")"
direct_unit_mode_before="$(mode_of "${direct_root}/etc/systemd/system/gost-iran-1.service")"
: > "${COMMAND_LOG}"
rm -f "${STUB_STATE_DIR}/enabled" "${STUB_STATE_DIR}/active"
run_installer "${direct_root}" >/dev/null
assert_eq "Direct env byte-identical" "${direct_env_before}" "$(cksum "${direct_root}/etc/gost/iran-1.env")"
assert_eq "Direct unit byte-identical" "${direct_unit_before}" "$(cksum "${direct_root}/etc/systemd/system/gost-iran-1.service")"
assert_eq "Direct env mode preserved" "${direct_env_mode_before}" "$(mode_of "${direct_root}/etc/gost/iran-1.env")"
assert_eq "Direct unit mode preserved" "${direct_unit_mode_before}" "$(mode_of "${direct_root}/etc/systemd/system/gost-iran-1.service")"
assert_eq "legacy /etc/gost mode preserved" "750" "$(mode_of "${direct_root}/etc/gost")"
assert_eq "shared systemd directory mode preserved" "711" "$(mode_of "${direct_root}/etc/systemd/system")"
assert_eq "shared sbin directory mode preserved" "751" "$(mode_of "${direct_root}/usr/local/sbin")"
assert_not_contains "Direct upgrade has no traffic systemctl" "gost-iran-1.service" "${COMMAND_LOG}"

alternate_root="${TEST_HOME}/alternate-db"
mkdir -p "${alternate_root}/etc/gost-manager"
cp "${ROOT_DIR}/packaging/monitoring.env" "${alternate_root}/etc/gost-manager/monitoring.env"
set_config_value "${alternate_root}/etc/gost-manager/monitoring.env" GOST_MONITOR_DB /var/lib/gost-manager/archive/custom.sqlite3
rm -f "${STUB_STATE_DIR}/enabled" "${STUB_STATE_DIR}/active"
run_installer "${alternate_root}" >/dev/null
assert_file "alternate configured database migrated" "${alternate_root}/var/lib/gost-manager/archive/custom.sqlite3"
assert_absent "alternate install creates no wrong default database" "${alternate_root}/var/lib/gost-manager/metrics.sqlite3"
assert_eq "alternate installed config preserved" "/var/lib/gost-manager/archive/custom.sqlite3" "$(sed -n 's/^GOST_MONITOR_DB=//p' "${alternate_root}/etc/gost-manager/monitoring.env")"

invalid_policy_root="${TEST_HOME}/invalid-policy"
mkdir -p "${invalid_policy_root}/etc/gost-manager"
cp "${ROOT_DIR}/packaging/monitoring.env" "${invalid_policy_root}/etc/gost-manager/monitoring.env"
set_config_value "${invalid_policy_root}/etc/gost-manager/monitoring.env" GOST_MONITOR_DB /srv/gost/metrics.sqlite3
invalid_policy_before="$(tree_digest "${invalid_policy_root}")"
if run_installer "${invalid_policy_root}" > "${TEST_HOME}/invalid-policy.out" 2>&1; then
  fail "policy-incompatible installed config aborts activation"
else
  pass "policy-incompatible installed config aborts activation"
fi
assert_eq "policy-incompatible config is not overwritten" "${invalid_policy_before}" "$(tree_digest "${invalid_policy_root}")"
assert_contains "policy-incompatible config prints migration guidance" "move its database below /var/lib/gost-manager" "${TEST_HOME}/invalid-policy.out"

db_symlink_root="${TEST_HOME}/db-parent-symlink"
mkdir -p "${db_symlink_root}/etc/gost-manager" "${db_symlink_root}/var/lib/gost-manager" "${TEST_HOME}/db-outside"
cp "${ROOT_DIR}/packaging/monitoring.env" "${db_symlink_root}/etc/gost-manager/monitoring.env"
set_config_value "${db_symlink_root}/etc/gost-manager/monitoring.env" GOST_MONITOR_DB /var/lib/gost-manager/archive/current.sqlite3
ln -s "${TEST_HOME}/db-outside" "${db_symlink_root}/var/lib/gost-manager/archive"
if run_installer "${db_symlink_root}" >/dev/null 2>&1; then
  fail "configured DB parent symlink rejected"
else
  pass "configured DB parent symlink rejected"
fi
assert_absent "DB symlink rejection creates no default database" "${db_symlink_root}/var/lib/gost-manager/metrics.sqlite3"

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

metadata_root="${TEST_HOME}/metadata-rollback"
mkdir -p \
  "${metadata_root}/usr/local/sbin" \
  "${metadata_root}/usr/local/lib/gost-manager" \
  "${metadata_root}/etc/gost" \
  "${metadata_root}/etc/gost-manager" \
  "${metadata_root}/etc/systemd/system" \
  "${metadata_root}/var/lib/gost-manager"
chmod 751 "${metadata_root}/usr/local/sbin"
chmod 750 "${metadata_root}/etc/gost"
chmod 711 "${metadata_root}/etc/systemd/system"
chmod 755 "${metadata_root}/usr/local/lib/gost-manager"
chmod 755 "${metadata_root}/etc/gost-manager"
chmod 755 "${metadata_root}/var/lib/gost-manager"
printf 'MAPPINGS=2052:2052\nPASSWORD=metadata-canary\n' > "${metadata_root}/etc/gost/iran-1.env"
chmod 640 "${metadata_root}/etc/gost/iran-1.env"
metadata_before="$(tree_digest "${metadata_root}/etc/gost")"
rm -f "${STUB_STATE_DIR}/enabled" "${STUB_STATE_DIR}/active"
if GOST_MANAGER_FAIL_PHASE=file_replacement run_installer "${metadata_root}" >/dev/null 2>&1; then
  fail "metadata rollback fixture fails as injected"
else
  pass "metadata rollback fixture fails as injected"
fi
assert_eq "shared sbin metadata restored after failure" "751" "$(mode_of "${metadata_root}/usr/local/sbin")"
assert_eq "shared systemd metadata restored after failure" "711" "$(mode_of "${metadata_root}/etc/systemd/system")"
assert_eq "legacy Direct directory unchanged after failure" "${metadata_before}" "$(tree_digest "${metadata_root}/etc/gost")"
assert_eq "private library directory metadata restored" "755" "$(mode_of "${metadata_root}/usr/local/lib/gost-manager")"
assert_eq "private config directory metadata restored" "755" "$(mode_of "${metadata_root}/etc/gost-manager")"
assert_eq "private state directory metadata restored" "755" "$(mode_of "${metadata_root}/var/lib/gost-manager")"

partial_root="${TEST_HOME}/partial-fresh-start"
mkdir -p "${partial_root}"
rm -f "${STUB_STATE_DIR}/enabled" "${STUB_STATE_DIR}/active"
rm -rf "${STUB_STATE_DIR}/wants"
: > "${COMMAND_LOG}"
if STUB_FAIL_SYSTEMCTL_AFTER_ACTION=start STUB_REMOVE_UNIT_BEFORE_DISABLE=1 \
  run_installer "${partial_root}" > "${TEST_HOME}/partial-start.out" 2>&1; then
  fail "partial fresh start failure returns non-zero"
else
  pass "partial fresh start failure returns non-zero"
fi
assert_absent "partial fresh start removes installed unit" "${partial_root}/etc/systemd/system/gost-monitor-collector.service"
assert_absent "partial fresh start removes installed manager" "${partial_root}/usr/local/sbin/gost-manager"
assert_absent "partial fresh start clears enabled state" "${STUB_STATE_DIR}/enabled"
assert_absent "partial fresh start clears active state" "${STUB_STATE_DIR}/active"
assert_absent "partial fresh start clears wants symlink" "${STUB_STATE_DIR}/wants/gost-monitor-collector.service"
assert_contains "partial fresh start stops collector before file rollback" "systemctl stop gost-monitor-collector.service" "${COMMAND_LOG}"
assert_contains "partial fresh start disables collector before file rollback" "systemctl disable gost-monitor-collector.service" "${COMMAND_LOG}"
assert_not_contains "partial fresh rollback never targets traffic" "gost-iran-" "${COMMAND_LOG}"

upgrade_root="${TEST_HOME}/service-upgrade"
mkdir -p "${upgrade_root}"
rm -f "${STUB_STATE_DIR}/enabled" "${STUB_STATE_DIR}/active"
run_installer "${upgrade_root}" >/dev/null
printf 'old-active-manager\n' > "${upgrade_root}/usr/local/sbin/gost-manager"
active_before="$(cksum "${upgrade_root}/usr/local/sbin/gost-manager")"
touch "${STUB_STATE_DIR}/enabled" "${STUB_STATE_DIR}/active"
: > "${COMMAND_LOG}"
if STUB_FAIL_SYSTEMCTL_ACTION=restart run_installer "${upgrade_root}" >/dev/null 2>&1; then
  fail "existing active restart failure returns non-zero"
else
  pass "existing active restart failure returns non-zero"
fi
assert_eq "existing active rollback restores files" "${active_before}" "$(cksum "${upgrade_root}/usr/local/sbin/gost-manager")"
assert_file "existing active rollback restores enabled state" "${STUB_STATE_DIR}/enabled"
assert_file "existing active rollback restores active state" "${STUB_STATE_DIR}/active"
assert_not_contains "existing active rollback avoids traffic" "gost-iran-" "${COMMAND_LOG}"

printf 'old-inactive-manager\n' > "${upgrade_root}/usr/local/sbin/gost-manager"
inactive_before="$(cksum "${upgrade_root}/usr/local/sbin/gost-manager")"
rm -f "${STUB_STATE_DIR}/active"
touch "${STUB_STATE_DIR}/enabled"
if GOST_MANAGER_FAIL_PHASE=collector_start run_installer "${upgrade_root}" >/dev/null 2>&1; then
  fail "existing inactive upgrade failure returns non-zero"
else
  pass "existing inactive upgrade failure returns non-zero"
fi
assert_eq "existing inactive rollback restores files" "${inactive_before}" "$(cksum "${upgrade_root}/usr/local/sbin/gost-manager")"
assert_file "existing inactive rollback keeps enabled state" "${STUB_STATE_DIR}/enabled"
assert_absent "existing inactive rollback keeps inactive state" "${STUB_STATE_DIR}/active"

printf 'old-disabled-active-manager\n' > "${upgrade_root}/usr/local/sbin/gost-manager"
edge_before="$(cksum "${upgrade_root}/usr/local/sbin/gost-manager")"
rm -f "${STUB_STATE_DIR}/enabled"
touch "${STUB_STATE_DIR}/active"
if GOST_MANAGER_FAIL_PHASE=collector_start run_installer "${upgrade_root}" >/dev/null 2>&1; then
  fail "disabled-active upgrade failure returns non-zero"
else
  pass "disabled-active upgrade failure returns non-zero"
fi
assert_eq "disabled-active rollback restores files" "${edge_before}" "$(cksum "${upgrade_root}/usr/local/sbin/gost-manager")"
assert_absent "disabled-active rollback keeps disabled state" "${STUB_STATE_DIR}/enabled"
assert_file "disabled-active rollback keeps active state" "${STUB_STATE_DIR}/active"

printf 'recovery-backup-manager\n' > "${upgrade_root}/usr/local/sbin/gost-manager"
touch "${STUB_STATE_DIR}/enabled" "${STUB_STATE_DIR}/active"
if GOST_MANAGER_FAIL_PHASE=collector_start STUB_FAIL_SYSTEMCTL_ACTION=stop \
  run_installer "${upgrade_root}" > "${TEST_HOME}/recovery-failure.out" 2>&1; then
  fail "unverifiable restoration returns non-zero"
else
  pass "unverifiable restoration returns non-zero"
fi
if find "${upgrade_root}" -name '*.gost-manager-backup.*' -type d | grep -q .; then
  pass "unverifiable restoration retains backup"
else
  fail "unverifiable restoration retains backup"
fi
assert_contains "unverifiable restoration prints recovery procedure" "Installer rollback could not be verified" "${TEST_HOME}/recovery-failure.out"
rm -f "${STUB_STATE_DIR}/enabled" "${STUB_STATE_DIR}/active"

reload_failure_root="${TEST_HOME}/rollback-daemon-reload"
mkdir -p "${reload_failure_root}"
run_installer "${reload_failure_root}" >/dev/null
printf 'daemon-reload-recovery-manager\n' > "${reload_failure_root}/usr/local/sbin/gost-manager"
reload_failure_before="$(cksum "${reload_failure_root}/usr/local/sbin/gost-manager")"
touch "${STUB_STATE_DIR}/enabled" "${STUB_STATE_DIR}/active"
: > "${COMMAND_LOG}"
if GOST_MANAGER_FAIL_PHASE=collector_start STUB_FAIL_SYSTEMCTL_ACTION=daemon-reload \
  run_installer "${reload_failure_root}" > "${TEST_HOME}/reload-recovery.out" 2>&1; then
  fail "rollback daemon-reload failure returns non-zero"
else
  pass "rollback daemon-reload failure returns non-zero"
fi
assert_eq "daemon-reload failure restores old files before refusal" "${reload_failure_before}" "$(cksum "${reload_failure_root}/usr/local/sbin/gost-manager")"
if find "${reload_failure_root}" -name '*.gost-manager-backup.*' -type d | grep -q .; then
  pass "daemon-reload failure retains recovery backup"
else
  fail "daemon-reload failure retains recovery backup"
fi
assert_contains "daemon-reload failure prints exact recovery" "systemctl daemon-reload" "${TEST_HOME}/reload-recovery.out"
assert_not_contains "daemon-reload failure avoids traffic" "gost-kharej-" "${COMMAND_LOG}"
rm -f "${STUB_STATE_DIR}/enabled" "${STUB_STATE_DIR}/active"

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

manifest_missing_source="${TEST_HOME}/source-missing"
create_source_fixture "${manifest_missing_source}"
rm -f "${manifest_missing_source}/monitoring/models.py"
manifest_missing_root="${TEST_HOME}/manifest-missing-root"
mkdir -p "${manifest_missing_root}"
manifest_missing_before="$(tree_digest "${manifest_missing_root}")"
if GOST_MANAGER_SOURCE_ROOT_TEST="${manifest_missing_source}" run_installer "${manifest_missing_root}" >/dev/null 2>&1; then
  fail "missing manifest module fails before mutation"
else
  pass "missing manifest module fails before mutation"
fi
assert_eq "missing manifest module leaves root unchanged" "${manifest_missing_before}" "$(tree_digest "${manifest_missing_root}")"

manifest_duplicate_source="${TEST_HOME}/source-duplicate"
create_source_fixture "${manifest_duplicate_source}"
printf 'monitoring/models.py\n' >> "${manifest_duplicate_source}/packaging/monitoring-runtime-manifest.txt"
manifest_duplicate_root="${TEST_HOME}/manifest-duplicate-root"
mkdir -p "${manifest_duplicate_root}"
if GOST_MANAGER_SOURCE_ROOT_TEST="${manifest_duplicate_source}" run_installer "${manifest_duplicate_root}" >/dev/null 2>&1; then
  fail "duplicate manifest entry rejected"
else
  pass "duplicate manifest entry rejected"
fi

manifest_blank_source="${TEST_HOME}/source-blank-manifest"
create_source_fixture "${manifest_blank_source}"
printf '\n' >> "${manifest_blank_source}/packaging/monitoring-runtime-manifest.txt"
manifest_blank_root="${TEST_HOME}/manifest-blank-root"
mkdir -p "${manifest_blank_root}"
if GOST_MANAGER_SOURCE_ROOT_TEST="${manifest_blank_source}" run_installer "${manifest_blank_root}" >/dev/null 2>&1; then
  fail "blank manifest entry rejected"
else
  pass "blank manifest entry rejected"
fi

manifest_file_symlink_source="${TEST_HOME}/source-symlink-manifest"
create_source_fixture "${manifest_file_symlink_source}"
cp "${manifest_file_symlink_source}/packaging/monitoring-runtime-manifest.txt" "${TEST_HOME}/external-manifest.txt"
rm -f "${manifest_file_symlink_source}/packaging/monitoring-runtime-manifest.txt"
ln -s "${TEST_HOME}/external-manifest.txt" "${manifest_file_symlink_source}/packaging/monitoring-runtime-manifest.txt"
manifest_file_symlink_root="${TEST_HOME}/manifest-file-symlink-root"
mkdir -p "${manifest_file_symlink_root}"
if GOST_MANAGER_SOURCE_ROOT_TEST="${manifest_file_symlink_source}" run_installer "${manifest_file_symlink_root}" >/dev/null 2>&1; then
  fail "symlinked runtime manifest rejected"
else
  pass "symlinked runtime manifest rejected"
fi

for unsafe_manifest in 'monitoring/../escape.py' '/tmp/escape.py'; do
  unsafe_name="$(printf '%s' "${unsafe_manifest}" | cksum | awk '{print $1}')"
  unsafe_source="${TEST_HOME}/source-unsafe-${unsafe_name}"
  unsafe_root="${TEST_HOME}/root-unsafe-${unsafe_name}"
  create_source_fixture "${unsafe_source}"
  printf '%s\n' "${unsafe_manifest}" >> "${unsafe_source}/packaging/monitoring-runtime-manifest.txt"
  mkdir -p "${unsafe_root}"
  if GOST_MANAGER_SOURCE_ROOT_TEST="${unsafe_source}" run_installer "${unsafe_root}" >/dev/null 2>&1; then
    fail "unsafe manifest entry ${unsafe_manifest} rejected"
  else
    pass "unsafe manifest entry ${unsafe_manifest} rejected"
  fi
done

manifest_symlink_source="${TEST_HOME}/source-symlink-module"
create_source_fixture "${manifest_symlink_source}"
printf 'external-runtime-canary\n' > "${TEST_HOME}/external-runtime.py"
rm -f "${manifest_symlink_source}/monitoring/runtime_lock.py"
ln -s "${TEST_HOME}/external-runtime.py" "${manifest_symlink_source}/monitoring/runtime_lock.py"
manifest_symlink_root="${TEST_HOME}/manifest-symlink-root"
mkdir -p "${manifest_symlink_root}"
if GOST_MANAGER_SOURCE_ROOT_TEST="${manifest_symlink_source}" run_installer "${manifest_symlink_root}" >/dev/null 2>&1; then
  fail "symlinked runtime module rejected"
else
  pass "symlinked runtime module rejected"
fi
assert_absent "external symlink content never installed" "${manifest_symlink_root}/usr/local/lib/gost-manager/monitoring/runtime_lock.py"

manifest_extra_source="${TEST_HOME}/source-extra-module"
create_source_fixture "${manifest_extra_source}"
printf 'EXTRA = True\n' > "${manifest_extra_source}/monitoring/unlisted_extra.py"
manifest_extra_root="${TEST_HOME}/manifest-extra-root"
mkdir -p "${manifest_extra_root}"
rm -f "${STUB_STATE_DIR}/enabled" "${STUB_STATE_DIR}/active"
GOST_MANAGER_SOURCE_ROOT_TEST="${manifest_extra_source}" run_installer "${manifest_extra_root}" >/dev/null
assert_absent "unlisted extra Python module is not installed" "${manifest_extra_root}/usr/local/lib/gost-manager/monitoring/unlisted_extra.py"

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
[[ "${STUB_APT_FAIL:-0}" != "1" ]] || exit 1
if [[ "${1:-}" == "install" ]]; then
  IFS=',' read -r -a provided <<< "${STUB_PROVIDE_COMMANDS:-ss}"
  for command_name in "${provided[@]}"; do
    case "${command_name}" in
      ss) target="${STUB_TRUE_BIN}" ;;
      cmp) target="${STUB_REAL_CMP_BIN}" ;;
      stat) target="${STUB_REAL_STAT_BIN}" ;;
      *) exit 2 ;;
    esac
    ln -sfn "${target}" "${STUB_BIN_PATH}/${command_name}"
  done
fi
STUB
chmod 755 "${STUB_BIN}/apt-get"
REAL_CMP_BIN="$(command -v cmp)"
REAL_STAT_BIN="$(command -v stat)"
TRUE_BIN="$(type -P true)"
dependency_root="${TEST_HOME}/dependency-opt-in"
mkdir -p "${dependency_root}"
mv "${STUB_BIN}/ss" "${STUB_BIN}/ss.disabled"
: > "${COMMAND_LOG}"
GOST_MANAGER_ALLOW_TEST_PACKAGE_STUB=1 \
STUB_BIN_PATH="${STUB_BIN}" \
STUB_TRUE_BIN="${TRUE_BIN}" \
STUB_REAL_CMP_BIN="${REAL_CMP_BIN}" \
STUB_REAL_STAT_BIN="${REAL_STAT_BIN}" \
APT_GET_BIN="${STUB_BIN}/apt-get" \
run_installer "${dependency_root}" --install-dependencies >/dev/null
assert_contains "dependency opt-in runs apt update" "apt-get update" "${COMMAND_LOG}"
assert_contains "dependency opt-in installs only expected package" "apt-get install -y iproute2" "${COMMAND_LOG}"
assert_file "dependency opt-in completes installation" "${dependency_root}/usr/local/sbin/gost-monitor"
rm -f "${STUB_BIN}/ss.disabled"

cmp_dependency_root="${TEST_HOME}/dependency-cmp"
mkdir -p "${cmp_dependency_root}"
rm -f "${STUB_BIN}/cmp"
: > "${COMMAND_LOG}"
GOST_MANAGER_TEST_MISSING_COMMANDS=cmp \
GOST_MANAGER_TEST_DEP_BIN="${STUB_BIN}" \
GOST_MANAGER_ALLOW_TEST_PACKAGE_STUB=1 \
STUB_PROVIDE_COMMANDS=cmp \
STUB_BIN_PATH="${STUB_BIN}" \
STUB_TRUE_BIN="${TRUE_BIN}" \
STUB_REAL_CMP_BIN="${REAL_CMP_BIN}" \
STUB_REAL_STAT_BIN="${REAL_STAT_BIN}" \
APT_GET_BIN="${STUB_BIN}/apt-get" \
run_installer "${cmp_dependency_root}" --install-dependencies >/dev/null
assert_contains "missing cmp installs only diffutils" "apt-get install -y diffutils" "${COMMAND_LOG}"

dedupe_dependency_root="${TEST_HOME}/dependency-dedupe"
mkdir -p "${dedupe_dependency_root}"
rm -f "${STUB_BIN}/cmp" "${STUB_BIN}/stat"
: > "${COMMAND_LOG}"
GOST_MANAGER_TEST_MISSING_COMMANDS='cmp,stat' \
GOST_MANAGER_TEST_DEP_BIN="${STUB_BIN}" \
GOST_MANAGER_ALLOW_TEST_PACKAGE_STUB=1 \
STUB_PROVIDE_COMMANDS='cmp,stat' \
STUB_BIN_PATH="${STUB_BIN}" \
STUB_TRUE_BIN="${TRUE_BIN}" \
STUB_REAL_CMP_BIN="${REAL_CMP_BIN}" \
STUB_REAL_STAT_BIN="${REAL_STAT_BIN}" \
APT_GET_BIN="${STUB_BIN}/apt-get" \
run_installer "${dedupe_dependency_root}" --install-dependencies >/dev/null
assert_contains "missing commands across packages map exactly" "apt-get install -y diffutils coreutils" "${COMMAND_LOG}"

recheck_dependency_root="${TEST_HOME}/dependency-recheck"
mkdir -p "${recheck_dependency_root}"
recheck_before="$(tree_digest "${recheck_dependency_root}")"
rm -f "${STUB_BIN}/cmp" "${STUB_BIN}/stat"
if GOST_MANAGER_TEST_MISSING_COMMANDS='cmp,stat' \
  GOST_MANAGER_TEST_DEP_BIN="${STUB_BIN}" \
  GOST_MANAGER_ALLOW_TEST_PACKAGE_STUB=1 \
  STUB_PROVIDE_COMMANDS=cmp \
  STUB_BIN_PATH="${STUB_BIN}" \
  STUB_TRUE_BIN="${TRUE_BIN}" \
  STUB_REAL_CMP_BIN="${REAL_CMP_BIN}" \
  STUB_REAL_STAT_BIN="${REAL_STAT_BIN}" \
  APT_GET_BIN="${STUB_BIN}/apt-get" \
  run_installer "${recheck_dependency_root}" --install-dependencies >/dev/null 2>&1; then
  fail "dependency commands are rechecked after apt success"
else
  pass "dependency commands are rechecked after apt success"
fi
assert_eq "dependency recheck failure causes no file mutation" "${recheck_before}" "$(tree_digest "${recheck_dependency_root}")"

apt_failure_root="${TEST_HOME}/dependency-apt-failure"
mkdir -p "${apt_failure_root}"
apt_failure_before="$(tree_digest "${apt_failure_root}")"
rm -f "${STUB_BIN}/cmp"
if GOST_MANAGER_TEST_MISSING_COMMANDS=cmp \
  GOST_MANAGER_TEST_DEP_BIN="${STUB_BIN}" \
  GOST_MANAGER_ALLOW_TEST_PACKAGE_STUB=1 \
  STUB_APT_FAIL=1 \
  STUB_BIN_PATH="${STUB_BIN}" \
  STUB_TRUE_BIN="${TRUE_BIN}" \
  STUB_REAL_CMP_BIN="${REAL_CMP_BIN}" \
  STUB_REAL_STAT_BIN="${REAL_STAT_BIN}" \
  APT_GET_BIN="${STUB_BIN}/apt-get" \
  run_installer "${apt_failure_root}" --install-dependencies >/dev/null 2>&1; then
  fail "failed package installation returns non-zero"
else
  pass "failed package installation returns non-zero"
fi
assert_eq "failed package installation causes no file mutation" "${apt_failure_before}" "$(tree_digest "${apt_failure_root}")"

finish_suite
