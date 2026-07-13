#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=integration-test-lib.sh
source "${ROOT_DIR}/tests/integration-test-lib.sh"

TEST_HOME="$(mktemp -d "${TMPDIR:-/tmp}/gost-stability-tests.XXXXXX")"
cleanup_test_home() {
  local status=$?
  rm -rf "${TEST_HOME}"
  exit "${status}"
}
trap cleanup_test_home EXIT

SYSTEMD_ROOT="${TEST_HOME}/systemd"
SYSCTL_ROOT="${TEST_HOME}/sysctl.d"
ENV_ROOT="${TEST_HOME}/gost"
STUB_BIN="${TEST_HOME}/bin"
COMMAND_LOG="${TEST_HOME}/commands.log"
SYSCTL_STATE="${TEST_HOME}/sysctl.state"
mkdir -p "${SYSTEMD_ROOT}" "${SYSCTL_ROOT}" "${ENV_ROOT}" "${STUB_BIN}"
: > "${COMMAND_LOG}"

cat > "${STUB_BIN}/sysctl" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'sysctl' >> "${COMMAND_LOG}"
printf ' %q' "$@" >> "${COMMAND_LOG}"
printf '\n' >> "${COMMAND_LOG}"
if [[ "${1:-}" == "--system" ]]; then
  [[ "${STUB_SYSCTL_APPLY_EXIT:-0}" == "0" ]] || exit "${STUB_SYSCTL_APPLY_EXIT}"
  candidate="${SYSCTL_STATE}.candidate"
  : > "${candidate}"
  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ -n "${line}" && "${line}" != \#* ]] || continue
    key="${line%% = *}"
    value="${line#* = }"
    printf '%s|%s\n' "${key}" "${value}" >> "${candidate}"
  done < "${GOST_STABILITY_SYSCTL_FILE_TEST}"
  mv "${candidate}" "${SYSCTL_STATE}"
  exit 0
fi
if [[ "${1:-}" == "-n" && -n "${2:-}" ]]; then
  value="$(awk -F '|' -v key="$2" '$1 == key {print $2; found=1; exit} END {if (!found) exit 1}' "${SYSCTL_STATE}")" || exit 1
  printf '%s\n' "${value}"
  exit 0
fi
exit 2
STUB

cat > "${STUB_BIN}/systemctl" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'systemctl' >> "${COMMAND_LOG}"
printf ' %q' "$@" >> "${COMMAND_LOG}"
printf '\n' >> "${COMMAND_LOG}"
[[ "$#" -eq 1 && "$1" == "daemon-reload" ]] || exit 97
exit "${STUB_DAEMON_RELOAD_EXIT:-0}"
STUB
chmod 755 "${STUB_BIN}/sysctl" "${STUB_BIN}/systemctl"

cat > "${SYSTEMD_ROOT}/gost-iran-1.service" <<'UNIT'
[Service]
EnvironmentFile=/etc/gost/iran-1.env
ExecStart=/usr/local/lib/gost-manager/gost-run-iran.sh
UNIT
cat > "${SYSTEMD_ROOT}/gost-kharej-2.service" <<'UNIT'
[Service]
EnvironmentFile=/etc/gost/kharej-2.env
ExecStart=/usr/local/lib/gost-manager/gost-run-kharej.sh
UNIT
cat > "${SYSTEMD_ROOT}/gost.service" <<'UNIT'
[Service]
ExecStart=/bin/true
UNIT
cat > "${SYSTEMD_ROOT}/gost-iran-01.service" <<'UNIT'
[Service]
ExecStart=/bin/true
UNIT
cat > "${SYSTEMD_ROOT}/gost-kharej-edge.service" <<'UNIT'
[Service]
ExecStart=/bin/true
UNIT
cat > "${SYSTEMD_ROOT}/nginx.service" <<'UNIT'
[Service]
ExecStart=/bin/true
UNIT

printf 'SENSITIVE_CANARY=stability-output-must-not-read-env\n' > "${ENV_ROOT}/iran-1.env"
printf 'PROFILE_LABEL=edge-two\n' > "${ENV_ROOT}/kharej-2.env"

cat > "${SYSCTL_STATE}" <<'STATE'
fs.file-max|100000
net.core.somaxconn|128
net.ipv4.ip_local_port_range|32768 60999
net.ipv4.tcp_max_syn_backlog|4096
net.ipv4.tcp_fin_timeout|60
net.ipv4.tcp_keepalive_time|7200
net.ipv4.tcp_keepalive_intvl|75
net.ipv4.tcp_keepalive_probes|9
net.ipv4.tcp_slow_start_after_idle|1
STATE

