#!/usr/bin/env bash
set -Eeuo pipefail

PASS_COUNT=0
FAIL_COUNT=0

pass() {
  printf 'PASS: %s\n' "$1"
  PASS_COUNT=$((PASS_COUNT + 1))
}

fail() {
  printf 'FAIL: %s\n' "$1"
  FAIL_COUNT=$((FAIL_COUNT + 1))
}

assert_file() {
  local name="$1"
  local path="$2"
  if [[ -f "${path}" ]]; then pass "${name}"; else fail "${name} (missing ${path})"; fi
}

assert_dir() {
  local name="$1"
  local path="$2"
  if [[ -d "${path}" ]]; then pass "${name}"; else fail "${name} (missing ${path})"; fi
}

assert_absent() {
  local name="$1"
  local path="$2"
  if [[ ! -e "${path}" && ! -L "${path}" ]]; then pass "${name}"; else fail "${name} (unexpected ${path})"; fi
}

assert_contains() {
  local name="$1"
  local needle="$2"
  local file="$3"
  if grep -Fq -- "${needle}" "${file}"; then pass "${name}"; else fail "${name} (missing '${needle}')"; fi
}

assert_not_contains() {
  local name="$1"
  local needle="$2"
  local file="$3"
  if grep -Fq -- "${needle}" "${file}"; then
    fail "${name} (found '${needle}')"
  else
    pass "${name}"
  fi
}

assert_eq() {
  local name="$1"
  local expected="$2"
  local actual="$3"
  if [[ "${expected}" == "${actual}" ]]; then pass "${name}"; else fail "${name} (expected '${expected}', got '${actual}')"; fi
}

mode_of() {
  local path="$1"
  stat -c '%a' "${path}" 2>/dev/null || stat -f '%Lp' "${path}"
}

owner_of() {
  local path="$1"
  stat -c '%u:%g' "${path}" 2>/dev/null || stat -f '%u:%g' "${path}"
}

tree_digest() {
  local root="$1"
  filesystem_manifest "${root}" | cksum | awk '{print $1":"$2}'
}

filesystem_manifest() {
  local root="$1"
  local path relative mode owner kind detail
  while IFS= read -r -d '' path; do
    relative="${path#"${root}"}"
    [[ -n "${relative}" ]] || relative="/"
    mode="$(mode_of "${path}")"
    owner="$(owner_of "${path}")"
    detail=""
    if [[ -L "${path}" ]]; then
      kind="symlink"
      detail="$(readlink "${path}")"
    elif [[ -d "${path}" ]]; then
      kind="directory"
    elif [[ -f "${path}" ]]; then
      kind="file"
      detail="$(cksum "${path}" | awk '{print $1":"$2}')"
    else
      kind="other"
    fi
    printf '%s|%s|%s|%s|%s\n' "${kind}" "${mode}" "${owner}" "${relative}" "${detail}"
  done < <(find "${root}" -print0) | LC_ALL=C sort
}

make_command_stubs() {
  local bin_dir="$1"
  mkdir -p "${bin_dir}"
  cat > "${bin_dir}/systemctl" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'systemctl' >> "${COMMAND_LOG}"
printf ' %q' "$@" >> "${COMMAND_LOG}"
printf '\n' >> "${COMMAND_LOG}"
action=""
unit=""
result=0
for argument in "$@"; do
  if [[ -z "${action}" ]]; then
    [[ "${argument}" == -* ]] && continue
    action="${argument}"
    continue
  fi
  [[ "${argument}" == -* ]] && continue
  unit="${argument}"
  break
done
state_active=""
state_enabled=""
unit_path=""
case "${unit}" in
  gost-monitor-collector.service)
    state_active="${STUB_STATE_DIR}/active"
    state_enabled="${STUB_STATE_DIR}/enabled"
    unit_path="${STUB_UNIT_PATH:-}"
    if [[ -z "${unit_path}" && -n "${GOST_MANAGER_ROOT:-}" ]]; then
      unit_path="${GOST_MANAGER_ROOT%/}/etc/systemd/system/${unit}"
    fi
    ;;
  gost-upstream-watchdog.service)
    state_active="${STUB_STATE_DIR}/watchdog-active"
    state_enabled="${STUB_STATE_DIR}/watchdog-enabled"
    unit_path="${STUB_WATCHDOG_UNIT_PATH:-}"
    if [[ -z "${unit_path}" && -n "${GOST_MANAGER_ROOT:-}" ]]; then
      unit_path="${GOST_MANAGER_ROOT%/}/etc/systemd/system/${unit}"
    fi
    ;;
