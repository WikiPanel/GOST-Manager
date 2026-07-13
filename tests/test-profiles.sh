#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=integration-test-lib.sh
source "${ROOT_DIR}/tests/integration-test-lib.sh"

TEST_HOME="$(mktemp -d "${TMPDIR:-/tmp}/gost-profile-tests.XXXXXX")"
cleanup() {
  cleanup_status=$?
  rm -rf "${TEST_HOME}"
  exit "${cleanup_status}"
}
trap cleanup EXIT

ENV_DIR="${TEST_HOME}/etc/gost"
UNIT_DIR="${TEST_HOME}/systemd"
STUB_BIN="${TEST_HOME}/bin"
COMMAND_LOG="${TEST_HOME}/commands.log"
SS_FIXTURE="${TEST_HOME}/ss.txt"
SS_AFTER_FIXTURE="${TEST_HOME}/ss-after.txt"
SS_COUNT_FILE="${TEST_HOME}/ss-count"
SYSTEMD_STATE_DIR="${TEST_HOME}/systemd-state"
IPTABLES_STATE="${TEST_HOME}/iptables.state"
IPTABLES_LOG="${TEST_HOME}/iptables.log"
IPTABLES_MUTATIONS="${TEST_HOME}/iptables.mutations"
TRANSACTION_LOG="${TEST_HOME}/transaction.log"
mkdir -p "${ENV_DIR}" "${UNIT_DIR}" "${STUB_BIN}" "${SYSTEMD_STATE_DIR}"
: > "${COMMAND_LOG}"
: > "${SS_FIXTURE}"
: > "${SS_AFTER_FIXTURE}"
: > "${IPTABLES_STATE}"
: > "${IPTABLES_LOG}"
: > "${TRANSACTION_LOG}"
printf '0\n' > "${SS_COUNT_FILE}"
printf '0\n' > "${IPTABLES_MUTATIONS}"

cat > "${STUB_BIN}/ss" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'ss %s\n' "$*" >> "${TRANSACTION_LOG}"
count="$(cat "${SS_COUNT_FILE}")"
printf '%s\n' "$((count + 1))" > "${SS_COUNT_FILE}"
if [[ "${SS_USE_AFTER_FIRST:-0}" == "1" && "${count}" -ge 1 ]]; then
  cat "${SS_AFTER_FIXTURE}"
else
  cat "${SS_FIXTURE}"
fi
STUB
cat > "${STUB_BIN}/systemctl" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'systemctl %s\n' "$*" >> "${COMMAND_LOG}"
printf 'systemctl %s\n' "$*" >> "${TRANSACTION_LOG}"
action="${1:-}"
last=""
service=""
for value in "$@"; do
  last="${value}"
  [[ "${value}" == gost-*.service ]] && service="${value}"
done
service="${service:-${last}}"

state_path() { printf '%s/%s.state\n' "${SYSTEMD_STATE_DIR}" "$1"; }
default_pid() {
  case "$1" in
    gost-iran-*.service) number="${1#gost-iran-}"; number="${number%.service}"; printf '%s\n' "$((100 + number))" ;;
    gost-kharej-*.service) number="${1#gost-kharej-}"; number="${number%.service}"; printf '%s\n' "$((200 + number))" ;;
    *) printf '900\n' ;;
  esac
}
ensure_state() {
  local service="$1" path
  path="$(state_path "${service}")"
  [[ -f "${path}" ]] && return 0
  if [[ -f "${GOST_SYSTEMD_DIR_TEST}/${service}" ]]; then
    printf 'loaded|disabled|inactive|0\n' > "${path}"
  else
    printf 'not-found|none|inactive|0\n' > "${path}"
  fi
}
read_state() {
  local service="$1"
  ensure_state "${service}"
  IFS='|' read -r load_state unit_state active_state main_pid < "$(state_path "${service}")"
}
write_state() {
  printf '%s|%s|%s|%s\n' "$2" "$3" "$4" "$5" > "$(state_path "$1")"
}
should_fail=0
if [[ "${STUB_FAIL_ACTION:-}" == "${action}" ]]; then should_fail=1; fi
if [[ "${STUB_FAIL_SECONDARY_ACTION:-}" == "${action}" ]]; then should_fail=1; fi
if [[ "${STUB_FAIL_TERTIARY_ACTION:-}" == "${action}" ]]; then should_fail=1; fi
if [[ "${STUB_FAIL_ONCE_ACTION:-}" == "${action}" && ! -e "${STUB_FAIL_ONCE_MARKER:-}" ]]; then
  touch "${STUB_FAIL_ONCE_MARKER}"
  should_fail=1
fi
case "${action}" in
  show)
    read_state "${service}"
    if [[ " $* " == *" --property=LoadState,UnitFileState,ActiveState,MainPID "* ]]; then
      printf 'LoadState=%s\nUnitFileState=%s\nActiveState=%s\nMainPID=%s\n' "${load_state}" "${unit_state}" "${active_state}" "${main_pid}"
    elif [[ " $* " == *" MainPID "* ]]; then
      printf '%s\n' "${main_pid}"
    elif [[ " $* " == *" SubState "* ]]; then
      if [[ "${active_state}" == "active" ]]; then printf 'running\n'; else printf 'dead\n'; fi
    else
      printf 'Id=%s\nLoadState=%s\nActiveState=%s\nSubState=%s\nUnitFileState=%s\nNRestarts=0\nMainPID=%s\n' \
        "${service}" "${load_state}" "${active_state}" "$(if [[ "${active_state}" == "active" ]]; then printf running; else printf dead; fi)" "${unit_state}" "${main_pid}"
    fi
    ;;
  is-active)
    read_state "${service}"
    [[ "${STUB_INACTIVE:-0}" != "1" && "${active_state}" == "active" ]]
    [[ " $* " == *" --quiet "* ]] || printf '%s\n' "${active_state}"
    ;;
  is-enabled)
    read_state "${service}"
    printf '%s\n' "${unit_state}"
    [[ "${unit_state}" == "enabled" || "${unit_state}" == "enabled-runtime" ]]
    ;;
  enable)
    read_state "${service}"
    if [[ "${should_fail}" -eq 1 && "${STUB_ENABLE_FAIL_MODE:-}" == "inactive" ]]; then
      write_state "${service}" "${load_state}" enabled inactive 0
      exit 1
    fi
    if [[ "${should_fail}" -eq 1 && "${STUB_PARTIAL_FAIL_ACTION:-}" != "enable" ]]; then exit 1; fi
    unit_state="enabled"
    if [[ " $* " == *" --now "* ]]; then active_state="active"; main_pid="$(default_pid "${service}")"; fi
    write_state "${service}" "${load_state}" "${unit_state}" "${active_state}" "${main_pid}"
    [[ "${should_fail}" -eq 0 ]]
    ;;
  disable)
    read_state "${service}"
    if [[ "${should_fail}" -eq 1 && "${STUB_PARTIAL_FAIL_ACTION:-}" != "disable" ]]; then exit 1; fi
    unit_state="disabled"
    if [[ " $* " == *" --now "* ]]; then active_state="inactive"; main_pid=0; fi
    write_state "${service}" "${load_state}" "${unit_state}" "${active_state}" "${main_pid}"
    [[ "${should_fail}" -eq 0 ]]
    ;;
  start)
    read_state "${service}"
    if [[ "${should_fail}" -eq 1 && "${STUB_PARTIAL_FAIL_ACTION:-}" != "start" ]]; then exit 1; fi
    active_state="active"
    if [[ "${STUB_START_BAD_PID:-0}" == "1" ]]; then main_pid=0; else main_pid="$(default_pid "${service}")"; fi
    write_state "${service}" "${load_state}" "${unit_state}" "${active_state}" "${main_pid}"
    [[ "${should_fail}" -eq 0 ]]
    ;;
  stop)
    read_state "${service}"
    if [[ "${should_fail}" -eq 1 && "${STUB_PARTIAL_FAIL_ACTION:-}" != "stop" ]]; then exit 1; fi
    active_state="inactive"; main_pid=0
    write_state "${service}" "${load_state}" "${unit_state}" "${active_state}" "${main_pid}"
    [[ "${should_fail}" -eq 0 ]]
    ;;
  restart)
    read_state "${service}"
    if [[ "${should_fail}" -eq 1 && "${STUB_PARTIAL_FAIL_ACTION:-}" != "restart" ]]; then exit 1; fi
    active_state="active"; main_pid="$(default_pid "${service}")"
    write_state "${service}" "${load_state}" "${unit_state}" "${active_state}" "${main_pid}"
    [[ "${should_fail}" -eq 0 ]]
    ;;
  daemon-reload)
    [[ "${should_fail}" -eq 0 ]]
    ;;
  *) [[ "${should_fail}" -eq 0 ]] ;;
