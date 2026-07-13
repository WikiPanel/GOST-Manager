#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=integration-test-lib.sh
source "${ROOT_DIR}/tests/integration-test-lib.sh"

TEST_HOME="$(mktemp -d "${TMPDIR:-/tmp}/gost-firewall-tests.XXXXXX")"
cleanup() {
  cleanup_status=$?
  rm -rf "${TEST_HOME}"
  exit "${cleanup_status}"
}
trap cleanup EXIT

STUB_BIN="${TEST_HOME}/bin"
IPTABLES_STATE="${TEST_HOME}/iptables.state"
IPTABLES_LOG="${TEST_HOME}/iptables.log"
IPTABLES_MUTATIONS="${TEST_HOME}/iptables.mutations"
mkdir -p "${STUB_BIN}"
: > "${IPTABLES_STATE}"
: > "${IPTABLES_LOG}"
printf '0\n' > "${IPTABLES_MUTATIONS}"

cat > "${STUB_BIN}/iptables" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'iptables %s\n' "$*" >> "${IPTABLES_LOG}"
operation="${1:-}"
if [[ "${operation}" == "-S" ]]; then
  cat "${IPTABLES_STATE}"
  exit 0
fi
count="$(cat "${IPTABLES_MUTATIONS}")"
count=$((count + 1))
printf '%s\n' "${count}" > "${IPTABLES_MUTATIONS}"
if [[ -n "${IPTABLES_FAIL_AT:-}" && "${count}" == "${IPTABLES_FAIL_AT}" ]]; then
  exit 1
fi
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
  -A)
    shift 2
    printf -- '-A %s %s\n' "${chain}" "$*" >> "${IPTABLES_STATE}"
    ;;
  -D)
    shift 2
    target="-A ${chain} $*"
    awk -v target="${target}" 'BEGIN { removed=0 } { if (!removed && $0 == target) { removed=1; next } print }' "${IPTABLES_STATE}" > "${tmp}"
    mv "${tmp}" "${IPTABLES_STATE}"
    ;;
  *) exit 1 ;;
esac
STUB
chmod 755 "${STUB_BIN}/iptables"

export IPTABLES_STATE IPTABLES_LOG IPTABLES_MUTATIONS
export PATH="${STUB_BIN}:${PATH}"
export GOST_MANAGER_TESTING=1
# shellcheck source=../gost-manager.sh
source "${ROOT_DIR}/gost-manager.sh"

assert_ok() {
  local name="$1"
  shift
  if "$@" >/dev/null 2>&1; then pass "${name}"; else fail "${name}"; fi
}

assert_fails() {
  local name="$1"
  shift
  if "$@" >/dev/null 2>&1; then fail "${name}"; else pass "${name}"; fi
}

printf '%s\n' '-P INPUT ACCEPT' '-A INPUT -p tcp --dport 22 -m comment --comment unrelated:ssh -j ACCEPT' > "${IPTABLES_STATE}"
assert_ok "legacy single source firewall applies" add_kharej_firewall_rules 1 198.51.100.10/32 28420
assert_contains "legacy source has exact profile allow comment" "gost-manager:kharej-1:allow" "${IPTABLES_STATE}"
assert_contains "legacy profile has exact drop comment" "gost-manager:kharej-1:drop" "${IPTABLES_STATE}"
assert_contains "unrelated firewall rule is preserved" "unrelated:ssh" "${IPTABLES_STATE}"

sources="$(canonicalize_allowed_sources '198.51.100.11,198.51.100.10,198.51.100.10/32')"
assert_eq "duplicate source canonicalization is deterministic" "198.51.100.10/32,198.51.100.11/32" "${sources}"
assert_ok "two-source firewall applies" add_kharej_firewall_rules 1 "${sources}" 28420
assert_eq "two exact allow rules emitted" "2" "$(grep -c 'gost-manager:kharej-1:allow' "${IPTABLES_STATE}")"
allow_last="$(grep -n 'gost-manager:kharej-1:allow' "${IPTABLES_STATE}" | tail -n 1 | cut -d: -f1)"
drop_line="$(grep -n 'gost-manager:kharej-1:drop' "${IPTABLES_STATE}" | cut -d: -f1)"
if [[ "${allow_last}" -lt "${drop_line}" ]]; then pass "all allows precede the profile drop"; else fail "all allows precede the profile drop"; fi

assert_ok "second profile firewall applies independently" add_kharej_firewall_rules 2 203.0.113.10/32 28421
profile_two_before="$(grep 'gost-manager:kharej-2:' "${IPTABLES_STATE}")"
assert_ok "profile one port and sources can be edited" add_kharej_firewall_rules 1 198.51.100.12/32 29420
assert_contains "edited profile uses new port" "--dport 29420" "${IPTABLES_STATE}"
assert_eq "other profile rules remain byte-equivalent" "${profile_two_before}" "$(grep 'gost-manager:kharej-2:' "${IPTABLES_STATE}")"

snapshot="${TEST_HOME}/profile-one.snapshot"
snapshot_kharej_firewall_rules 1 "${snapshot}"
state_before_failure="$(cat "${IPTABLES_STATE}")"
current_mutations="$(cat "${IPTABLES_MUTATIONS}")"
export IPTABLES_FAIL_AT="$((current_mutations + 3))"
assert_fails "partial iptables failure returns nonzero" add_kharej_firewall_rules 1 '198.51.100.20/32,198.51.100.21/32' 30420
unset IPTABLES_FAIL_AT
assert_eq "partial failure restores exact prior managed state" "${state_before_failure}" "$(cat "${IPTABLES_STATE}")"
assert_eq "rollback preserves another profile" "${profile_two_before}" "$(grep 'gost-manager:kharej-2:' "${IPTABLES_STATE}")"
assert_contains "rollback preserves unrelated rule" "unrelated:ssh" "${IPTABLES_STATE}"
assert_contains "rollback preserves INPUT policy" "-P INPUT ACCEPT" "${IPTABLES_STATE}"

assert_ok "profile firewall can be disabled" delete_kharej_firewall_rules 1
assert_not_contains "disabled profile has no allow rule" "gost-manager:kharej-1:allow" "${IPTABLES_STATE}"
assert_not_contains "disabled profile has no drop rule" "gost-manager:kharej-1:drop" "${IPTABLES_STATE}"
assert_contains "disable leaves profile two intact" "gost-manager:kharej-2:allow" "${IPTABLES_STATE}"
assert_ok "saved exact profile firewall can be restored" restore_kharej_firewall_rules 1 "${snapshot}"
assert_contains "restored profile has exact allow" "gost-manager:kharej-1:allow" "${IPTABLES_STATE}"
assert_contains "restored profile has exact drop" "gost-manager:kharej-1:drop" "${IPTABLES_STATE}"
assert_not_contains "firewall manager never flushes a chain" "iptables -F" "${IPTABLES_LOG}"
assert_not_contains "firewall manager never changes policy" "iptables -P" "${IPTABLES_LOG}"

finish_suite
