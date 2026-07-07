#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export GOST_MANAGER_TESTING=1
# shellcheck source=../gost-manager.sh
# shellcheck disable=SC1090
source "${ROOT_DIR}/gost-manager.sh"

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

assert_eq() {
  local name="$1"
  local expected="$2"
  local actual="$3"
  if [[ "${expected}" == "${actual}" ]]; then
    pass "${name}"
  else
    fail "${name} (expected '${expected}', got '${actual}')"
  fi
}

assert_success() {
  local name="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    pass "${name}"
  else
    fail "${name}"
  fi
}

assert_failure() {
  local name="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    fail "${name}"
  else
    pass "${name}"
  fi
}

assert_eq "architecture normalization x86_64" "amd64" "$(normalize_arch x86_64)"
assert_eq "architecture normalization aarch64" "arm64" "$(normalize_arch aarch64)"
assert_success "valid port check" is_valid_port 2052
assert_failure "invalid port check zero" is_valid_port 0
assert_failure "invalid port check too high" is_valid_port 65536
assert_success "valid tunnel number" is_positive_integer 12
assert_failure "invalid tunnel number" is_positive_integer 0
assert_success "valid single mapping" validate_mappings "2052:2052" 1
assert_success "valid multi mapping" validate_mappings "80:80,8080:8080,8880:8880" 1
assert_failure "invalid mapping" validate_mappings "80,8080:8080" 1
assert_success "duplicate listen port detection" has_duplicate_listen_ports "80:80,80:8080"
assert_eq "iran service name generation" "gost-iran-2.service" "$(service_name iran 2)"
assert_eq "kharej service name generation" "gost-kharej-1.service" "$(service_name kharej 1)"
assert_eq "iran env path generation" "/etc/gost/iran-2.env" "$(env_path iran 2)"
assert_eq "kharej env path generation" "/etc/gost/kharej-1.env" "$(env_path kharej 1)"

printf '\nResult: %s passed, %s failed\n' "${PASS_COUNT}" "${FAIL_COUNT}"
if [[ "${FAIL_COUNT}" -ne 0 ]]; then
  exit 1
fi
