#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

usage() {
  printf '%s\n' \
    "Usage:" \
    "  $0 preflight OUTPUT_DIR" \
    "  $0 verify BASELINE_DIR OUTPUT_DIR"
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    printf 'Run this read-only snapshot tool as root.\n' >&2
    exit 2
  fi
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    printf 'Required command is missing: %s\n' "$1" >&2
    exit 2
  }
}

prepare_output() {
  local output="$1"
  if [[ -e "${output}" ]]; then
    printf 'Output path already exists: %s\n' "${output}" >&2
    exit 2
  fi
  install -d -m 0700 "${output}"
}

capture_hashes() {
  local output="$1"
  local path
  local -a env_files=() unit_files=()
  shopt -s nullglob
  env_files=(/etc/gost/iran-*.env /etc/gost/kharej-*.env)
  unit_files=(
    /etc/systemd/system/gost-iran-*.service
    /etc/systemd/system/gost-kharej-*.service
  )
  shopt -u nullglob
  : > "${output}/gost-env.sha256"
  for path in "${env_files[@]}"; do
    sha256sum "${path}" >> "${output}/gost-env.sha256"
  done
  : > "${output}/gost-units.sha256"
  for path in "${unit_files[@]}"; do
    sha256sum "${path}" >> "${output}/gost-units.sha256"
  done
}

capture_traffic_state() {
  local output="$1"
  local path service
  local -a unit_files=()
  shopt -s nullglob
  unit_files=(
    /etc/systemd/system/gost-iran-*.service
    /etc/systemd/system/gost-kharej-*.service
  )
  shopt -u nullglob
  : > "${output}/traffic-services.tsv"
  for path in "${unit_files[@]}"; do
    service="$(basename "${path}")"
    if [[ ! "${service}" =~ ^gost-(iran|kharej)-[1-9][0-9]*\.service$ ]]; then
      printf 'Unsafe traffic unit name: %s\n' "${service}" >&2
      exit 2
    fi
    systemctl show "${service}" \
      --property=Id,MainPID,NRestarts,ActiveState,SubState \
      --no-pager | tr '\n' '\t' >> "${output}/traffic-services.tsv"
    printf '\n' >> "${output}/traffic-services.tsv"
  done
  LC_ALL=C sort -o "${output}/traffic-services.tsv" \
    "${output}/traffic-services.tsv"
}

capture_firewall() {
  local output="$1"
  local captured=0
  : > "${output}/firewall.snapshot"
  if command -v nft >/dev/null 2>&1; then
    printf '%s\n' '### nft --stateless list ruleset' >> "${output}/firewall.snapshot"
    nft --stateless list ruleset >> "${output}/firewall.snapshot"
    captured=1
  fi
  if command -v iptables-save >/dev/null 2>&1; then
    printf '%s\n' '### iptables-save' >> "${output}/firewall.snapshot"
    iptables-save >> "${output}/firewall.snapshot"
    captured=1
  fi
  if command -v ip6tables-save >/dev/null 2>&1; then
    printf '%s\n' '### ip6tables-save' >> "${output}/firewall.snapshot"
    ip6tables-save >> "${output}/firewall.snapshot"
    captured=1
  fi
  if [[ "${captured}" -ne 1 ]]; then
    printf 'No supported firewall snapshot command is available.\n' >&2
    exit 2
  fi
}

capture_watchdog_policy() {
  local output="$1"
  if ! command -v gost-watchdog-admin >/dev/null 2>&1; then
    printf 'gost-watchdog-admin is unavailable.\n' >&2
    exit 2
  fi
  gost-watchdog-admin status --json | canonicalize_watchdog_policy > \
    "${output}/watchdog-policy.json"
}

canonicalize_watchdog_policy() {
  python3 -c '
import json, sys
payload = json.load(sys.stdin)
fields = (
    "profile_id", "mode", "check_mode", "maintenance", "manual_override",
    "stopped_by_watchdog", "stopped_by_maintenance",
)
rows = []
for item in payload.get("profiles", []):
    row = {key: item.get(key) for key in fields}
    if row["check_mode"] is None:
        row["check_mode"] = "ping"
    elif row["check_mode"] not in ("ping", "tcp"):
        raise ValueError("invalid Watchdog check mode in status output")
    rows.append(row)
json.dump(sorted(rows, key=lambda item: item.get("profile_id") or ""), sys.stdout,
          sort_keys=True, indent=2)
print()
'
}