esac
STUB
cat > "${STUB_BIN}/iptables" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'iptables %s\n' "$*" >> "${IPTABLES_LOG}"
printf 'iptables %s\n' "$*" >> "${TRANSACTION_LOG}"
operation="${1:-}"
if [[ "${operation}" == "-S" ]]; then
  cat "${IPTABLES_STATE}"
  exit 0
fi
count="$(cat "${IPTABLES_MUTATIONS}")"
count=$((count + 1))
printf '%s\n' "${count}" > "${IPTABLES_MUTATIONS}"
if [[ -n "${IPTABLES_FAIL_AT:-}" && "${count}" == "${IPTABLES_FAIL_AT}" ]]; then exit 1; fi
chain="${2:-}"
tmp="${IPTABLES_STATE}.tmp"
case "${operation}" in
  -I)
    position="${3}"
    shift 3
    line="-A ${chain} $*"
    awk -v chain="${chain}" -v position="${position}" -v line="${line}" '
      $1 == "-A" && $2 == chain { rule_number++ }
      rule_number == position && !inserted { print line; inserted=1 }
      { print }
      END { if (!inserted) print line }
    ' "${IPTABLES_STATE}" > "${tmp}"
    mv "${tmp}" "${IPTABLES_STATE}"
    ;;
  -D)
    if [[ "$#" -eq 3 && "${3}" =~ ^[1-9][0-9]*$ ]]; then
      position="${3}"
      awk -v chain="${chain}" -v position="${position}" '
        $1 == "-A" && $2 == chain { rule_number++ }
        rule_number == position && !removed { removed=1; next }
        { print }
        END { if (!removed) exit 1 }
      ' "${IPTABLES_STATE}" > "${tmp}" || { rm -f "${tmp}"; exit 1; }
    else
      shift 2
      target="-A ${chain} $*"
      awk -v target="${target}" 'BEGIN { removed=0 } { if (!removed && $0 == target) { removed=1; next } print } END { if (!removed) exit 1 }' \
        "${IPTABLES_STATE}" > "${tmp}" || { rm -f "${tmp}"; exit 1; }
    fi
    mv "${tmp}" "${IPTABLES_STATE}"
    ;;
  -A)
    shift 2
    printf -- '-A %s %s\n' "${chain}" "$*" >> "${IPTABLES_STATE}"
    ;;
  *) exit 1 ;;
esac
STUB
cat > "${STUB_BIN}/journalctl" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'journalctl %s\n' "$*" >> "${COMMAND_LOG}"
printf 'gost started socks5://user-1:credential-canary-29@203.0.113.1:28420\n'
STUB
chmod 755 "${STUB_BIN}/ss" "${STUB_BIN}/systemctl" "${STUB_BIN}/iptables" "${STUB_BIN}/journalctl"

export COMMAND_LOG SS_FIXTURE SS_AFTER_FIXTURE SS_COUNT_FILE SYSTEMD_STATE_DIR
export IPTABLES_STATE IPTABLES_LOG IPTABLES_MUTATIONS TRANSACTION_LOG
export PATH="${STUB_BIN}:${PATH}"
export GOST_MANAGER_TESTING=1
export GOST_ETC_DIR_TEST="${ENV_DIR}"
export GOST_SYSTEMD_DIR_TEST="${UNIT_DIR}"
export GOST_MONITOR_BIN_TEST="${TEST_HOME}/missing-monitor"
# shellcheck source=../gost-manager.sh
source "${ROOT_DIR}/gost-manager.sh"

assert_ok() {
  local name="$1"
  shift
  if "$@"; then pass "${name}"; else fail "${name}"; fi
}

assert_fails() {
  local name="$1"
  shift
  if "$@" >/dev/null 2>&1; then fail "${name}"; else pass "${name}"; fi
}

write_iran() {
  local number="$1" ports="$2" label="${3:-}" secret="${4:-profile-canary}"
  cat > "${ENV_DIR}/iran-${number}.env" <<EOF
GOST_USER=user-${number}
GOST_PASS=${secret}
KHAREJ_IP=203.0.113.${number}
TUNNEL_PORT=$((28419 + number))
MAPPINGS=${ports}
${label:+PROFILE_LABEL=${label}}
EOF
  chmod 600 "${ENV_DIR}/iran-${number}.env"
  printf '[Service]\n' > "${UNIT_DIR}/gost-iran-${number}.service"
  chmod 644 "${UNIT_DIR}/gost-iran-${number}.service"
  printf 'loaded|enabled|active|%s\n' "$((100 + number))" > "${SYSTEMD_STATE_DIR}/gost-iran-${number}.service.state"
}

write_kharej() {
  local number="$1" port="$2" sources="${3:-198.51.100.10}" label="${4:-}"
  cat > "${ENV_DIR}/kharej-${number}.env" <<EOF
GOST_USER=user-k-${number}
GOST_PASS=kharej-canary-${number}
TUNNEL_PORT=${port}
ALLOWED_IRAN_SOURCES=${sources}
FIREWALL_ENABLED=0
${label:+PROFILE_LABEL=${label}}
EOF
  chmod 600 "${ENV_DIR}/kharej-${number}.env"
  printf '[Service]\n' > "${UNIT_DIR}/gost-kharej-${number}.service"
  chmod 644 "${UNIT_DIR}/gost-kharej-${number}.service"
  printf 'loaded|enabled|active|%s\n' "$((200 + number))" > "${SYSTEMD_STATE_DIR}/gost-kharej-${number}.service.state"
}