export COMMAND_LOG SYSCTL_STATE
export PATH="${STUB_BIN}:${PATH}"
export GOST_MANAGER_TESTING=1
export GOST_SYSTEMD_DIR_TEST="${SYSTEMD_ROOT}"
export GOST_ETC_DIR_TEST="${ENV_ROOT}"
export GOST_STABILITY_SYSCTL_FILE_TEST="${SYSCTL_ROOT}/99-gost-stability.conf"

# shellcheck source=../gost-manager.sh
source "${ROOT_DIR}/gost-manager.sh"

require_root() { return 0; }

assert_eq "sysctl dependency maps to procps" "procps" "$(package_for_command sysctl)"
assert_eq "cmp dependency maps to diffutils" "diffutils" "$(package_for_command cmp)"

inode_of() {
  local path="$1"
  stat -c '%i' "${path}" 2>/dev/null || stat -f '%i' "${path}"
}

count_log_line() {
  local expected="$1"
  awk -v expected="${expected}" '$0 == expected {count++} END {print count + 0}' "${COMMAND_LOG}"
}

count_log_prefix() {
  local prefix="$1"
  awk -v prefix="${prefix}" 'index($0, prefix) == 1 {count++} END {print count + 0}' "${COMMAND_LOG}"
}

iran_unit_before="$(cksum "${SYSTEMD_ROOT}/gost-iran-1.service")"
kharej_unit_before="$(cksum "${SYSTEMD_ROOT}/gost-kharej-2.service")"
iran_env_before="$(cksum "${ENV_ROOT}/iran-1.env")"
kharej_env_before="$(cksum "${ENV_ROOT}/kharej-2.env")"

inventory="${TEST_HOME}/inventory"
discover_stability_services "${inventory}"
assert_eq "exact stability service discovery count" "2" "$(wc -l < "${inventory}" | tr -d ' ')"
assert_contains "exact Iran service accepted" "gost-iran-1.service" "${inventory}"
assert_contains "exact Kharej service accepted" "gost-kharej-2.service" "${inventory}"
assert_not_contains "unnumbered GOST service rejected" "gost.service" "${inventory}"
assert_not_contains "leading-zero service rejected" "gost-iran-01.service" "${inventory}"
assert_not_contains "named service rejected" "gost-kharej-edge.service" "${inventory}"
assert_not_contains "NGINX service rejected" "nginx.service" "${inventory}"

first_output="${TEST_HOME}/first.out"
if run_server_stability > "${first_output}" 2>&1; then
  pass "first stability run succeeds"
else
  fail "first stability run succeeds"
fi

sysctl_file="${GOST_STABILITY_SYSCTL_FILE_TEST}"
iran_override="${SYSTEMD_ROOT}/gost-iran-1.service.d/stability.conf"
kharej_override="${SYSTEMD_ROOT}/gost-kharej-2.service.d/stability.conf"
assert_file "sysctl stability file created" "${sysctl_file}"
assert_eq "sysctl stability file mode" "644" "$(mode_of "${sysctl_file}")"
if stability_file_matches "${sysctl_file}" render_stability_sysctl_config; then
  pass "sysctl stability content is exact"
else
  fail "sysctl stability content is exact"
fi
for setting in \
  "fs.file-max = 2097152" \
  "net.core.somaxconn = 65535" \
  "net.core.netdev_max_backlog = 250000" \
  "net.ipv4.ip_local_port_range = 10000 65000" \
  "net.ipv4.tcp_max_syn_backlog = 65535" \
  "net.ipv4.tcp_fin_timeout = 15" \
  "net.ipv4.tcp_keepalive_time = 60" \
  "net.ipv4.tcp_keepalive_intvl = 10" \
  "net.ipv4.tcp_keepalive_probes = 6" \
  "net.ipv4.tcp_slow_start_after_idle = 0"; do
  assert_contains "sysctl contains ${setting%% =*}" "${setting}" "${sysctl_file}"
done
assert_not_contains "tcp_tw_reuse remains absent" "net.ipv4.tcp_tw_reuse" "${sysctl_file}"
assert_file "Iran stability override created" "${iran_override}"
assert_file "Kharej stability override created" "${kharej_override}"
assert_eq "Iran stability override mode" "644" "$(mode_of "${iran_override}")"
if stability_file_matches "${iran_override}" render_stability_systemd_override &&
   stability_file_matches "${kharej_override}" render_stability_systemd_override; then
  pass "systemd stability override content is exact"
else
  fail "systemd stability override content is exact"
fi
for setting in \
  "LimitNOFILE=1048576" \
  "TasksMax=infinity" \
  "OOMScoreAdjust=-500" \
  "Restart=always" \
  "RestartSec=3"; do
  assert_contains "Iran override contains ${setting%%=*}" "${setting}" "${iran_override}"
  assert_contains "Kharej override contains ${setting%%=*}" "${setting}" "${kharej_override}"