capture_snapshot() {
  local output="$1"
  prepare_output "${output}"
  capture_hashes "${output}"
  capture_traffic_state "${output}"
  capture_firewall "${output}"
  capture_watchdog_policy "${output}"
  if [[ -f /etc/gost-manager/monitoring.env && ! -L /etc/gost-manager/monitoring.env ]]; then
    install -m 0600 /etc/gost-manager/monitoring.env \
      "${output}/monitoring.env.backup"
  else
    printf 'Monitoring config is missing or unsafe.\n' >&2
    exit 2
  fi
  {
    printf 'captured_at_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'manager_version=%s\n' \
      "$(tr -d '\n' < /usr/local/lib/gost-manager/VERSION)"
    printf 'kernel=%s\n' "$(uname -sr)"
    printf 'loadavg=%s\n' "$(< /proc/loadavg)"
    awk '/^(MemTotal|MemAvailable|SwapTotal|SwapFree):/ {print}' /proc/meminfo
    stat -c 'monitor_db_inode=%i monitor_db_bytes=%s' \
      /var/lib/gost-manager/metrics.sqlite3
    stat -c 'watchdog_db_inode=%i watchdog_db_bytes=%s' \
      /var/lib/gost-manager/watchdog/watchdog.sqlite3
  } > "${output}/host-state.txt"
  gost-monitor-admin status > "${output}/monitoring-status.json"
  printf 'Snapshot written to %s\n' "${output}"
}

compare_file() {
  local baseline="$1"
  local current="$2"
  local label="$3"
  if ! cmp -s "${baseline}" "${current}"; then
    printf 'FAIL: %s changed\n' "${label}" >&2
    return 1
  fi
  printf 'PASS: %s unchanged\n' "${label}"
}

verify_snapshot() {
  local baseline="$1"
  local output="$2"
  local failed=0 version
  [[ -d "${baseline}" ]] || {
    printf 'Baseline directory is unavailable: %s\n' "${baseline}" >&2
    exit 2
  }
  capture_snapshot "${output}"
  compare_file "${baseline}/gost-env.sha256" "${output}/gost-env.sha256" \
    "traffic env hashes" || failed=1
  compare_file "${baseline}/gost-units.sha256" "${output}/gost-units.sha256" \
    "traffic unit hashes" || failed=1
  compare_file "${baseline}/traffic-services.tsv" "${output}/traffic-services.tsv" \
    "GOST MainPID/NRestarts/state" || failed=1
  compare_file "${baseline}/firewall.snapshot" "${output}/firewall.snapshot" \
    "firewall snapshot" || failed=1
  compare_file "${baseline}/watchdog-policy.json" "${output}/watchdog-policy.json" \
    "Watchdog modes/manual override" || failed=1
  compare_file "${baseline}/monitoring.env.backup" \
    "${output}/monitoring.env.backup" "Monitoring config" || failed=1
  version="$(tr -d '\n' < /usr/local/lib/gost-manager/VERSION)"
  if [[ "${version}" != "2.0.3" ]]; then
    printf 'FAIL: installed version is %s, expected 2.0.3\n' "${version}" >&2
    failed=1
  else
    printf 'PASS: installed version is 2.0.3\n'
  fi
  if [[ "${failed}" -ne 0 ]]; then
    printf 'Verification failed; do not continue rollout.\n' >&2
    exit 1
  fi
  printf 'Zero-traffic verification passed.\n'
}

main() {
  require_root
  require_command install
  require_command python3
  require_command sha256sum
  require_command systemctl
  require_command gost-monitor-admin
  case "${1:-}" in
    preflight)
      [[ "$#" -eq 2 ]] || { usage >&2; exit 2; }
      capture_snapshot "$2"
      ;;
    verify)
      [[ "$#" -eq 3 ]] || { usage >&2; exit 2; }
      verify_snapshot "$2" "$3"
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