esac
if [[ "${STUB_REMOVE_UNIT_BEFORE_DISABLE:-0}" == "1" && "${action}" == "disable" && -n "${unit_path}" ]]; then
  rm -f "${unit_path}"
fi
if [[ "${STUB_FAIL_SYSTEMCTL_ACTION:-}" == "${action}" && ( -z "${STUB_FAIL_SYSTEMCTL_UNIT:-}" || "${STUB_FAIL_SYSTEMCTL_UNIT}" == "${unit}" ) ]]; then
  exit 1
fi
case "${action}" in
  list-units|list-unit-files) ;;
  is-enabled)
    if [[ -n "${state_enabled}" && -f "${state_enabled}" ]]; then
      result=0
    else
      result=1
    fi
    ;;
  is-active)
    if [[ -n "${state_active}" && -f "${state_active}" ]]; then
      result=0
    else
      result=1
    fi
    ;;
  enable)
    if [[ -n "${state_enabled}" ]]; then
      touch "${state_enabled}"
      mkdir -p "${STUB_STATE_DIR}/wants"
      ln -sfn "${unit_path:-${unit}}" "${STUB_STATE_DIR}/wants/${unit}"
      if [[ " ${*} " == *" --now "* ]]; then
        touch "${state_active}"
      fi
    fi
    ;;
  disable)
    if [[ -n "${state_enabled}" ]]; then
      rm -f "${state_enabled}"
      rm -f "${STUB_STATE_DIR}/wants/${unit}"
      if [[ " ${*} " == *" --now "* ]]; then
        rm -f "${state_active}"
      fi
    fi
    ;;
  start|restart)
    [[ -z "${state_active}" ]] || touch "${state_active}"
    ;;
  stop)
    [[ -z "${state_active}" ]] || rm -f "${state_active}"
    ;;
  status)
    if [[ -z "${state_active}" || ! -f "${state_active}" ]]; then
      result=1
    fi
    ;;
  daemon-reload) ;;
  show)
    force_loaded=0
    [[ "${unit}" != "gost-monitor-collector.service" || "${STUB_FORCE_MONITOR_LOADED:-0}" != "1" ]] || force_loaded=1
    [[ "${unit}" != "gost-upstream-watchdog.service" || "${STUB_FORCE_WATCHDOG_LOADED:-0}" != "1" ]] || force_loaded=1
    if [[ -n "${state_active}" && ( "${force_loaded}" == "1" || -e "${unit_path:-/nonexistent}" || -f "${state_active}" || -f "${state_enabled}" ) ]]; then
      printf 'loaded\n'
    else
      printf 'not-found\n'
    fi
    ;;
  *) ;;
esac
if [[ "${STUB_FAIL_SYSTEMCTL_AFTER_ACTION:-}" == "${action}" && ( -z "${STUB_FAIL_SYSTEMCTL_AFTER_UNIT:-}" || "${STUB_FAIL_SYSTEMCTL_AFTER_UNIT}" == "${unit}" ) ]]; then
  exit 1
fi
exit "${result}"
STUB
  cat > "${bin_dir}/ss" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'ss %s\n' "$*" >> "${COMMAND_LOG}"
STUB
  cat > "${bin_dir}/ping" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'ping %s\n' "$*" >> "${COMMAND_LOG}"
[[ "$#" -eq 7 ]]
[[ "$1" == "-n" && "$2" == "-c" && "$3" == "1" ]]
[[ "$4" == "-W" && "$5" == "1" && "$6" == "--" && "$7" == "127.0.0.1" ]]
[[ "${STUB_PING_CAPABILITY:-success}" == "success" ]]
STUB
  cat > "${bin_dir}/chown" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'chown %s\n' "$*" >> "${COMMAND_LOG}"
STUB
  cat > "${bin_dir}/systemd-analyze" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'systemd-analyze %s\n' "$*" >> "${COMMAND_LOG}"
[[ -z "${STUB_SYSTEMD_ANALYZE_OUTPUT:-}" ]] || printf '%s\n' "${STUB_SYSTEMD_ANALYZE_OUTPUT}" >&2
[[ "${STUB_FAIL_SYSTEMD_ANALYZE:-0}" != "1" ]]
STUB
  chmod 755 "${bin_dir}/systemctl" "${bin_dir}/ss" "${bin_dir}/ping" \
    "${bin_dir}/chown" "${bin_dir}/systemd-analyze"
}

finish_suite() {
  printf '\nResult: %s passed, %s failed\n' "${PASS_COUNT}" "${FAIL_COUNT}"
  [[ "${FAIL_COUNT}" -eq 0 ]]
}