done
assert_not_contains "unsafe OOM immunity is absent" "OOMScoreAdjust=-900" "${iran_override}"
assert_absent "unrelated gost.service receives no override" "${SYSTEMD_ROOT}/gost.service.d"
assert_absent "NGINX receives no override" "${SYSTEMD_ROOT}/nginx.service.d"
assert_eq "first run applies sysctl once" "1" "$(count_log_line 'sysctl --system')"
assert_eq "first run daemon-reloads once" "1" "$(count_log_line 'systemctl daemon-reload')"
assert_eq "daemon-reload is the only systemctl command" "1" "$(count_log_prefix 'systemctl ')"
assert_not_contains "wizard issues no restart command" "systemctl restart" "${COMMAND_LOG}"
assert_not_contains "wizard issues no start command" "systemctl start" "${COMMAND_LOG}"
assert_not_contains "wizard issues no stop command" "systemctl stop" "${COMMAND_LOG}"
assert_eq "Iran unit remains byte-identical" "${iran_unit_before}" "$(cksum "${SYSTEMD_ROOT}/gost-iran-1.service")"
assert_eq "Kharej unit remains byte-identical" "${kharej_unit_before}" "$(cksum "${SYSTEMD_ROOT}/gost-kharej-2.service")"
assert_eq "Iran env remains byte-identical" "${iran_env_before}" "$(cksum "${ENV_ROOT}/iran-1.env")"
assert_eq "Kharej env remains byte-identical" "${kharej_env_before}" "$(cksum "${ENV_ROOT}/kharej-2.env")"
assert_contains "report shows old current value" "Current: 128" "${first_output}"
assert_contains "report shows unavailable current value" "net.core.netdev_max_backlog = unavailable" "${first_output}"
assert_contains "report shows applied kernel config" "Kernel configuration: Applied" "${first_output}"
assert_contains "report shows Iran restart required" $'Restart required:\n\ngost-iran-1\ngost-kharej-2' "${first_output}"
assert_contains "report shows Kharej restart required" $'gost-kharej-2\n\nReason:' "${first_output}"
assert_contains "report shows optimized service count" "Services optimized: 2" "${first_output}"
assert_contains "report records zero restarts" "Restart count: 0" "${first_output}"
assert_contains "report records one daemon reload" "Daemon reload count: 1" "${first_output}"
assert_contains "report confirms uninterrupted connections" "Existing connections were not interrupted" "${first_output}"
assert_not_contains "report never reads env canary" "stability-output-must-not-read-env" "${first_output}"

sysctl_inode="$(inode_of "${sysctl_file}")"
iran_override_inode="$(inode_of "${iran_override}")"
kharej_override_inode="$(inode_of "${kharej_override}")"
: > "${COMMAND_LOG}"
second_output="${TEST_HOME}/second.out"
if run_server_stability > "${second_output}" 2>&1; then
  pass "idempotent second stability run succeeds"
else
  fail "idempotent second stability run succeeds"
fi
assert_eq "idempotent sysctl file is not rewritten" "${sysctl_inode}" "$(inode_of "${sysctl_file}")"
assert_eq "idempotent Iran override is not rewritten" "${iran_override_inode}" "$(inode_of "${iran_override}")"
assert_eq "idempotent Kharej override is not rewritten" "${kharej_override_inode}" "$(inode_of "${kharej_override}")"
assert_eq "idempotent run skips sysctl apply" "0" "$(count_log_line 'sysctl --system')"
assert_eq "idempotent run skips daemon reload" "0" "$(count_log_line 'systemctl daemon-reload')"
assert_contains "idempotent report says already optimized" "Kernel configuration: Already optimized" "${second_output}"
assert_contains "idempotent service report says already optimized" "Override: Already optimized" "${second_output}"
assert_contains "idempotent report adds no restart requirement" $'Restart required:\n\nNone from this run.' "${second_output}"

printf '%s\nfs.file-max = 2097152\n' "${STABILITY_MANAGED_MARKER}" > "${sysctl_file}"
backup_count_before="$(find "${SYSCTL_ROOT}" -name '99-gost-stability.conf.bak.*' -type f | wc -l | tr -d ' ')"
: > "${COMMAND_LOG}"
managed_update_output="${TEST_HOME}/managed-update.out"
if run_server_stability > "${managed_update_output}" 2>&1; then
  pass "managed sysctl update succeeds"
else
  fail "managed sysctl update succeeds"