assert_eq "next free Iran is 1 when empty" "1" "$(next_free_profile_number iran)"
assert_eq "next free Kharej is independently 1" "1" "$(next_free_profile_number kharej)"
touch "${ENV_DIR}/iran-1.env" "${UNIT_DIR}/gost-iran-2.service" "${ENV_DIR}/iran-4.env"
assert_eq "env-only and unit-only identities occupy gaps" "3" "$(next_free_profile_number iran)"
touch "${ENV_DIR}/iran-bad.env" "${UNIT_DIR}/gost-iran-0.service"
assert_eq "invalid filenames do not occupy numbers" "3" "$(next_free_profile_number iran)"
assert_eq "Kharej number space remains independent" "1" "$(next_free_profile_number kharej)"
assert_ok "valid optional label" validate_profile_label edge.tehran_1~a
assert_ok "empty label remains valid" validate_profile_label ""
assert_fails "space in label rejected" validate_profile_label "edge tehran"
# shellcheck disable=SC2016
assert_fails "shell syntax in label rejected" validate_profile_label '$(id)'
assert_eq "sources canonicalize, deduplicate, and sort" "198.51.100.0/24,198.51.100.10/32" "$(canonicalize_allowed_sources '198.51.100.10,198.51.100.7/24,198.51.100.10/32')"
assert_ok "fast source validation accepts safe IPv4 and CIDR" validate_allowed_sources_syntax '198.51.100.10,198.51.100.7/24'
assert_fails "fast source validation rejects broad CIDR" validate_allowed_sources_syntax '192.0.0.0/7'
assert_fails "fast source validation rejects out-of-range IPv4" validate_allowed_sources_syntax '198.51.100.999'
assert_fails "fast source validation rejects ambiguous leading zero" validate_allowed_sources_syntax '198.51.100.010'
assert_fails "IPv6 source rejected" canonicalize_allowed_sources '2001:db8::1'
assert_fails "unsafe broad source rejected" canonicalize_allowed_sources '0.0.0.0/0'
assert_fails "source whitespace ambiguity rejected" canonicalize_allowed_sources '198.51.100.1, 198.51.100.2'

