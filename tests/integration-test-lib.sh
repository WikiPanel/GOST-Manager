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
  stat -f '%Lp' "${path}" 2>/dev/null || stat -c '%a' "${path}"
}

tree_digest() {
  local root="$1"
  find "${root}" -type f -print -exec cksum {} \; | LC_ALL=C sort | cksum | awk '{print $1":"$2}'
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
action="${1:-}"
if [[ "${STUB_FAIL_SYSTEMCTL_ACTION:-}" == "${action}" ]]; then
  exit 1
fi
case "${action}" in
  is-enabled) [[ -f "${STUB_STATE_DIR}/enabled" ]] ;;
  is-active) [[ -f "${STUB_STATE_DIR}/active" ]] ;;
  enable)
    touch "${STUB_STATE_DIR}/enabled"
    [[ " ${*} " == *" --now "* ]] && touch "${STUB_STATE_DIR}/active"
    ;;
  disable)
    rm -f "${STUB_STATE_DIR}/enabled"
    [[ " ${*} " == *" --now "* ]] && rm -f "${STUB_STATE_DIR}/active"
    ;;
  start|restart) touch "${STUB_STATE_DIR}/active" ;;
  stop) rm -f "${STUB_STATE_DIR}/active" ;;
  status) [[ -f "${STUB_STATE_DIR}/active" ]] ;;
  daemon-reload) ;;
  *) ;;
esac
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
[[ "${STUB_FAIL_SYSTEMD_ANALYZE:-0}" != "1" ]]
STUB
  chmod 755 "${bin_dir}/systemctl" "${bin_dir}/ss" "${bin_dir}/chown" "${bin_dir}/systemd-analyze"
}

finish_suite() {
  printf '\nResult: %s passed, %s failed\n' "${PASS_COUNT}" "${FAIL_COUNT}"
  [[ "${FAIL_COUNT}" -eq 0 ]]
}