fi
backup_count_after="$(find "${SYSCTL_ROOT}" -name '99-gost-stability.conf.bak.*' -type f | wc -l | tr -d ' ')"
assert_eq "managed sysctl update creates one backup" "$((backup_count_before + 1))" "${backup_count_after}"
assert_contains "managed sysctl update is reported" "Kernel configuration: Updated" "${managed_update_output}"
assert_eq "managed sysctl update applies once" "1" "$(count_log_line 'sysctl --system')"

symlink_target="${TEST_HOME}/sysctl-symlink-target"
printf 'SYMLINK_TARGET_CANARY\n' > "${symlink_target}"
rm -f "${sysctl_file}"
ln -s "${symlink_target}" "${sysctl_file}"
: > "${COMMAND_LOG}"
symlink_output="${TEST_HOME}/symlink.out"
if run_server_stability > "${symlink_output}" 2>&1; then
  fail "symlinked sysctl destination is rejected"
else
  pass "symlinked sysctl destination is rejected"
fi
if [[ -L "${sysctl_file}" ]]; then
  pass "sysctl destination remains a symlink"
else
  fail "sysctl destination remains a symlink"
fi
assert_eq "sysctl symlink target remains unchanged" "SYMLINK_TARGET_CANARY" "$(sed -n '1p' "${symlink_target}")"
assert_contains "symlink rejection is reported" "Failed: unsafe path or symlink" "${symlink_output}"
assert_eq "symlink rejection performs no sysctl apply" "0" "$(count_log_line 'sysctl --system')"

rm -f "${sysctl_file}"
render_stability_sysctl_config > "${sysctl_file}"
override_target="${TEST_HOME}/override-symlink-target"
printf 'OVERRIDE_TARGET_CANARY\n' > "${override_target}"
rm -f "${iran_override}"
ln -s "${override_target}" "${iran_override}"
: > "${COMMAND_LOG}"
override_symlink_output="${TEST_HOME}/override-symlink.out"
if run_server_stability > "${override_symlink_output}" 2>&1; then
  fail "symlinked systemd override is rejected"
else
  pass "symlinked systemd override is rejected"
fi
assert_eq "systemd override symlink target remains unchanged" "OVERRIDE_TARGET_CANARY" "$(sed -n '1p' "${override_target}")"
assert_contains "systemd override rejection is reported" "Failed: unsafe or unmanaged override path" "${override_symlink_output}"
assert_eq "failed override requires no daemon reload" "0" "$(count_log_line 'systemctl daemon-reload')"
assert_not_contains "failed override still issues no restart" "systemctl restart" "${COMMAND_LOG}"

rm -f "${iran_override}"
render_stability_systemd_override > "${iran_override}"
unit_symlink_target="${TEST_HOME}/unit-symlink-target"
printf 'UNIT_SYMLINK_TARGET_CANARY\n' > "${unit_symlink_target}"
ln -s "${unit_symlink_target}" "${SYSTEMD_ROOT}/gost-iran-3.service"
: > "${COMMAND_LOG}"
unit_symlink_output="${TEST_HOME}/unit-symlink.out"
if run_server_stability > "${unit_symlink_output}" 2>&1; then
  fail "symlinked exact GOST unit is rejected"
else
  pass "symlinked exact GOST unit is rejected"
fi
assert_contains "symlinked exact unit is reported" "gost-iran-3.service" "${unit_symlink_output}"
assert_eq "unit symlink target remains unchanged" "UNIT_SYMLINK_TARGET_CANARY" "$(sed -n '1p' "${unit_symlink_target}")"
assert_eq "unit symlink rejection performs no daemon reload" "0" "$(count_log_line 'systemctl daemon-reload')"
assert_not_contains "unit symlink rejection performs no restart" "systemctl restart" "${COMMAND_LOG}"

original_systemd_dir="${SYSTEMD_DIR}"
empty_systemd_dir="${TEST_HOME}/empty-systemd"
mkdir "${empty_systemd_dir}"
SYSTEMD_DIR="${empty_systemd_dir}"
: > "${COMMAND_LOG}"
no_service_output="${TEST_HOME}/no-service.out"
if run_server_stability > "${no_service_output}" 2>&1; then
  pass "stability run with no GOST services succeeds"
else
  fail "stability run with no GOST services succeeds"
fi
assert_contains "no-service report is explicit" "No managed GOST services detected." "${no_service_output}"
assert_contains "no-service report optimizes zero services" "Services optimized: 0" "${no_service_output}"
assert_eq "no-service run performs no daemon reload" "0" "$(count_log_line 'systemctl daemon-reload')"
assert_not_contains "no-service run performs no lifecycle command" "systemctl " "${COMMAND_LOG}"
SYSTEMD_DIR="${original_systemd_dir}"

finish_suite