rm -f "${ENV_DIR}"/* "${UNIT_DIR}"/*
cat > "${ENV_DIR}/kharej-1.env" <<'EOF'
GOST_USER=legacy-user
GOST_PASS=legacy-canary
TUNNEL_PORT=28420
IRAN_IP=198.51.100.10
FIREWALL_ENABLED=1
UNKNOWN_SAFE=preserve-me
EOF
# shellcheck disable=SC2016
printf 'UNKNOWN_INERT=$(touch %s)\n' "${TEST_HOME}/must-not-exist" >> "${ENV_DIR}/kharej-1.env"
chmod 640 "${ENV_DIR}/kharej-1.env"
assert_ok "legacy IRAN_IP env parses" profile_env_load "${ENV_DIR}/kharej-1.env" kharej
assert_eq "legacy source becomes canonical /32" "198.51.100.10/32" "$(profile_sources_from_loaded)"
profile_env_set PROFILE_LABEL legacy-edge
write_loaded_profile_env "${ENV_DIR}/kharej-1.env" kharej
assert_contains "unknown env key preserved on edit" "UNKNOWN_SAFE=preserve-me" "${ENV_DIR}/kharej-1.env"
assert_contains "unknown shell-like value remains inert data" "UNKNOWN_INERT=\$(touch " "${ENV_DIR}/kharej-1.env"
assert_absent "strict env parser never executes unknown values" "${TEST_HOME}/must-not-exist"
assert_eq "edited env mode is private" "600" "$(mode_of "${ENV_DIR}/kharej-1.env")"
if find "${ENV_DIR}" -name '.*.tmp.*' -o -name '.*.restore.*' | grep -q .; then fail "atomic env temp is cleaned"; else pass "atomic env temp is cleaned"; fi
cat > "${ENV_DIR}/iran-9.env" <<'EOF'
GOST_USER=a
GOST_USER=b
GOST_PASS=c
KHAREJ_IP=203.0.113.9
TUNNEL_PORT=28429
MAPPINGS=9009:9009
EOF
assert_fails "duplicate known env key rejected" profile_env_load "${ENV_DIR}/iran-9.env" iran
ln -s "${TEST_HOME}/outside" "${ENV_DIR}/iran-8.env"
assert_fails "symlink env destination rejected" validate_managed_destination "${ENV_DIR}/iran-8.env" env

rm -f "${ENV_DIR}"/* "${UNIT_DIR}"/*
write_iran 1 '2052:80,2053:80' edge-iran credential-canary-29
write_kharej 1 28420 '198.51.100.10,198.51.100.11/32' edge-kharej
touch "${UNIT_DIR}/gost-iran-3.service"
inventory="${TEST_HOME}/inventory"
configured_port_inventory "${inventory}"
assert_contains "inventory contains first Iran mapping" "2052|iran-1" "${inventory}"
assert_contains "inventory contains second Iran mapping" "2053|iran-1" "${inventory}"
assert_contains "inventory contains Kharej SOCKS port" "28420|kharej-1" "${inventory}"
assert_contains "service-only profile is incomplete" "incomplete|iran-3" "${inventory}"
assert_fails "duplicate Iran local port rejected" validate_configured_ports iran-2 2052
assert_fails "cross-side local port conflict rejected" validate_configured_ports kharej-2 2053
assert_ok "duplicate target ports remain allowed" validate_configured_ports iran-1 2052,2053

: > "${SS_FIXTURE}"
assert_ok "free live port accepted" validate_live_ports_snapshot "${SS_FIXTURE}" 30000
printf 'LISTEN 0 4096 0.0.0.0:30000 0.0.0.0:* users:(("other",pid=900,fd=3))\n' > "${SS_FIXTURE}"
assert_fails "IPv4 wildcard listener conflicts" validate_live_ports_snapshot "${SS_FIXTURE}" 30000
printf 'LISTEN 0 4096 [::]:30000 [::]:*\n' > "${SS_FIXTURE}"
assert_fails "IPv6 wildcard with unknown owner conflicts" validate_live_ports_snapshot "${SS_FIXTURE}" 30000
printf 'LISTEN 0 4096 0.0.0.0:2052 0.0.0.0:* users:(("gost",pid=101,fd=3))\n' > "${SS_FIXTURE}"
assert_ok "unchanged exact profile listener is allowed" validate_live_ports_snapshot "${SS_FIXTURE}" 2052 iran-1 2052
assert_fails "same port without exact PID proof conflicts" validate_live_ports_snapshot "${SS_FIXTURE}" 2052 iran-2 2052

: > "${SS_FIXTURE}"
printf '0\n' > "${SS_COUNT_FILE}"
profile_env_load "${ENV_DIR}/iran-1.env" iran
assert_ok "multi-mapping validation succeeds from one snapshot" validate_profile_ports_before_write iran-1 2052,2053 iran-1 2052,2053
assert_eq "one ss call validates multiple mappings" "1" "$(cat "${SS_COUNT_FILE}")"

oversized_snapshot="${TEST_HOME}/oversized-ss.out"
python3 -c 'from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b"x" * (4 * 1024 * 1024 + 1))' "${SS_FIXTURE}"
printf '0\n' > "${SS_COUNT_FILE}"
assert_fails "oversized socket snapshot is rejected" take_listen_snapshot "${oversized_snapshot}"
assert_eq "oversized socket validation still runs ss once" "1" "$(cat "${SS_COUNT_FILE}")"

printf 'ESTAB 0 0 192.0.2.10:2052 198.51.100.10:443 users:(("gost",pid=101,fd=5))\n' > "${SS_FIXTURE}"
printf '0\n' > "${SS_COUNT_FILE}"
list_output="${TEST_HOME}/list.out"
list_profiles > "${list_output}"
assert_contains "profile list shows labelled Iran profile" "edge-iran" "${list_output}"
assert_contains "profile list shows labelled Kharej profile" "edge-kharej" "${list_output}"
assert_contains "profile list shows authoritative connection count" "1" "${list_output}"
assert_not_contains "profile list redacts Iran password" "credential-canary-29" "${list_output}"
assert_not_contains "profile list redacts Kharej password" "kharej-canary-1" "${list_output}"
assert_eq "profile list takes one socket snapshot" "1" "$(cat "${SS_COUNT_FILE}")"

write_iran 9 2099
printf 'PROFILE_LABEL=bad label\n' >> "${ENV_DIR}/iran-9.env"
invalid_list_output="${TEST_HOME}/invalid-list.out"
list_profiles > "${invalid_list_output}"
assert_contains "semantically malformed profile is marked invalid" "invalid" "${invalid_list_output}"
assert_not_contains "malformed profile label is never displayed" "bad label" "${invalid_list_output}"
rm -f "${ENV_DIR}/iran-9.env" "${UNIT_DIR}/gost-iran-9.service"

printf '0\n' > "${SS_COUNT_FILE}"
detail_output="${TEST_HOME}/detail.out"
show_selected_profile_detail iran 1 > "${detail_output}"
assert_contains "profile detail shows exact ID" "Profile ID: iran-1" "${detail_output}"
assert_contains "profile detail shows safe mappings" "2052:80,2053:80" "${detail_output}"
assert_not_contains "profile detail redacts credential" "credential-canary-29" "${detail_output}"
assert_eq "profile detail takes one socket snapshot" "1" "$(cat "${SS_COUNT_FILE}")"

select_existing_tunnel() {
  SELECTED_TUNNEL_SIDE=iran
  SELECTED_TUNNEL_NUMBER=1
  SELECTED_TUNNEL_SERVICE=gost-iran-1.service
  SELECTED_TUNNEL_SERVICE_FILE="${UNIT_DIR}/gost-iran-1.service"
  SELECTED_TUNNEL_ENV_FILE="${ENV_DIR}/iran-1.env"
}
: > "${COMMAND_LOG}"
printf '0\n' > "${SS_COUNT_FILE}"
status_output="${TEST_HOME}/safe-status.out"
show_status > "${status_output}"
assert_not_contains "safe status never prints credential" "credential-canary-29" "${status_output}"
assert_not_contains "safe status avoids raw systemctl status" "systemctl status" "${COMMAND_LOG}"
assert_eq "safe status takes one bounded socket snapshot" "1" "$(cat "${SS_COUNT_FILE}")"
logs_output="${TEST_HOME}/safe-logs.out"
show_logs > "${logs_output}"
assert_contains "logs redact password" "[redacted-password]" "${logs_output}"
assert_contains "logs redact username" "[redacted-user]" "${logs_output}"
assert_not_contains "logs never print credential" "credential-canary-29" "${logs_output}"
: > "${COMMAND_LOG}"
restart_output="${TEST_HOME}/safe-restart.out"
(require_root() { return 0; }; restart_tunnel) > "${restart_output}"
assert_not_contains "restart status never prints credential" "credential-canary-29" "${restart_output}"
assert_not_contains "restart avoids raw systemctl status" "systemctl status" "${COMMAND_LOG}"
assert_contains "restart targets the exact selected service" "restart gost-iran-1.service" "${COMMAND_LOG}"

before_checksum="$(cksum "${ENV_DIR}/iran-1.env")"
: > "${COMMAND_LOG}"
require_root() { return 0; }
ensure_commands() { return 0; }
edit_output="${TEST_HOME}/edit-noop.out"
edit_profile <<< $'\n\n\n\n\n' > "${edit_output}" 2>&1
assert_contains "no-op edit is explicit" "No changes detected" "${edit_output}"
assert_eq "no-op edit preserves exact env bytes" "${before_checksum}" "$(cksum "${ENV_DIR}/iran-1.env")"
assert_not_contains "no-op edit sends no restart" "restart" "${COMMAND_LOG}"

printf 'LISTEN 0 4096 0.0.0.0:2052 0.0.0.0:* users:(("gost",pid=101,fd=3))\nLISTEN 0 4096 0.0.0.0:2053 0.0.0.0:* users:(("gost",pid=101,fd=4))\n' > "${SS_FIXTURE}"
export STUB_FAIL_ONCE_ACTION=restart
export STUB_FAIL_ONCE_MARKER="${TEST_HOME}/restart-failed-once"
rollback_output="${TEST_HOME}/edit-rollback.out"
if edit_profile <<< $'edge-rollback\n\n\n\n\n2052:81,2053:444\ny\ny\n' > "${rollback_output}" 2>&1; then
  fail "failed restart makes edit return nonzero"
else
  pass "failed restart makes edit return nonzero"
fi
unset STUB_FAIL_ONCE_ACTION STUB_FAIL_ONCE_MARKER
assert_eq "edit rollback restores exact env bytes" "${before_checksum}" "$(cksum "${ENV_DIR}/iran-1.env")"
assert_not_contains "edit rollback output redacts password" "credential-canary-29" "${rollback_output}"
if find "${ENV_DIR}" -name '.iran-1.env.rollback.*' | grep -q .; then fail "verified edit rollback removes recovery snapshot"; else pass "verified edit rollback removes recovery snapshot"; fi

kharej_checksum_before_edit="$(cksum "${ENV_DIR}/kharej-1.env")"
kharej_mode_before_edit="$(mode_of "${ENV_DIR}/kharej-1.env")"
: > "${COMMAND_LOG}"
edit_success_output="${TEST_HOME}/edit-success.out"
edit_profile <<< $'edge-updated\n\n\n\n\n2052:82,2053:445\ny\ny\n' > "${edit_success_output}" 2>&1
assert_contains "successful edit stores new safe label" "PROFILE_LABEL=edge-updated" "${ENV_DIR}/iran-1.env"
assert_contains "successful edit stores new mappings" "MAPPINGS=2052:82,2053:445" "${ENV_DIR}/iran-1.env"
assert_not_contains "successful edit output redacts credential" "credential-canary-29" "${edit_success_output}"
assert_contains "successful edit restarts exact selected service" "restart gost-iran-1.service" "${COMMAND_LOG}"
assert_not_contains "successful edit sends no unselected service command" "gost-kharej-1.service" "${COMMAND_LOG}"
assert_not_contains "successful edit invokes no NGINX command" "nginx" "${COMMAND_LOG}"
assert_not_contains "successful edit invokes no Gateway command" "gateway" "${COMMAND_LOG}"
assert_not_contains "successful edit invokes no monitoring lifecycle command" "gost-monitor" "${COMMAND_LOG}"
assert_eq "successful edit keeps unselected env byte-identical" "${kharej_checksum_before_edit}" "$(cksum "${ENV_DIR}/kharej-1.env")"
assert_eq "successful edit keeps unselected env mode" "${kharej_mode_before_edit}" "$(mode_of "${ENV_DIR}/kharej-1.env")"

: > "${SS_FIXTURE}"
: > "${COMMAND_LOG}"
printf 'LISTEN 0 4096 0.0.0.0:3052 0.0.0.0:* users:(("gost",pid=102,fd=3))\n' > "${SS_AFTER_FIXTURE}"
printf '0\n' > "${SS_COUNT_FILE}"
export SS_USE_AFTER_FIRST=1
profile_env_reset
profile_env_set GOST_USER create-user
profile_env_set GOST_PASS create-canary-secret
profile_env_set KHAREJ_IP 203.0.113.30
profile_env_set TUNNEL_PORT 29430
profile_env_set MAPPINGS 3052:80
profile_env_set PROFILE_LABEL created-edge
create_output="${TEST_HOME}/create.out"
assert_ok "new exact profile installs and starts" install_new_profile_from_loaded iran 2 1 > "${create_output}"
assert_file "new profile env created" "${ENV_DIR}/iran-2.env"
assert_file "new profile unit created" "${UNIT_DIR}/gost-iran-2.service"
assert_eq "new profile env mode is 0600" "600" "$(mode_of "${ENV_DIR}/iran-2.env")"
assert_eq "new profile unit mode is 0644" "644" "$(mode_of "${UNIT_DIR}/gost-iran-2.service")"
assert_not_contains "create output redacts password" "create-canary-secret" "${create_output}"
assert_contains "create starts only exact new service" "enable --now gost-iran-2.service" "${COMMAND_LOG}"
unset SS_USE_AFTER_FIRST

clone_source_checksum="$(cksum "${ENV_DIR}/iran-1.env")"
clone_source_unit_checksum="$(cksum "${UNIT_DIR}/gost-iran-1.service")"
clone_source_unit_mode="$(mode_of "${UNIT_DIR}/gost-iran-1.service")"
: > "${COMMAND_LOG}"
clone_success_output="${TEST_HOME}/clone-success.out"
clone_profile <<< $'y\n\nclone-ui\n\n\n5052:82,5053:445\ny\nn\n' > "${clone_success_output}" 2>&1
assert_file "clone workflow creates a new env" "${ENV_DIR}/iran-4.env"
assert_file "clone workflow creates a new unit" "${UNIT_DIR}/gost-iran-4.service"
assert_contains "clone workflow stores new unique mappings" "MAPPINGS=5052:82,5053:445" "${ENV_DIR}/iran-4.env"
assert_eq "clone workflow leaves source byte-identical" "${clone_source_checksum}" "$(cksum "${ENV_DIR}/iran-1.env")"
assert_eq "clone workflow leaves source unit byte-identical" "${clone_source_unit_checksum}" "$(cksum "${UNIT_DIR}/gost-iran-1.service")"
assert_eq "clone workflow leaves source unit mode" "${clone_source_unit_mode}" "$(mode_of "${UNIT_DIR}/gost-iran-1.service")"
if [[ "$(env_get GOST_PASS "${ENV_DIR}/iran-4.env")" == "$(env_get GOST_PASS "${ENV_DIR}/iran-1.env")" ]]; then pass "clone can reuse credentials without displaying them"; else fail "clone can reuse credentials without displaying them"; fi
assert_not_contains "clone workflow output redacts credential" "credential-canary-29" "${clone_success_output}"
assert_not_contains "clone created without start sends no enable" "enable --now gost-iran-4.service" "${COMMAND_LOG}"

source_checksum="$(cksum "${ENV_DIR}/iran-1.env")"
profile_env_reset
profile_env_set GOST_USER clone-user
profile_env_set GOST_PASS clone-canary-secret
profile_env_set KHAREJ_IP 203.0.113.40
profile_env_set TUNNEL_PORT 29440
profile_env_set MAPPINGS 4052:80
profile_env_set PROFILE_LABEL cloned-edge
export STUB_FAIL_ACTION=enable
clone_failure_output="${TEST_HOME}/clone-failure.out"
assert_fails "clone activation failure returns nonzero" install_new_profile_from_loaded iran 5 1 > "${clone_failure_output}"
unset STUB_FAIL_ACTION
assert_absent "failed clone removes only clone env" "${ENV_DIR}/iran-5.env"
assert_absent "failed clone removes only clone unit" "${UNIT_DIR}/gost-iran-5.service"
assert_eq "failed clone leaves source byte-identical" "${source_checksum}" "$(cksum "${ENV_DIR}/iran-1.env")"
assert_not_contains "clone failure output redacts credential" "clone-canary-secret" "${clone_failure_output}"

: > "${COMMAND_LOG}"
export STUB_FAIL_ACTION=disable
delete_output="${TEST_HOME}/delete-failure.out"
if (confirm() { return 0; }; delete_tunnel) > "${delete_output}" 2>&1; then fail "delete stop failure returns nonzero"; else pass "delete stop failure returns nonzero"; fi
unset STUB_FAIL_ACTION
assert_file "delete stop failure preserves env" "${ENV_DIR}/iran-1.env"
assert_file "delete stop failure preserves unit" "${UNIT_DIR}/gost-iran-1.service"
assert_contains "delete targets exact service" "disable --now gost-iran-1.service" "${COMMAND_LOG}"
assert_not_contains "delete never uses reset-failed" "reset-failed" "${COMMAND_LOG}"

: > "${COMMAND_LOG}"
if (confirm() { return 0; }; restart_profile_selection 'iran-1,iran-1,kharej-1' 0) >/dev/null; then pass "restart selected profiles succeeds"; else fail "restart selected profiles succeeds"; fi
assert_eq "duplicate restart selection is deduplicated" "2" "$(grep -c '^systemctl restart ' "${COMMAND_LOG}")"
assert_contains "restart selected includes exact Iran service" "restart gost-iran-1.service" "${COMMAND_LOG}"
assert_contains "restart selected includes exact Kharej service" "restart gost-kharej-1.service" "${COMMAND_LOG}"
assert_not_contains "restart selected uses no wildcard" "gost-*" "${COMMAND_LOG}"
: > "${COMMAND_LOG}"
if (confirm() { return 0; }; restart_profile_selection all 1) >/dev/null; then pass "restart all profiles succeeds after strong confirmation"; else fail "restart all profiles succeeds after strong confirmation"; fi
assert_contains "restart all includes Iran one" "restart gost-iran-1.service" "${COMMAND_LOG}"
assert_contains "restart all includes Iran two" "restart gost-iran-2.service" "${COMMAND_LOG}"
assert_contains "restart all includes Kharej one" "restart gost-kharej-1.service" "${COMMAND_LOG}"
assert_not_contains "restart all still uses no wildcard" "gost-*" "${COMMAND_LOG}"
: > "${COMMAND_LOG}"
assert_fails "unknown restart profile is rejected" restart_profile_selection iran-99 0
assert_eq "unknown restart sends no systemctl" "0" "$(wc -l < "${COMMAND_LOG}" | tr -d ' ')"

selected_source_checksum="$(cksum "${ENV_DIR}/iran-1.env")"
select_existing_tunnel() {
  SELECTED_TUNNEL_SIDE=iran
  SELECTED_TUNNEL_NUMBER=2
  SELECTED_TUNNEL_SERVICE=gost-iran-2.service
  SELECTED_TUNNEL_SERVICE_FILE="${UNIT_DIR}/gost-iran-2.service"
  SELECTED_TUNNEL_ENV_FILE="${ENV_DIR}/iran-2.env"
}
delete_restore_env_checksum="$(cksum "${ENV_DIR}/iran-2.env")"
delete_restore_unit_checksum="$(cksum "${UNIT_DIR}/gost-iran-2.service")"
: > "${COMMAND_LOG}"
export STUB_FAIL_ONCE_ACTION=daemon-reload
export STUB_FAIL_ONCE_MARKER="${TEST_HOME}/delete-daemon-reload.failed"
if (confirm() { return 0; }; delete_tunnel) >/dev/null 2>&1; then fail "delete daemon-reload failure returns nonzero"; else pass "delete daemon-reload failure returns nonzero"; fi
unset STUB_FAIL_ONCE_ACTION STUB_FAIL_ONCE_MARKER
assert_eq "delete daemon-reload failure restores env bytes" "${delete_restore_env_checksum}" "$(cksum "${ENV_DIR}/iran-2.env")"
assert_eq "delete daemon-reload failure restores unit bytes" "${delete_restore_unit_checksum}" "$(cksum "${UNIT_DIR}/gost-iran-2.service")"
assert_contains "delete daemon-reload failure restores selected service state" "start gost-iran-2.service" "${COMMAND_LOG}"
assert_not_contains "delete daemon-reload failure leaves unselected service state" "gost-iran-1.service" "${COMMAND_LOG}"
: > "${COMMAND_LOG}"
if (confirm() { return 0; }; delete_tunnel) >/dev/null; then pass "successful delete removes exact selected profile"; else fail "successful delete removes exact selected profile"; fi
assert_absent "successful delete removes selected env" "${ENV_DIR}/iran-2.env"
assert_absent "successful delete removes selected unit" "${UNIT_DIR}/gost-iran-2.service"
assert_eq "successful delete preserves unselected env bytes" "${selected_source_checksum}" "$(cksum "${ENV_DIR}/iran-1.env")"
assert_contains "successful delete disables exact selected service" "disable --now gost-iran-2.service" "${COMMAND_LOG}"
assert_not_contains "successful delete sends no Iran one command" "gost-iran-1.service" "${COMMAND_LOG}"

# Transactional Kharej port/firewall edit regressions.
select_existing_tunnel() {
  SELECTED_TUNNEL_SIDE=kharej
  SELECTED_TUNNEL_NUMBER=1
  SELECTED_TUNNEL_SERVICE=gost-kharej-1.service
  SELECTED_TUNNEL_SERVICE_FILE="${UNIT_DIR}/gost-kharej-1.service"
  SELECTED_TUNNEL_ENV_FILE="${ENV_DIR}/kharej-1.env"
}
profile_env_load "${ENV_DIR}/kharej-1.env" kharej
profile_env_set FIREWALL_ENABLED 1
write_loaded_profile_env "${ENV_DIR}/kharej-1.env" kharej
printf '%s\n' \
  '-P INPUT ACCEPT' \
  '-A INPUT -p tcp --dport 22 -m comment --comment unrelated:ssh -j ACCEPT' \
  '-A INPUT -p tcp -s 198.51.100.10/32 --dport 28420 -m comment --comment gost-manager:kharej-1:allow -j ACCEPT' \
  '-A INPUT -p tcp -s 198.51.100.11/32 --dport 28420 -m comment --comment gost-manager:kharej-1:allow -j ACCEPT' \
  '-A INPUT -p tcp --dport 28420 -m comment --comment gost-manager:kharej-1:drop -j DROP' > "${IPTABLES_STATE}"
printf 'loaded|enabled|active|201\n' > "${SYSTEMD_STATE_DIR}/gost-kharej-1.service.state"

kharej_before_decline="$(cksum "${ENV_DIR}/kharej-1.env")"
firewall_before_decline="$(cksum "${IPTABLES_STATE}")"
: > "${COMMAND_LOG}"
: > "${TRANSACTION_LOG}"
: > "${SS_FIXTURE}"
printf '0\n' > "${SS_COUNT_FILE}"
decline_output="${TEST_HOME}/kharej-port-decline.out"
edit_profile <<< $'\n\n\n29420\n\n\ny\nn\n' > "${decline_output}" 2>&1
assert_eq "protected port migration decline leaves env unchanged" "${kharej_before_decline}" "$(cksum "${ENV_DIR}/kharej-1.env")"
assert_eq "protected port migration decline leaves firewall unchanged" "${firewall_before_decline}" "$(cksum "${IPTABLES_STATE}")"
assert_eq "protected port migration decline sends no systemctl command" "0" "$(wc -l < "${COMMAND_LOG}" | tr -d ' ')"
assert_not_contains "protected port migration decline performs no firewall mutation" "iptables -I" "${TRANSACTION_LOG}"

: > "${COMMAND_LOG}"
: > "${TRANSACTION_LOG}"
: > "${SS_FIXTURE}"
printf 'LISTEN 0 4096 0.0.0.0:29420 0.0.0.0:* users:(("gost",pid=201,fd=3))\n' > "${SS_AFTER_FIXTURE}"
printf '0\n' > "${SS_COUNT_FILE}"
export SS_USE_AFTER_FIRST=1
migration_output="${TEST_HOME}/kharej-port-success.out"
edit_profile <<< $'\n\n\n29420\n\n\ny\ny\n' > "${migration_output}" 2>&1
unset SS_USE_AFTER_FIRST
assert_contains "successful protected migration stores candidate port" "TUNNEL_PORT=29420" "${ENV_DIR}/kharej-1.env"
assert_contains "successful protected migration installs final new-port rules" "--dport 29420" "${IPTABLES_STATE}"
assert_not_contains "successful protected migration removes obsolete old-port rules" "--dport 28420" "${IPTABLES_STATE}"
candidate_insert_line="$(awk '/^iptables -I INPUT 1 / {print NR; exit}' "${TRANSACTION_LOG}")"
restart_line="$(awk '/^systemctl restart gost-kharej-1.service$/ {print NR; exit}' "${TRANSACTION_LOG}")"
listener_verify_line="$(awk '/^ss / {line=NR} END {print line}' "${TRANSACTION_LOG}")"
old_rule_delete_line="$(awk '/^iptables -D INPUT [0-9]+$/ {print NR; exit}' "${TRANSACTION_LOG}")"
if [[ "${candidate_insert_line}" -lt "${restart_line}" ]]; then pass "candidate protection is installed before exact restart"; else fail "candidate protection is installed before exact restart"; fi
if [[ "${listener_verify_line}" -lt "${old_rule_delete_line}" ]]; then pass "old-port rules remain until candidate listener verification"; else fail "old-port rules remain until candidate listener verification"; fi
assert_not_contains "successful migration sends no unselected lifecycle command" "gost-iran-1.service" "${COMMAND_LOG}"

kharej_before_failed_migration="$(cksum "${ENV_DIR}/kharej-1.env")"
firewall_before_failed_migration="$(cksum "${IPTABLES_STATE}")"
: > "${COMMAND_LOG}"
: > "${TRANSACTION_LOG}"
: > "${SS_FIXTURE}"
printf 'LISTEN 0 4096 0.0.0.0:29420 0.0.0.0:* users:(("gost",pid=201,fd=3))\n' > "${SS_AFTER_FIXTURE}"
printf '0\n' > "${SS_COUNT_FILE}"
export SS_USE_AFTER_FIRST=1
export STUB_FAIL_ONCE_ACTION=restart
export STUB_FAIL_ONCE_MARKER="${TEST_HOME}/kharej-migration-restart.failed"
failed_migration_output="${TEST_HOME}/kharej-port-failure.out"
if edit_profile <<< $'\n\n\n30420\n\n\ny\ny\n' > "${failed_migration_output}" 2>&1; then fail "failed protected migration returns nonzero"; else pass "failed protected migration returns nonzero"; fi
unset SS_USE_AFTER_FIRST STUB_FAIL_ONCE_ACTION STUB_FAIL_ONCE_MARKER
assert_eq "failed protected migration restores exact env" "${kharej_before_failed_migration}" "$(cksum "${ENV_DIR}/kharej-1.env")"
assert_eq "failed protected migration restores exact firewall positions" "${firewall_before_failed_migration}" "$(cksum "${IPTABLES_STATE}")"
assert_contains "failed protected migration restores active enabled service" "loaded|enabled|active|201" "${SYSTEMD_STATE_DIR}/gost-kharej-1.service.state"
assert_contains "failed protected migration keeps old listener protected" "--dport 29420" "${IPTABLES_STATE}"
assert_contains "failed protected migration reports verified restoration" "exact prior env, firewall, service state, and listener were restored" "${failed_migration_output}"
assert_not_contains "failed migration sends no unselected lifecycle command" "gost-iran-1.service" "${COMMAND_LOG}"

: > "${COMMAND_LOG}"
printf 'LISTEN 0 4096 0.0.0.0:29420 0.0.0.0:* users:(("gost",pid=201,fd=3))\n' > "${SS_FIXTURE}"
source_edit_output="${TEST_HOME}/kharej-source-edit.out"
edit_profile <<< $'\n\n\n\n198.51.100.12\n\ny\n' > "${source_edit_output}" 2>&1
assert_contains "source-only edit stores canonical source" "ALLOWED_IRAN_SOURCES=198.51.100.12/32" "${ENV_DIR}/kharej-1.env"
assert_contains "source-only edit installs exact source rule" "-s 198.51.100.12/32" "${IPTABLES_STATE}"
assert_not_contains "source-only edit does not restart GOST" "restart gost-kharej-1.service" "${COMMAND_LOG}"
assert_contains "source-only edit reports no restart required" "no GOST restart was required" "${source_edit_output}"

: > "${COMMAND_LOG}"
firewall_disable_output="${TEST_HOME}/kharej-firewall-disable.out"
edit_profile <<< $'\n\n\n\n\n0\ny\n' > "${firewall_disable_output}" 2>&1
assert_contains "firewall disable stores disabled state" "FIREWALL_ENABLED=0" "${ENV_DIR}/kharej-1.env"
assert_not_contains "firewall disable removes managed rules" "gost-manager:kharej-1:" "${IPTABLES_STATE}"
assert_not_contains "firewall disable does not restart GOST" "restart gost-kharej-1.service" "${COMMAND_LOG}"

: > "${COMMAND_LOG}"
firewall_enable_output="${TEST_HOME}/kharej-firewall-enable.out"
edit_profile <<< $'\n\n\n\n\n1\ny\n' > "${firewall_enable_output}" 2>&1
assert_contains "firewall enable stores enabled state" "FIREWALL_ENABLED=1" "${ENV_DIR}/kharej-1.env"
assert_contains "firewall enable restores managed protection" "gost-manager:kharej-1:drop" "${IPTABLES_STATE}"
assert_not_contains "firewall enable does not restart GOST" "restart gost-kharej-1.service" "${COMMAND_LOG}"

# Partial create activation and rollback regressions.
prepare_iran_candidate() {
  local listen_port="$1"
  profile_env_reset
  profile_env_set GOST_USER create-state-user
  profile_env_set GOST_PASS create-state-canary
  profile_env_set KHAREJ_IP 203.0.113.50
  profile_env_set TUNNEL_PORT 29500
  profile_env_set MAPPINGS "${listen_port}:80"
}
: > "${SS_FIXTURE}"
prepare_iran_candidate 4106
export STUB_FAIL_ACTION=enable
export STUB_ENABLE_FAIL_MODE=inactive
create_inactive_failure="${TEST_HOME}/create-inactive-failure.out"
if install_new_profile_from_loaded iran 6 1 > "${create_inactive_failure}" 2>&1; then fail "enable/start failure before activation returns nonzero"; else pass "enable/start failure before activation returns nonzero"; fi
unset STUB_FAIL_ACTION STUB_ENABLE_FAIL_MODE
assert_absent "inactive partial activation removes env only after verified rollback" "${ENV_DIR}/iran-6.env"
assert_absent "inactive partial activation removes unit only after verified rollback" "${UNIT_DIR}/gost-iran-6.service"

prepare_iran_candidate 4107
export STUB_FAIL_ACTION=enable
export STUB_PARTIAL_FAIL_ACTION=enable
create_active_failure="${TEST_HOME}/create-active-failure.out"
if install_new_profile_from_loaded iran 7 1 > "${create_active_failure}" 2>&1; then fail "activation that returns failure after start returns nonzero"; else pass "activation that returns failure after start returns nonzero"; fi
unset STUB_FAIL_ACTION STUB_PARTIAL_FAIL_ACTION
assert_absent "active partial activation removes env after verified stop" "${ENV_DIR}/iran-7.env"
assert_absent "active partial activation removes unit after verified stop" "${UNIT_DIR}/gost-iran-7.service"

prepare_iran_candidate 4111
export STUB_FAIL_ACTION=enable
export STUB_PARTIAL_FAIL_ACTION=enable
export STUB_FAIL_SECONDARY_ACTION=disable
create_disable_failure="${TEST_HOME}/create-disable-failure.out"
if install_new_profile_from_loaded iran 11 1 > "${create_disable_failure}" 2>&1; then fail "create rollback disable failure returns nonzero"; else pass "create rollback disable failure returns nonzero"; fi
unset STUB_FAIL_ACTION STUB_PARTIAL_FAIL_ACTION STUB_FAIL_SECONDARY_ACTION
assert_file "create rollback disable failure retains env" "${ENV_DIR}/iran-11.env"
assert_file "create rollback disable failure retains unit" "${UNIT_DIR}/gost-iran-11.service"
assert_contains "create rollback disable failure retains enabled inactive state" "loaded|enabled|inactive|0" "${SYSTEMD_STATE_DIR}/gost-iran-11.service.state"

profile_env_reset
profile_env_set GOST_USER create-state-user
profile_env_set GOST_PASS create-state-canary
profile_env_set TUNNEL_PORT 32008
profile_env_set ALLOWED_IRAN_SOURCES 198.51.100.20/32
profile_env_set FIREWALL_ENABLED 1
export STUB_FAIL_ACTION=enable
export STUB_PARTIAL_FAIL_ACTION=enable
export STUB_FAIL_SECONDARY_ACTION=disable
export STUB_FAIL_TERTIARY_ACTION=stop
create_unverified_failure="${TEST_HOME}/create-unverified-failure.out"
if install_new_profile_from_loaded kharej 8 1 > "${create_unverified_failure}" 2>&1; then fail "failed create rollback with surviving service returns nonzero"; else pass "failed create rollback with surviving service returns nonzero"; fi
unset STUB_FAIL_ACTION STUB_PARTIAL_FAIL_ACTION STUB_FAIL_SECONDARY_ACTION STUB_FAIL_TERTIARY_ACTION
assert_file "unverified create rollback retains env beneath surviving service" "${ENV_DIR}/kharej-8.env"
assert_file "unverified create rollback retains unit beneath surviving service" "${UNIT_DIR}/gost-kharej-8.service"
assert_contains "unverified create rollback retains firewall dependency" "gost-manager:kharej-8:drop" "${IPTABLES_STATE}"
assert_contains "unverified create rollback leaves active state observable" "loaded|enabled|active|208" "${SYSTEMD_STATE_DIR}/gost-kharej-8.service.state"
assert_contains "unverified create rollback reports retained recovery material" "Retained recovery files" "${create_unverified_failure}"

prepare_iran_candidate 4109
export STUB_FAIL_ACTION=enable
export STUB_PARTIAL_FAIL_ACTION=enable
export STUB_FAIL_SECONDARY_ACTION=stop
create_stop_failure="${TEST_HOME}/create-stop-failure.out"
if install_new_profile_from_loaded iran 9 1 > "${create_stop_failure}" 2>&1; then fail "create rollback stop failure returns nonzero"; else pass "create rollback stop failure returns nonzero"; fi
unset STUB_FAIL_ACTION STUB_PARTIAL_FAIL_ACTION STUB_FAIL_SECONDARY_ACTION
assert_file "create rollback stop failure retains env" "${ENV_DIR}/iran-9.env"
assert_file "create rollback stop failure retains unit" "${UNIT_DIR}/gost-iran-9.service"
assert_contains "create rollback stop failure retains active service state" "loaded|disabled|active|109" "${SYSTEMD_STATE_DIR}/gost-iran-9.service.state"

# Stateful delete failure and exact restoration regressions.
write_iran 10 5010 delete-state
select_existing_tunnel() {
  SELECTED_TUNNEL_SIDE=iran
  SELECTED_TUNNEL_NUMBER=10
  SELECTED_TUNNEL_SERVICE=gost-iran-10.service
  SELECTED_TUNNEL_SERVICE_FILE="${UNIT_DIR}/gost-iran-10.service"
  SELECTED_TUNNEL_ENV_FILE="${ENV_DIR}/iran-10.env"
}
printf 'LISTEN 0 4096 0.0.0.0:5010 0.0.0.0:* users:(("gost",pid=110,fd=3))\n' > "${SS_FIXTURE}"
: > "${COMMAND_LOG}"
export STUB_FAIL_ACTION=disable
delete_unchanged_failure="${TEST_HOME}/delete-disable-unchanged.out"
if (confirm() { return 0; }; delete_tunnel) > "${delete_unchanged_failure}" 2>&1; then fail "delete disable failure without state change returns nonzero"; else pass "delete disable failure without state change returns nonzero"; fi
unset STUB_FAIL_ACTION
assert_contains "delete disable failure restores exact active enabled state" "loaded|enabled|active|110" "${SYSTEMD_STATE_DIR}/gost-iran-10.service.state"
assert_file "delete disable failure retains env" "${ENV_DIR}/iran-10.env"
assert_file "delete disable failure retains unit" "${UNIT_DIR}/gost-iran-10.service"

printf 'loaded|enabled|active|110\n' > "${SYSTEMD_STATE_DIR}/gost-iran-10.service.state"
export STUB_FAIL_ACTION=disable
export STUB_PARTIAL_FAIL_ACTION=disable
delete_partial_failure="${TEST_HOME}/delete-disable-partial.out"
if (confirm() { return 0; }; delete_tunnel) > "${delete_partial_failure}" 2>&1; then fail "partially applied delete disable returns nonzero"; else pass "partially applied delete disable returns nonzero"; fi
unset STUB_FAIL_ACTION STUB_PARTIAL_FAIL_ACTION
assert_contains "partial delete disable successfully restores prior state" "loaded|enabled|active|110" "${SYSTEMD_STATE_DIR}/gost-iran-10.service.state"
assert_contains "partial delete disable reports verified restoration" "exact prior service state was restored" "${delete_partial_failure}"

printf 'loaded|enabled|active|110\n' > "${SYSTEMD_STATE_DIR}/gost-iran-10.service.state"
export STUB_FAIL_ACTION=disable
export STUB_PARTIAL_FAIL_ACTION=disable
export STUB_FAIL_SECONDARY_ACTION=enable
delete_enable_restore_failure="${TEST_HOME}/delete-enable-restore-failure.out"
if (confirm() { return 0; }; delete_tunnel) > "${delete_enable_restore_failure}" 2>&1; then fail "delete restoration enable failure returns nonzero"; else pass "delete restoration enable failure returns nonzero"; fi
unset STUB_FAIL_ACTION STUB_PARTIAL_FAIL_ACTION STUB_FAIL_SECONDARY_ACTION
assert_contains "delete restoration enable failure reports unverified state" "restoration was not proven" "${delete_enable_restore_failure}"
if find "${ENV_DIR}" -name '.iran-10.env.rollback.*' | grep -q .; then pass "delete restoration enable failure retains env recovery snapshot"; else fail "delete restoration enable failure retains env recovery snapshot"; fi

printf 'loaded|enabled|active|110\n' > "${SYSTEMD_STATE_DIR}/gost-iran-10.service.state"
export STUB_FAIL_ACTION=disable
export STUB_PARTIAL_FAIL_ACTION=disable
export STUB_FAIL_SECONDARY_ACTION=start
delete_start_restore_failure="${TEST_HOME}/delete-start-restore-failure.out"
if (confirm() { return 0; }; delete_tunnel) > "${delete_start_restore_failure}" 2>&1; then fail "delete restoration start failure returns nonzero"; else pass "delete restoration start failure returns nonzero"; fi
unset STUB_FAIL_ACTION STUB_PARTIAL_FAIL_ACTION STUB_FAIL_SECONDARY_ACTION
assert_contains "delete restoration start failure retains unit recovery path" ".gost-iran-10.service.rollback." "${delete_start_restore_failure}"

printf 'loaded|enabled|active|110\n' > "${SYSTEMD_STATE_DIR}/gost-iran-10.service.state"
export STUB_FAIL_ACTION=disable
export STUB_PARTIAL_FAIL_ACTION=disable
export STUB_START_BAD_PID=1
delete_verify_mismatch="${TEST_HOME}/delete-verify-mismatch.out"
if (confirm() { return 0; }; delete_tunnel) > "${delete_verify_mismatch}" 2>&1; then fail "delete final state verification mismatch returns nonzero"; else pass "delete final state verification mismatch returns nonzero"; fi
unset STUB_FAIL_ACTION STUB_PARTIAL_FAIL_ACTION STUB_START_BAD_PID
assert_contains "delete verification mismatch never claims successful preservation" "restoration was not proven" "${delete_verify_mismatch}"
assert_not_contains "stateful safety cases send zero lifecycle commands to unselected profile" "gost-iran-1.service" "${COMMAND_LOG}"

performance_root="${TEST_HOME}/performance"
mkdir -p "${performance_root}/env" "${performance_root}/units"
old_env_dir="${GOST_ETC_DIR}"
old_unit_dir="${SYSTEMD_DIR}"
GOST_ETC_DIR="${performance_root}/env"
SYSTEMD_DIR="${performance_root}/units"
for ((number = 1; number <= 50; number++)); do
  printf 'GOST_USER=u\nGOST_PASS=p\nKHAREJ_IP=203.0.113.1\nTUNNEL_PORT=%s\nMAPPINGS=%s:80,%s:443\n' "$((30000 + number))" "$((10000 + number))" "$((11000 + number))" > "${GOST_ETC_DIR}/iran-${number}.env"
  printf 'GOST_USER=u\nGOST_PASS=p\nTUNNEL_PORT=%s\nIRAN_IP=198.51.100.10\nFIREWALL_ENABLED=0\n' "$((20000 + number))" > "${GOST_ETC_DIR}/kharej-${number}.env"
done
SECONDS=0
profiles="${TEST_HOME}/profiles-100"
ports="${TEST_HOME}/ports-100"
discover_existing_tunnels "${profiles}"
next_free_profile_number iran >/dev/null
configured_port_inventory "${ports}"
duration="${SECONDS}"
assert_eq "representative discovery finds 100 profiles" "100" "$(wc -l < "${profiles}" | tr -d ' ')"
if [[ "${duration}" -lt 3 ]]; then pass "100-profile discovery and inventory stay under 3 seconds (${duration}s)"; else fail "100-profile discovery and inventory stay under 3 seconds (${duration}s)"; fi
: > "${SS_FIXTURE}"
printf '0\n' > "${SS_COUNT_FILE}"
SECONDS=0
list_profiles > "${TEST_HOME}/profiles-100-list"
list_duration="${SECONDS}"
if [[ "${list_duration}" -lt 3 ]]; then pass "100-profile list rendering stays under 3 seconds (${list_duration}s)"; else fail "100-profile list rendering stays under 3 seconds (${list_duration}s)"; fi
assert_eq "100-profile list rendering takes one ss snapshot" "1" "$(cat "${SS_COUNT_FILE}")"
GOST_ETC_DIR="${old_env_dir}"
SYSTEMD_DIR="${old_unit_dir}"

finish_suite
