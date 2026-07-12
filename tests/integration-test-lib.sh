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
gateway_removed() {
  [[ ! -e "${STUB_STATE_DIR}/gateway-removed-$1" ]]
}
gateway_listed() {
  local service
  for service in ${STUB_GATEWAY_LOADED_SERVICES:-} ${STUB_GATEWAY_ENABLED_SERVICES:-}; do
    if [[ "${service}" == "$1" ]] && gateway_removed "${service}"; then
      return 0
    fi
  done
  return 1
}
gateway_active() {
  local service
  for service in ${STUB_GATEWAY_ACTIVE_SERVICES:-}; do
    if [[ "${service}" == "$1" ]] && gateway_removed "${service}"; then
      return 0
    fi
  done
  return 1
}
gateway_enabled() {
  local service
  for service in ${STUB_GATEWAY_ENABLED_SERVICES:-}; do
    if [[ "${service}" == "$1" ]] && gateway_removed "${service}"; then
      return 0
    fi
  done
  return 1
}
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
if [[ "${STUB_REMOVE_UNIT_BEFORE_DISABLE:-0}" == "1" && "${action}" == "disable" && -n "${STUB_UNIT_PATH:-}" ]]; then
  rm -f "${STUB_UNIT_PATH}"
fi
if [[ "${STUB_FAIL_SYSTEMCTL_ACTION:-}" == "${action}" && ( -z "${STUB_FAIL_SYSTEMCTL_UNIT:-}" || "${STUB_FAIL_SYSTEMCTL_UNIT}" == "${unit}" ) ]]; then
  exit 1
fi
case "${action}" in
  list-units)
    for service in ${STUB_GATEWAY_LOADED_SERVICES:-}; do
      gateway_removed "${service}" && printf '%s%s loaded inactive dead test\n' "${STUB_GATEWAY_LIST_PREFIX:-}" "${service}"
    done
    ;;
  list-unit-files)
    for service in ${STUB_GATEWAY_ENABLED_SERVICES:-}; do
      gateway_removed "${service}" && printf '%s enabled\n' "${service}"
    done
    ;;
  is-enabled)
    if [[ "${unit}" == "gost-monitor-collector.service" && -f "${STUB_STATE_DIR}/enabled" ]]; then
      result=0
    elif gateway_enabled "${unit}"; then
      result=0
    else
      result=1
    fi
    ;;
  is-active)
    if [[ "${unit}" == "gost-monitor-collector.service" && -f "${STUB_STATE_DIR}/active" ]]; then
      result=0
    elif gateway_active "${unit}"; then
      result=0
    else
      result=1
    fi
    ;;
  enable)
    if [[ "${unit}" == "gost-monitor-collector.service" ]]; then
      touch "${STUB_STATE_DIR}/enabled"
      mkdir -p "${STUB_STATE_DIR}/wants"
      ln -sfn "${STUB_UNIT_PATH:-${unit}}" "${STUB_STATE_DIR}/wants/${unit}"
      if [[ " ${*} " == *" --now "* ]]; then
        touch "${STUB_STATE_DIR}/active"
      fi
    fi
    ;;
  disable)
    if [[ "${unit}" == "gost-monitor-collector.service" ]]; then
      rm -f "${STUB_STATE_DIR}/enabled"
      rm -f "${STUB_STATE_DIR}/wants/${unit}"
      if [[ " ${*} " == *" --now "* ]]; then
        rm -f "${STUB_STATE_DIR}/active"
      fi
    fi
    if [[ "${unit}" =~ ^gost-gateway-exit-[a-z][a-z0-9-]{0,62}\.service$ ]] && gateway_listed "${unit}"; then
      touch "${STUB_STATE_DIR}/gateway-removed-${unit}"
    fi
    ;;
  start|restart)
    [[ "${unit}" != "gost-monitor-collector.service" ]] || touch "${STUB_STATE_DIR}/active"
    ;;
  stop)
    [[ "${unit}" != "gost-monitor-collector.service" ]] || rm -f "${STUB_STATE_DIR}/active"
    ;;
  status)
    if [[ "${unit}" != "gost-monitor-collector.service" || ! -f "${STUB_STATE_DIR}/active" ]]; then
      result=1
    fi
    ;;
  daemon-reload) ;;
  show)
    if [[ "${unit}" == "gost-monitor-collector.service" && ( "${STUB_FORCE_MONITOR_LOADED:-0}" == "1" || -e "${STUB_UNIT_PATH:-/nonexistent}" || -f "${STUB_STATE_DIR}/active" || -f "${STUB_STATE_DIR}/enabled" ) ]]; then
      printf 'loaded\n'
    elif gateway_listed "${unit}"; then
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
  chmod 755 "${bin_dir}/systemctl" "${bin_dir}/ss" "${bin_dir}/chown" "${bin_dir}/systemd-analyze"
}

finish_suite() {
  printf '\nResult: %s passed, %s failed\n' "${PASS_COUNT}" "${FAIL_COUNT}"
  [[ "${FAIL_COUNT}" -eq 0 ]]
}
