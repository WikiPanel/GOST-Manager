#!/usr/bin/env bash
set -Eeuo pipefail

MANAGER_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GOST_BIN="/usr/local/bin/gost"
LIB_DIR="/usr/local/lib/gost-manager"
RUNNER_IRAN="${LIB_DIR}/gost-run-iran.sh"
RUNNER_KHAREJ="${LIB_DIR}/gost-run-kharej.sh"
GOST_ETC_DIR="/etc/gost"
SYSTEMD_DIR="/etc/systemd/system"
GITHUB_RELEASES_API="https://api.github.com/repos/go-gost/gost/releases?per_page=100"
MONITOR_SERVICE="gost-monitor-collector.service"
MONITOR_CONFIG="/etc/gost-manager/monitoring.env"
MONITOR_EXPORT_DIR="/root"
MONITOR_BIN="/usr/local/sbin/gost-monitor"
MONITOR_COLLECTOR_BIN="/usr/local/sbin/gost-monitor-collector"
MONITOR_ADMIN_BIN="/usr/local/sbin/gost-monitor-admin"
WATCHDOG_SERVICE="gost-upstream-watchdog.service"
WATCHDOG_ADMIN_BIN="/usr/local/sbin/gost-watchdog-admin"
STABILITY_SYSCTL_FILE="/etc/sysctl.d/99-gost-stability.conf"
STABILITY_MANAGED_MARKER="# Managed by GOST Manager: Server Stability"

if [[ "${GOST_MANAGER_TESTING:-0}" == "1" ]]; then
  GOST_BIN="${GOST_BIN_TEST:-${GOST_BIN}}"
  LIB_DIR="${GOST_LIB_DIR_TEST:-${LIB_DIR}}"
  RUNNER_IRAN="${GOST_RUNNER_IRAN_TEST:-${LIB_DIR}/gost-run-iran.sh}"
  RUNNER_KHAREJ="${GOST_RUNNER_KHAREJ_TEST:-${LIB_DIR}/gost-run-kharej.sh}"
  GOST_ETC_DIR="${GOST_ETC_DIR_TEST:-${GOST_ETC_DIR}}"
  SYSTEMD_DIR="${GOST_SYSTEMD_DIR_TEST:-${SYSTEMD_DIR}}"
  MONITOR_CONFIG="${GOST_MONITOR_CONFIG_TEST:-${MONITOR_CONFIG}}"
  MONITOR_EXPORT_DIR="${GOST_MONITOR_EXPORT_DIR_TEST:-${MONITOR_EXPORT_DIR}}"
  MONITOR_BIN="${GOST_MONITOR_BIN_TEST:-${MONITOR_BIN}}"
  MONITOR_COLLECTOR_BIN="${GOST_MONITOR_COLLECTOR_BIN_TEST:-${MONITOR_COLLECTOR_BIN}}"
  MONITOR_ADMIN_BIN="${GOST_MONITOR_ADMIN_BIN_TEST:-${MONITOR_ADMIN_BIN}}"
  WATCHDOG_ADMIN_BIN="${GOST_WATCHDOG_ADMIN_BIN_TEST:-${WATCHDOG_ADMIN_BIN}}"
  STABILITY_SYSCTL_FILE="${GOST_STABILITY_SYSCTL_FILE_TEST:-${STABILITY_SYSCTL_FILE}}"
fi

MAX_PROFILE_NUMBER=10000
PROFILE_ENV_KEYS=()
PROFILE_ENV_VALUES=()
PROFILE_ENV_PRESERVED_LINES=()
STABILITY_SYSCTL_KEYS=(
  fs.file-max
  net.core.somaxconn
  net.core.netdev_max_backlog
  net.ipv4.ip_local_port_range
  net.ipv4.tcp_max_syn_backlog
  net.ipv4.tcp_fin_timeout
  net.ipv4.tcp_keepalive_time
  net.ipv4.tcp_keepalive_intvl
  net.ipv4.tcp_keepalive_probes
  net.ipv4.tcp_slow_start_after_idle
)
STABILITY_SYSCTL_VALUES=(
  2097152
  65535
  250000
  "10000 65000"
  65535
  15
  60
  10
  6
  0
)
STABILITY_CURRENT_VALUES=()
STABILITY_FINAL_VALUES=()
STABILITY_KERNEL_RESULTS=()
STABILITY_SERVICES=()
STABILITY_SERVICE_RESULTS=()
STABILITY_RESTART_REQUIRED=()
STABILITY_SYSCTL_FILE_RESULT=""
STABILITY_SYSCTL_CHANGED=0
STABILITY_SYSCTL_APPLY_COUNT=0
STABILITY_DAEMON_RELOAD_COUNT=0
STABILITY_OPTIMIZED_COUNT=0
STABILITY_FAILURE_COUNT=0
WATCHDOG_SELECTED_PROFILE=""

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

info() {
  printf '%s\n' "$*"
}

manager_version() {
  local candidate value
  local -a candidates=()
  if [[ "${GOST_MANAGER_TESTING:-0}" == "1" && -n "${GOST_MANAGER_VERSION_FILE_TEST:-}" ]]; then
    candidates+=("${GOST_MANAGER_VERSION_FILE_TEST}")
  else
    candidates+=("${MANAGER_SCRIPT_DIR}/VERSION" "${LIB_DIR}/VERSION")
  fi
  for candidate in "${candidates[@]}"; do
    [[ -f "${candidate}" && ! -L "${candidate}" ]] || continue
    value="$(< "${candidate}")"
    if [[ "${value}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
      printf '%s\n' "${value}"
      return 0
    fi
  done
  return 1
}

manager_banner() {
  local version
  if version="$(manager_version)"; then
    printf 'GOST Manager v%s\n' "${version}"
  else
    printf 'GOST Manager version unknown\n'
  fi
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "this action must be run as root. Try: sudo bash gost-manager.sh"
  fi
}

confirm() {
  local prompt="${1:-Continue?}"
  local answer
  read -r -p "${prompt} [y/N]: " answer
  case "${answer}" in
    y|Y|yes|YES|Yes) return 0 ;;
    *) return 1 ;;
  esac
}

prompt_default() {
  local prompt="$1"
  local default_value="$2"
  local value
  read -r -p "${prompt} [${default_value}]: " value
  if [[ -z "${value}" ]]; then
    printf '%s\n' "${default_value}"
  else
    printf '%s\n' "${value}"
  fi
}

prompt_required() {
  local prompt="$1"
  local value
  while true; do
    read -r -p "${prompt}: " value
    if [[ -n "${value}" ]]; then
      printf '%s\n' "${value}"
      return 0
    fi
    info "Value is required."
  done
}

is_positive_integer() {
  local value="$1"
  [[ "${value}" =~ ^[1-9][0-9]*$ ]]
}

is_valid_port() {
  local value="$1"
  is_positive_integer "${value}" && [[ "${value}" -ge 1 && "${value}" -le 65535 ]]
}

normalize_arch() {
  local arch="$1"
  case "${arch}" in
    x86_64|amd64) printf 'amd64\n' ;;
    aarch64|arm64) printf 'arm64\n' ;;
    *) return 1 ;;
  esac
}

validate_side() {
  local side="$1"
  [[ "${side}" == "iran" || "${side}" == "kharej" ]]
}

service_name() {
  local side="$1"
  local number="$2"
  printf 'gost-%s-%s.service\n' "${side}" "${number}"
}

service_path() {
  local side="$1"
  local number="$2"
  printf '%s/%s\n' "${SYSTEMD_DIR}" "$(service_name "${side}" "${number}")"
}

env_path() {
  local side="$1"
  local number="$2"
  printf '%s/%s-%s.env\n' "${GOST_ETC_DIR}" "${side}" "${number}"
}

validate_tunnel_number_or_die() {
  local number="$1"
  is_positive_integer "${number}" || die "tunnel number must be a positive integer."
}

validate_port_or_die() {
  local port="$1"
  is_valid_port "${port}" || die "port must be between 1 and 65535: ${port}"
}

validate_token_or_die() {
  local label="$1"
  local value="$2"
  [[ "${value}" =~ ^[A-Za-z0-9._~-]+$ ]] || die "${label} may contain only letters, numbers, dot, underscore, tilde, or hyphen."
}

validate_host_or_die() {
  local label="$1"
  local value="$2"
  [[ "${value}" =~ ^[A-Za-z0-9.-]+$ ]] || die "${label} must be an IPv4 address or DNS name without spaces."
}

validate_iptables_source_or_die() {
  local value="$1"
  [[ "${value}" =~ ^[0-9./]+$ ]] || die "Iran IP must be an IPv4 address or CIDR value."
}

has_duplicate_listen_ports() {
  local mappings="$1"
  local IFS=','
  local pairs=()
  local pair listen target
  local seen=","
  read -r -a pairs <<< "${mappings}"
  for pair in "${pairs[@]}"; do
    listen="${pair%%:*}"
    target="${pair#*:}"
    if [[ -z "${listen}" || -z "${target}" ]]; then
      return 1
    fi
    if [[ "${seen}" == *",${listen},"* ]]; then
      return 0
    fi
    seen="${seen}${listen},"
  done
  return 1
}

validate_mappings() {
  local mappings="$1"
  local quiet="${2:-0}"
  local IFS=','
  local pairs=()
  local pair listen target
  local seen=","

  if [[ -z "${mappings}" || "${mappings}" == *, || "${mappings}" == ,* || "${mappings}" == *,,* ]]; then
    [[ "${quiet}" == "1" ]] || printf 'Invalid mapping format. Use listen_port:target_port, for example 80:80,8080:8080.\n' >&2
    return 1
  fi

  read -r -a pairs <<< "${mappings}"
  for pair in "${pairs[@]}"; do
    if [[ ! "${pair}" =~ ^[0-9]+:[0-9]+$ ]]; then
      [[ "${quiet}" == "1" ]] || printf 'Invalid mapping: %s\n' "${pair}" >&2
      return 1
    fi
    listen="${pair%%:*}"
    target="${pair#*:}"
    if ! is_valid_port "${listen}"; then
      [[ "${quiet}" == "1" ]] || printf 'Invalid listen port in mapping: %s\n' "${listen}" >&2
      return 1
    fi
    if ! is_valid_port "${target}"; then
      [[ "${quiet}" == "1" ]] || printf 'Invalid target port in mapping: %s\n' "${target}" >&2
      return 1
    fi
    if [[ "${seen}" == *",${listen},"* ]]; then
      [[ "${quiet}" == "1" ]] || printf 'Duplicate listen port in mappings: %s\n' "${listen}" >&2
      return 1
    fi
    seen="${seen}${listen},"
  done
}

parse_mapping() {
  validate_mappings "$@"
}

parse_tunnel_service_name() {
  local service="$1"
  if [[ "${service}" =~ ^gost-(iran|kharej)-([1-9][0-9]*)\.service$ ]]; then
    printf '%s %s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
    return 0
  fi
  return 1
}

parse_tunnel_env_name() {
  local env_file="$1"
  local base
  base="${env_file##*/}"
  if [[ "${base}" =~ ^(iran|kharej)-([1-9][0-9]*)\.env$ ]]; then
    printf '%s %s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
    return 0
  fi
  return 1
}

env_get() {
  local key="$1"
  local file="$2"
  awk -F= -v key="${key}" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "${file}" 2>/dev/null || true
}

profile_id_parts() {
  local profile_id="$1"
  if [[ "${profile_id}" =~ ^(iran|kharej)-([1-9][0-9]*)$ ]]; then
    printf '%s %s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
    return 0
  fi
  return 1
}

validate_profile_label() {
  local value="$1"
  [[ -z "${value}" || "${value}" =~ ^[A-Za-z0-9][A-Za-z0-9._~-]{0,63}$ ]]
}

validate_profile_label_or_die() {
  validate_profile_label "$1" || die "profile label must use 1-64 safe letters, numbers, dot, underscore, tilde, or hyphen."
}

profile_key_is_known() {
  local side="$1"
  local key="$2"
  case "${side}:${key}" in
    iran:GOST_USER|iran:GOST_PASS|iran:KHAREJ_IP|iran:TUNNEL_PORT|iran:MAPPINGS|iran:PROFILE_LABEL) return 0 ;;
    kharej:GOST_USER|kharej:GOST_PASS|kharej:TUNNEL_PORT|kharej:IRAN_IP|kharej:ALLOWED_IRAN_SOURCES|kharej:FIREWALL_ENABLED|kharej:PROFILE_LABEL) return 0 ;;
    *) return 1 ;;
  esac
}

profile_env_reset() {
  PROFILE_ENV_KEYS=()
  PROFILE_ENV_VALUES=()
  PROFILE_ENV_PRESERVED_LINES=()
}

profile_env_value() {
  local wanted="$1"
  local index
  for ((index = 0; index < ${#PROFILE_ENV_KEYS[@]}; index++)); do
    if [[ "${PROFILE_ENV_KEYS[${index}]}" == "${wanted}" ]]; then
      printf '%s\n' "${PROFILE_ENV_VALUES[${index}]}"
      return 0
    fi
  done
  return 1
}

profile_env_has_key() {
  profile_env_value "$1" >/dev/null 2>&1
}

profile_env_set() {
  local key="$1"
  local value="$2"
  local index
  for ((index = 0; index < ${#PROFILE_ENV_KEYS[@]}; index++)); do
    if [[ "${PROFILE_ENV_KEYS[${index}]}" == "${key}" ]]; then
      PROFILE_ENV_VALUES[index]="${value}"
      return 0
    fi
  done
  PROFILE_ENV_KEYS+=("${key}")
  PROFILE_ENV_VALUES+=("${value}")
}

profile_env_unset() {
  local wanted="$1"
  local keys=()
  local values=()
  local index
  for ((index = 0; index < ${#PROFILE_ENV_KEYS[@]}; index++)); do
    if [[ "${PROFILE_ENV_KEYS[${index}]}" != "${wanted}" ]]; then
      keys+=("${PROFILE_ENV_KEYS[${index}]}")
      values+=("${PROFILE_ENV_VALUES[${index}]}")
    fi
  done
  PROFILE_ENV_KEYS=("${keys[@]}")
  PROFILE_ENV_VALUES=("${values[@]}")
}

profile_env_load() {
  local file="$1"
  local side="$2"
  local skip_binary_check="${3:-0}"
  local line key value index

  validate_side "${side}" || return 1
  [[ -f "${file}" && ! -L "${file}" ]] || return 1
  if [[ "${skip_binary_check}" != "1" ]]; then
    python3 -c 'import pathlib,sys; data=pathlib.Path(sys.argv[1]).read_bytes(); sys.exit(1 if len(data) > 131072 or b"\0" in data or b"\r" in data else 0)' "${file}" || return 1
  fi

  profile_env_reset
  while IFS= read -r line || [[ -n "${line}" ]]; do
    if [[ -z "${line}" || "${line}" == \#* ]]; then
      continue
    fi
    if [[ ! "${line}" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      profile_env_reset
      return 1
    fi
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    if [[ "${value}" == *$'\n'* || "${value}" == *$'\r'* ]]; then
      profile_env_reset
      return 1
    fi
    if profile_key_is_known "${side}" "${key}"; then
      for ((index = 0; index < ${#PROFILE_ENV_KEYS[@]}; index++)); do
        if [[ "${PROFILE_ENV_KEYS[${index}]}" == "${key}" ]]; then
          profile_env_reset
          return 1
        fi
      done
      PROFILE_ENV_KEYS+=("${key}")
      PROFILE_ENV_VALUES+=("${value}")
    else
      PROFILE_ENV_PRESERVED_LINES+=("${line}")
    fi
  done < "${file}"
}

canonicalize_allowed_sources() {
  local value="$1"
  python3 -c '
import ipaddress
import sys

raw = sys.argv[1]
if not raw or any(char.isspace() for char in raw):
    raise SystemExit(1)
parts = raw.split(",")
if len(parts) > 64 or any(not part for part in parts):
    raise SystemExit(1)
networks = set()
for part in parts:
    try:
        network = ipaddress.ip_network(part, strict=False)
    except ValueError:
        raise SystemExit(1)
    if network.version != 4 or network.prefixlen < 8:
        raise SystemExit(1)
    networks.add(network)
ordered = sorted(networks, key=lambda item: (int(item.network_address), item.prefixlen))
print(",".join(str(item) for item in ordered))
' "${value}"
}

is_valid_ipv4_address() {
  local value="$1"
  local octet
  local octets=()
  [[ "${value}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  IFS='.' read -r -a octets <<< "${value}"
  [[ "${#octets[@]}" -eq 4 ]] || return 1
  for octet in "${octets[@]}"; do
    [[ "${octet}" == "0" || "${octet}" != 0* ]] || return 1
    ((10#${octet} <= 255)) || return 1
  done
}

validate_allowed_sources_syntax() {
  local value="$1"
  local item address prefix
  local items=()
  [[ -n "${value}" && "${value}" != *[[:space:]]* ]] || return 1
  IFS=',' read -r -a items <<< "${value}"
  [[ "${#items[@]}" -ge 1 && "${#items[@]}" -le 64 ]] || return 1
  for item in "${items[@]}"; do
    [[ -n "${item}" ]] || return 1
    address="${item%%/*}"
    if [[ "${item}" == */* ]]; then
      prefix="${item#*/}"
      [[ "${prefix}" =~ ^[0-9]+$ && "${prefix}" -ge 8 && "${prefix}" -le 32 ]] || return 1
      [[ "${item}" != */*/* ]] || return 1
    fi
    is_valid_ipv4_address "${address}" || return 1
  done
}

profile_sources_from_loaded() {
  local legacy sources
  legacy="$(profile_env_value IRAN_IP || true)"
  sources="$(profile_env_value ALLOWED_IRAN_SOURCES || true)"
  if [[ -n "${legacy}" && -n "${sources}" ]]; then
    return 1
  fi
  if [[ -n "${sources}" ]]; then
    canonicalize_allowed_sources "${sources}"
    return $?
  fi
  [[ -n "${legacy}" ]] || return 1
  canonicalize_allowed_sources "${legacy}"
}

validate_loaded_profile() {
  local side="$1"
  local label user password port mappings host firewall sources
  user="$(profile_env_value GOST_USER || true)"
  password="$(profile_env_value GOST_PASS || true)"
  port="$(profile_env_value TUNNEL_PORT || true)"
  label="$(profile_env_value PROFILE_LABEL || true)"

  validate_token_or_die "GOST username" "${user}"
  validate_token_or_die "GOST password" "${password}"
  validate_profile_label_or_die "${label}"
  validate_port_or_die "${port}"

  if [[ "${side}" == "iran" ]]; then
    host="$(profile_env_value KHAREJ_IP || true)"
    mappings="$(profile_env_value MAPPINGS || true)"
    validate_host_or_die "Kharej IP" "${host}"
    validate_mappings "${mappings}" || return 1
    return 0
  fi

  sources="$(profile_sources_from_loaded)" || die "Kharej profile must contain exactly one valid Iran source field."
  [[ -n "${sources}" ]] || die "at least one Iran source is required."
  firewall="$(profile_env_value FIREWALL_ENABLED || true)"
  [[ "${firewall}" == "0" || "${firewall}" == "1" ]] || die "FIREWALL_ENABLED must be 0 or 1."
}

loaded_profile_is_valid() {
  local side="$1"
  local user password port label host mappings firewall sources
  user="$(profile_env_value GOST_USER || true)"
  password="$(profile_env_value GOST_PASS || true)"
  port="$(profile_env_value TUNNEL_PORT || true)"
  label="$(profile_env_value PROFILE_LABEL || true)"
  [[ "${user}" =~ ^[A-Za-z0-9._~-]+$ && "${password}" =~ ^[A-Za-z0-9._~-]+$ ]] || return 1
  is_valid_port "${port}" || return 1
  validate_profile_label "${label}" || return 1
  if [[ "${side}" == "iran" ]]; then
    host="$(profile_env_value KHAREJ_IP || true)"
    mappings="$(profile_env_value MAPPINGS || true)"
    [[ "${host}" =~ ^[A-Za-z0-9.-]+$ ]] || return 1
    validate_mappings "${mappings}" 1
    return $?
  fi
  local legacy_sources allowed_sources
  legacy_sources="$(profile_env_value IRAN_IP || true)"
  allowed_sources="$(profile_env_value ALLOWED_IRAN_SOURCES || true)"
  [[ -z "${legacy_sources}" || -z "${allowed_sources}" ]] || return 1
  sources="${allowed_sources:-${legacy_sources}}"
  validate_allowed_sources_syntax "${sources}" || return 1
  firewall="$(profile_env_value FIREWALL_ENABLED || true)"
  [[ "${firewall}" == "0" || "${firewall}" == "1" ]]
}

profile_identity_exists() {
  local side="$1"
  local number="$2"
  local env_file="${GOST_ETC_DIR}/${side}-${number}.env"
  local unit_file="${SYSTEMD_DIR}/gost-${side}-${number}.service"
  [[ -e "${env_file}" || -L "${env_file}" || -e "${unit_file}" || -L "${unit_file}" ]]
}

find_invalid_profile_env_files() {
  local profiles="$1"
  local output="$2"
  python3 -c '
import pathlib
import sys

invalid = []
for raw in pathlib.Path(sys.argv[1]).read_text().splitlines():
    parts = raw.split("|", 4)
    if len(parts) != 5:
        continue
    path = pathlib.Path(parts[4])
    if not path.is_file() or path.is_symlink():
        continue
    try:
        data = path.read_bytes()
    except OSError:
        invalid.append(str(path))
        continue
    if len(data) > 131072 or b"\0" in data or b"\r" in data:
        invalid.append(str(path))
pathlib.Path(sys.argv[2]).write_text("\n".join(invalid) + ("\n" if invalid else ""))
' "${profiles}" "${output}"
}

next_free_profile_number() {
  local side="$1"
  local number
  validate_side "${side}" || return 1
  for ((number = 1; number <= MAX_PROFILE_NUMBER; number++)); do
    if ! profile_identity_exists "${side}" "${number}"; then
      printf '%s\n' "${number}"
      return 0
    fi
  done
  printf 'Error: no free %s profile number below the safe limit %s.\n' "${side}" "${MAX_PROFILE_NUMBER}" >&2
  return 1
}

profile_local_ports_from_loaded() {
  local side="$1"
  local mappings pair ports port
  local pairs=()
  if [[ "${side}" == "kharej" ]]; then
    port="$(profile_env_value TUNNEL_PORT || true)"
    is_valid_port "${port}" || return 1
    printf '%s\n' "${port}"
    return 0
  fi
  mappings="$(profile_env_value MAPPINGS || true)"
  validate_mappings "${mappings}" 1 || return 1
  IFS=',' read -r -a pairs <<< "${mappings}"
  ports=""
  for pair in "${pairs[@]}"; do
    port="${pair%%:*}"
    ports="${ports}${ports:+,}${port}"
  done
  printf '%s\n' "${ports}"
}

package_for_command() {
  case "$1" in
    curl) printf 'curl ca-certificates\n' ;;
    tar) printf 'tar\n' ;;
    gzip) printf 'gzip\n' ;;
    sha256sum) printf 'coreutils\n' ;;
    python3) printf 'python3\n' ;;
    ss) printf 'iproute2\n' ;;
    iptables) printf 'iptables\n' ;;
    systemctl) printf 'systemd\n' ;;
    sysctl) printf 'procps\n' ;;
    cmp) printf 'diffutils\n' ;;
    *) printf '%s\n' "$1" ;;
  esac
}

ensure_commands() {
  local missing=()
  local packages=()
  local command_name

  for command_name in "$@"; do
    if ! command -v "${command_name}" >/dev/null 2>&1; then
      missing+=("${command_name}")
      case "${command_name}" in
        curl) packages+=("curl" "ca-certificates") ;;
        tar) packages+=("tar") ;;
        gzip) packages+=("gzip") ;;
        sha256sum) packages+=("coreutils") ;;
        python3) packages+=("python3") ;;
        ss) packages+=("iproute2") ;;
        iptables) packages+=("iptables") ;;
        systemctl) packages+=("systemd") ;;
        *) packages+=("$(package_for_command "${command_name}")") ;;
      esac
    fi
  done

  if [[ "${#missing[@]}" -eq 0 ]]; then
    return 0
  fi

  info "Missing required commands: ${missing[*]}"
  if command -v apt-get >/dev/null 2>&1; then
    info "Packages to install: ${packages[*]}"
    if confirm "Install missing packages with apt-get now?"; then
      apt-get update
      apt-get install -y "${packages[@]}"
      return 0
    fi
  fi

  die "missing required commands: ${missing[*]}"
}

backup_existing_file() {
  local path="$1"
  local timestamp backup
  if [[ -e "${path}" ]]; then
    timestamp="$(date +%Y%m%d%H%M%S)"
    backup="${path}.bak.${timestamp}"
    cp -a "${path}" "${backup}"
    info "Backup created: ${backup}"
  fi
}

validate_managed_destination() {
  local path="$1"
  local kind="$2"
  local directory base
  directory="$(dirname "${path}")"
  base="$(basename "${path}")"
  case "${kind}" in
    env)
      [[ "${directory}" == "${GOST_ETC_DIR}" && "${base}" =~ ^(iran|kharej)-[1-9][0-9]*\.env$ ]] || return 1
      ;;
    unit)
      [[ "${directory}" == "${SYSTEMD_DIR}" && "${base}" =~ ^gost-(iran|kharej)-[1-9][0-9]*\.service$ ]] || return 1
      ;;
    *) return 1 ;;
  esac
  [[ ! -L "${directory}" && -d "${directory}" ]] || return 1
  [[ ! -L "${path}" ]] || return 1
  if [[ -e "${path}" && ! -f "${path}" ]]; then
    return 1
  fi
}

fsync_file_or_directory() {
  local path="$1"
  python3 -c 'import os,sys; fd=os.open(sys.argv[1], os.O_RDONLY); os.fsync(fd); os.close(fd)' "${path}" >/dev/null 2>&1 || true
}

set_production_owner() {
  local path="$1"
  if [[ "${GOST_MANAGER_TESTING:-0}" != "1" ]]; then
    chown root:root "${path}"
  fi
}

profile_env_write_lines() {
  local side="$1"
  local output="$2"
  local key value line
  local order=()
  if [[ "${side}" == "iran" ]]; then
    order=(GOST_USER GOST_PASS KHAREJ_IP TUNNEL_PORT MAPPINGS PROFILE_LABEL)
  else
    order=(GOST_USER GOST_PASS TUNNEL_PORT IRAN_IP ALLOWED_IRAN_SOURCES FIREWALL_ENABLED PROFILE_LABEL)
  fi
  : > "${output}"
  for key in "${order[@]}"; do
    if profile_env_has_key "${key}"; then
      value="$(profile_env_value "${key}")"
      printf '%s=%s\n' "${key}" "${value}" >> "${output}"
    fi
  done
  for line in "${PROFILE_ENV_PRESERVED_LINES[@]-}"; do
    [[ -n "${line}" ]] || continue
    printf '%s\n' "${line}" >> "${output}"
  done
}

write_loaded_profile_env() {
  local path="$1"
  local side="$2"
  local directory base tmp
  directory="$(dirname "${path}")"
  base="$(basename "${path}")"
  validate_managed_destination "${path}" env || return 1
  tmp="$(mktemp "${directory}/.${base}.tmp.XXXXXX")" || return 1
  chmod 600 "${tmp}" || { rm -f "${tmp}"; return 1; }
  set_production_owner "${tmp}" || { rm -f "${tmp}"; return 1; }
  profile_env_write_lines "${side}" "${tmp}" || { rm -f "${tmp}"; return 1; }
  chmod 600 "${tmp}" || { rm -f "${tmp}"; return 1; }
  fsync_file_or_directory "${tmp}"
  mv -f "${tmp}" "${path}" || { rm -f "${tmp}"; return 1; }
  fsync_file_or_directory "${directory}"
}

write_secure_env_file() {
  local path="$1"
  local identity side
  shift
  identity="$(parse_tunnel_env_name "${path}")" || return 1
  side="${identity%% *}"
  profile_env_reset
  while [[ "$#" -gt 0 ]]; do
    [[ "$#" -ge 2 ]] || return 1
    profile_env_set "$1" "$2"
    shift 2
  done
  write_loaded_profile_env "${path}" "${side}"
}

write_service_file() {
  local path="$1"
  local description="$2"
  local env_file="$3"
  local runner="$4"
  local tmp
  local directory base
  directory="$(dirname "${path}")"
  base="$(basename "${path}")"
  validate_managed_destination "${path}" unit || return 1
  tmp="$(mktemp "${directory}/.${base}.tmp.XXXXXX")" || return 1
  chmod 644 "${tmp}" || { rm -f "${tmp}"; return 1; }
  set_production_owner "${tmp}" || { rm -f "${tmp}"; return 1; }
  cat > "${tmp}" <<SERVICE_EOF
[Unit]
Description=${description}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=${env_file}
ExecStart=${runner}
Restart=always
RestartSec=3
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
SERVICE_EOF
  chmod 644 "${tmp}" || { rm -f "${tmp}"; return 1; }
  fsync_file_or_directory "${tmp}"
  mv -f "${tmp}" "${path}" || { rm -f "${tmp}"; return 1; }
  fsync_file_or_directory "${directory}"
}

snapshot_managed_file() {
  local path="$1"
  local directory base snapshot
  [[ -f "${path}" && ! -L "${path}" ]] || return 1
  directory="$(dirname "${path}")"
  base="$(basename "${path}")"
  snapshot="$(mktemp "${directory}/.${base}.rollback.XXXXXX")" || return 1
  chmod 600 "${snapshot}" || { rm -f "${snapshot}"; return 1; }
  cp -p "${path}" "${snapshot}" || { rm -f "${snapshot}"; return 1; }
  printf '%s\n' "${snapshot}"
}

restore_managed_snapshot() {
  local snapshot="$1"
  local destination="$2"
  local kind="$3"
  local directory base tmp
  [[ -f "${snapshot}" && ! -L "${snapshot}" ]] || return 1
  validate_managed_destination "${destination}" "${kind}" || return 1
  directory="$(dirname "${destination}")"
  base="$(basename "${destination}")"
  tmp="$(mktemp "${directory}/.${base}.restore.XXXXXX")" || return 1
  cp -p "${snapshot}" "${tmp}" || { rm -f "${tmp}"; return 1; }
  set_production_owner "${tmp}" || { rm -f "${tmp}"; return 1; }
  fsync_file_or_directory "${tmp}"
  mv -f "${tmp}" "${destination}" || { rm -f "${tmp}"; return 1; }
  fsync_file_or_directory "${directory}"
  cmp -s "${snapshot}" "${destination}"
}

install_or_update_gost() {
  require_root
  ensure_commands curl tar gzip sha256sum python3

  local arch normalized_arch current_version answer tmpdir api_json release_info
  local tag asset_name asset_url checksum_name checksum_url archive checksum_file extract_dir extracted_gost
  arch="$(uname -m)"
  normalized_arch="$(normalize_arch "${arch}")" || die "unsupported architecture: ${arch}. Supported: x86_64, aarch64."

  if [[ -x "${GOST_BIN}" ]]; then
    current_version="$("${GOST_BIN}" -V 2>&1 || true)"
    info "Current GOST:"
    info "${current_version}"
    read -r -p "Update GOST from official go-gost/gost GitHub Releases? [y/N]: " answer
    case "${answer}" in
      y|Y|yes|YES|Yes) ;;
      *) info "GOST update skipped."; return 0 ;;
    esac
  else
    info "GOST is not installed at ${GOST_BIN}. Installing."
  fi

  tmpdir="$(mktemp -d)"
  api_json="${tmpdir}/releases.json"
  archive="${tmpdir}/gost.tar.gz"
  checksum_file="${tmpdir}/checksums.txt"
  extract_dir="${tmpdir}/extract"
  mkdir -p "${extract_dir}"

  info "Fetching official release metadata from go-gost/gost..."
  curl -fsSL "${GITHUB_RELEASES_API}" -o "${api_json}" || die "failed to fetch GitHub release metadata."

  release_info="$(python3 - "${normalized_arch}" "${api_json}" <<'PY'
import json
import re
import sys

arch = sys.argv[1]
path = sys.argv[2]
with open(path, "r", encoding="utf-8") as handle:
    releases = json.load(handle)

pattern = re.compile(r"^gost_.*_linux_%s\.tar\.gz$" % re.escape(arch))
for release in releases:
    if release.get("draft") or release.get("prerelease"):
        continue
    assets = release.get("assets", [])
    target = None
    for asset in assets:
        name = asset.get("name", "")
        if pattern.match(name) and "amd64v3" not in name:
            target = (name, asset.get("browser_download_url", ""))
            break
    if not target:
        continue
    checksum_assets = []
    for asset in assets:
        name = asset.get("name", "")
        if re.search(r"(checksums?|sha256|sha256sums?)", name, re.IGNORECASE):
            checksum_assets.append((name, asset.get("browser_download_url", "")))
    checksum = ("", "")
    for candidate in checksum_assets:
        if target[0] in candidate[0]:
            checksum = candidate
            break
    if checksum == ("", ""):
        for candidate in checksum_assets:
            if arch in candidate[0]:
                checksum = candidate
                break
    if checksum == ("", "") and checksum_assets:
        checksum = checksum_assets[0]
    print(release.get("tag_name", ""))
    print(target[0])
    print(target[1])
    print(checksum[0])
    print(checksum[1])
    sys.exit(0)

sys.exit("No stable Linux %s release asset found." % arch)
PY
)" || die "failed to select a stable Linux ${normalized_arch} release asset."

  tag="$(printf '%s\n' "${release_info}" | sed -n '1p')"
  asset_name="$(printf '%s\n' "${release_info}" | sed -n '2p')"
  asset_url="$(printf '%s\n' "${release_info}" | sed -n '3p')"
  checksum_name="$(printf '%s\n' "${release_info}" | sed -n '4p')"
  checksum_url="$(printf '%s\n' "${release_info}" | sed -n '5p')"

  [[ -n "${asset_url}" ]] || die "release asset URL was empty."
  case "${asset_url}" in
    https://github.com/go-gost/gost/releases/download/*) ;;
    *) die "release asset URL is not an official go-gost/gost GitHub Release URL." ;;
  esac
  if [[ -n "${checksum_url}" ]]; then
    case "${checksum_url}" in
      https://github.com/go-gost/gost/releases/download/*) ;;
      *) die "checksum URL is not an official go-gost/gost GitHub Release URL." ;;
    esac
  fi
  info "Selected release: ${tag}"
  info "Selected asset: ${asset_name}"
  curl -fL --retry 3 "${asset_url}" -o "${archive}" || die "failed to download ${asset_name}."

  if [[ -n "${checksum_url}" ]]; then
    info "Downloading checksum file: ${checksum_name}"
    curl -fL --retry 3 "${checksum_url}" -o "${checksum_file}" || die "failed to download checksum file."
    verify_sha256_or_die "${archive}" "${checksum_file}" "${asset_name}"
  else
    info "No checksum asset was found for this release."
    confirm "Install without SHA256 verification?" || die "aborted because checksum verification is unavailable."
  fi

  tar -xzf "${archive}" -C "${extract_dir}" || die "failed to extract ${asset_name}."
  extracted_gost="$(find "${extract_dir}" -type f -name gost -print | head -n 1 || true)"
  [[ -n "${extracted_gost}" ]] || die "extracted archive did not contain a gost binary."

  if [[ -e "${GOST_BIN}" ]]; then
    backup_existing_file "${GOST_BIN}"
  fi
  install -m 755 -o root -g root "${extracted_gost}" "${GOST_BIN}"
  info "Installed ${GOST_BIN}"
  "${GOST_BIN}" -V
  rm -rf "${tmpdir}"
}

verify_sha256_or_die() {
  local archive="$1"
  local checksum_file="$2"
  local asset_name="$3"
  local line expected actual expected_lower actual_lower

  line="$(grep -F "${asset_name}" "${checksum_file}" 2>/dev/null | head -n 1 || true)"
  expected="$(printf '%s\n' "${line}" | grep -Eo '[A-Fa-f0-9]{64}' | head -n 1 || true)"
  if [[ -z "${expected}" ]]; then
    expected="$(grep -Eo '[A-Fa-f0-9]{64}' "${checksum_file}" | head -n 1 || true)"
  fi
  [[ -n "${expected}" ]] || die "checksum file did not contain a SHA256 hash for ${asset_name}."

  actual="$(sha256sum "${archive}" | awk '{print $1}')"
  expected_lower="$(printf '%s' "${expected}" | tr 'A-F' 'a-f')"
  actual_lower="$(printf '%s' "${actual}" | tr 'A-F' 'a-f')"
  if [[ "${expected_lower}" != "${actual_lower}" ]]; then
    die "SHA256 verification failed for ${asset_name}."
  fi
  info "SHA256 verified."
}

prompt_secret_confirmed() {
  local prompt="$1"
  local allow_blank="${2:-0}"
  local first second
  while true; do
    read -r -s -p "${prompt}: " first
    printf '\n' >&2
    if [[ -z "${first}" && "${allow_blank}" == "1" ]]; then
      printf '\n'
      return 0
    fi
    [[ -n "${first}" ]] || { info "Value is required." >&2; continue; }
    read -r -s -p "Confirm password: " second
    printf '\n' >&2
    if [[ "${first}" == "${second}" ]]; then
      printf '%s\n' "${first}"
      return 0
    fi
    info "Passwords did not match; try again." >&2
  done
}

snapshot_kharej_firewall_rules() {
  local number="$1"
  local output="$2"
  local allow_comment="gost-manager:kharej-${number}:allow"
  local drop_comment="gost-manager:kharej-${number}:drop"
  local line position=0 all_rules
  : > "${output}"
  command -v iptables >/dev/null 2>&1 || return 0
  all_rules="$(mktemp)"
  if ! iptables -S INPUT > "${all_rules}" 2>/dev/null; then
    rm -f "${all_rules}"
    return 1
  fi
  while IFS= read -r line; do
    [[ "${line}" == "-A INPUT "* ]] || continue
    position=$((position + 1))
    if [[ "${line}" == *"--comment ${allow_comment}"* || "${line}" == *"--comment \"${allow_comment}\""* || "${line}" == *"--comment ${drop_comment}"* || "${line}" == *"--comment \"${drop_comment}\""* ]]; then
      printf '%s|%s\n' "${position}" "${line}" >> "${output}"
    fi
  done < "${all_rules}"
  rm -f "${all_rules}"
}

mutate_kharej_firewall_rules() {
  local number="$1"
  local port="$2"
  local sources="$3"
  local enabled="$4"
  local restore_file="${5:-}"

  command -v iptables >/dev/null 2>&1 || { printf 'iptables is unavailable; firewall was not changed.\n' >&2; return 1; }
  command -v python3 >/dev/null 2>&1 || { printf 'python3 is unavailable; firewall was not changed.\n' >&2; return 1; }
  python3 - "${number}" "${port}" "${sources}" "${enabled}" "${restore_file}" <<'PY'
import shlex
import subprocess
import sys
from pathlib import Path

number, port, sources_raw, enabled, restore_path = sys.argv[1:]
allow = f"gost-manager:kharej-{number}:allow"
drop = f"gost-manager:kharej-{number}:drop"


def run(args):
    return subprocess.run(["iptables", *args], text=True, capture_output=True, check=False)


def rules():
    result = run(["-S", "INPUT"])
    if result.returncode:
        raise RuntimeError("cannot inspect INPUT rules")
    return [shlex.split(line) for line in result.stdout.splitlines() if line.strip()]


def comment(args):
    try:
        return args[args.index("--comment") + 1]
    except (ValueError, IndexError):
        return None


def managed(items):
    return [item for item in items if comment(item) in {allow, drop}]


def managed_positions(items):
    input_rules = [item for item in items if item[:2] == ["-A", "INPUT"]]
    return [(index, item) for index, item in enumerate(input_rules, 1) if comment(item) in {allow, drop}]


def value_after(args, option, default=""):
    try:
        return args[args.index(option) + 1]
    except (ValueError, IndexError):
        return default


def signatures(items):
    return [
        (
            comment(item),
            value_after(item, "-s"),
            value_after(item, "--dport"),
            value_after(item, "-j"),
        )
        for item in items
    ]


def positioned_signatures(items):
    return [(position, signatures([item])[0]) for position, item in items]


def clear():
    for item in managed(rules()):
        if not item or item[0] != "-A":
            raise RuntimeError("unexpected rule shape")
        candidate = ["-D", *item[1:]]
        if run(candidate).returncode:
            raise RuntimeError("failed to delete managed rule")


def append(items):
    for item in items:
        if not item or item[0] != "-A" or comment(item) not in {allow, drop}:
            raise RuntimeError("invalid saved managed rule")
        if run(item).returncode:
            raise RuntimeError("failed to restore managed rule")


def insert_at_positions(positioned):
    for position, item in sorted(positioned):
        if len(item) < 3 or item[0] != "-A" or item[1] != "INPUT" or comment(item) not in {allow, drop}:
            raise RuntimeError("invalid positioned managed rule")
        candidate = ["-I", "INPUT", str(position), *item[2:]]
        if run(candidate).returncode:
            raise RuntimeError("failed to restore managed rule position")


before_all = rules()
before = managed_positions(before_all)
try:
    clear()
    if restore_path:
        desired_positioned = []
        for line in Path(restore_path).read_text().splitlines():
            if not line.strip() or "|" not in line:
                continue
            raw_position, raw_rule = line.split("|", 1)
            desired_positioned.append((int(raw_position), shlex.split(raw_rule)))
        insert_at_positions(desired_positioned)
        desired = [item for _position, item in desired_positioned]
    elif enabled == "1":
        source_items = [item for item in sources_raw.split(",") if item]
        drop_args = ["-I", "INPUT", "1", "-p", "tcp", "--dport", port, "-m", "comment", "--comment", drop, "-j", "DROP"]
        if run(drop_args).returncode:
            raise RuntimeError("failed to add managed drop")
        for source in reversed(source_items):
            allow_args = ["-I", "INPUT", "1", "-p", "tcp", "-s", source, "--dport", port, "-m", "comment", "--comment", allow, "-j", "ACCEPT"]
            if run(allow_args).returncode:
                raise RuntimeError("failed to add managed allow")
        desired = [
            ["-A", "INPUT", "-p", "tcp", "-s", source, "--dport", port, "-m", "comment", "--comment", allow, "-j", "ACCEPT"]
            for source in source_items
        ] + [["-A", "INPUT", "-p", "tcp", "--dport", port, "-m", "comment", "--comment", drop, "-j", "DROP"]]
    else:
        desired = []
    current_all = rules()
    if signatures(managed(current_all)) != signatures(desired):
        raise RuntimeError("managed firewall verification failed")
    if restore_path and positioned_signatures(managed_positions(current_all)) != positioned_signatures(desired_positioned):
        raise RuntimeError("managed firewall position verification failed")
except Exception:
    try:
        clear()
        insert_at_positions(before)
        if positioned_signatures(managed_positions(rules())) != positioned_signatures(before):
            raise RuntimeError("rollback verification failed")
    except Exception:
        pass
    raise SystemExit(1)
PY
}

transition_kharej_firewall_rules() {
  local action="$1"
  local number="$2"
  local port="$3"
  local sources="$4"
  local enabled="$5"
  local candidate_count="${6:-0}"
  local snapshot="${7:-}"
  command -v iptables >/dev/null 2>&1 || return 1
  python3 - "${action}" "${number}" "${port}" "${sources}" "${enabled}" "${candidate_count}" "${snapshot}" <<'PY'
import shlex
import subprocess
import sys
from pathlib import Path

action, number, port, sources_raw, enabled, count_raw, snapshot_path = sys.argv[1:]
allow = f"gost-manager:kharej-{number}:allow"
drop = f"gost-manager:kharej-{number}:drop"


def run(args):
    return subprocess.run(["iptables", *args], text=True, capture_output=True, check=False)


def input_rules():
    result = run(["-S", "INPUT"])
    if result.returncode:
        raise RuntimeError("cannot inspect INPUT rules")
    parsed = [shlex.split(line) for line in result.stdout.splitlines() if line.strip()]
    return [item for item in parsed if item[:2] == ["-A", "INPUT"]]


def rule_comment(rule):
    try:
        return rule[rule.index("--comment") + 1]
    except (ValueError, IndexError):
        return None


def is_managed(rule):
    return rule_comment(rule) in {allow, drop}


def candidate_rules():
    if enabled != "1":
        return []
    sources = [source for source in sources_raw.split(",") if source]
    return [
        ["-A", "INPUT", "-p", "tcp", "-s", source, "--dport", port,
         "-m", "comment", "--comment", allow, "-j", "ACCEPT"]
        for source in sources
    ] + [["-A", "INPUT", "-p", "tcp", "--dport", port,
          "-m", "comment", "--comment", drop, "-j", "DROP"]]


def insert_rule(position, rule):
    if rule[:2] != ["-A", "INPUT"]:
        raise RuntimeError("invalid rule")
    if run(["-I", "INPUT", str(position), *rule[2:]]).returncode:
        raise RuntimeError("rule insertion failed")


def delete_position(position):
    if run(["-D", "INPUT", str(position)]).returncode:
        raise RuntimeError("rule deletion failed")


def managed_positions(items):
    return [(position, rule) for position, rule in enumerate(items, 1) if is_managed(rule)]


candidate = candidate_rules()

if action == "prepare":
    before = input_rules()
    added = 0
    try:
        for rule in reversed(candidate):
            insert_rule(1, rule)
            added += 1
        if input_rules() != candidate + before:
            raise RuntimeError("candidate verification failed")
    except Exception:
        try:
            for _index in range(added):
                delete_position(1)
            if input_rules() != before:
                raise RuntimeError("prepare rollback verification failed")
        except Exception:
            raise SystemExit(2)
        raise SystemExit(1)
    print(len(candidate))
    raise SystemExit(0)

if action == "finalize":
    count = int(count_raw)
    current = input_rules()
    if count != len(candidate) or current[:count] != candidate:
        raise SystemExit(1)
    obsolete = [position for position, rule in managed_positions(current) if position > count]
    try:
        for position in sorted(obsolete, reverse=True):
            delete_position(position)
    except Exception:
        raise SystemExit(1)
    final = input_rules()
    if final[:count] != candidate or managed_positions(final) != list(enumerate(candidate, 1)):
        raise SystemExit(1)
    raise SystemExit(0)

if action == "rollback":
    count = int(count_raw)
    current = input_rules()
    candidate_block = current[:count]
    if len(candidate_block) != count or any(not is_managed(rule) for rule in candidate_block):
        raise SystemExit(1)
    saved = []
    if snapshot_path:
        for line in Path(snapshot_path).read_text().splitlines():
            if not line.strip() or "|" not in line:
                continue
            position_raw, rule_raw = line.split("|", 1)
            rule = shlex.split(rule_raw)
            if rule[:2] != ["-A", "INPUT"] or not is_managed(rule):
                raise SystemExit(1)
            saved.append((int(position_raw), rule))
    try:
        obsolete = [position for position, _rule in managed_positions(current) if position > count]
        for position in sorted(obsolete, reverse=True):
            delete_position(position)
        for position, rule in sorted(saved):
            insert_rule(position + count, rule)
        with_candidate = input_rules()
        expected_shifted = [(position + count, rule) for position, rule in saved]
        actual_shifted = [item for item in managed_positions(with_candidate) if item[0] > count]
        if with_candidate[:count] != candidate_block or actual_shifted != expected_shifted:
            raise RuntimeError("old rule staging verification failed")
        for _index in range(count):
            delete_position(1)
        if managed_positions(input_rules()) != saved:
            raise RuntimeError("rollback verification failed")
    except Exception:
        raise SystemExit(1)
    raise SystemExit(0)

raise SystemExit(1)
PY
}

prepare_kharej_firewall_transition() {
  transition_kharej_firewall_rules prepare "$1" "$2" "$3" "$4"
}

finalize_kharej_firewall_transition() {
  transition_kharej_firewall_rules finalize "$1" "$2" "$3" "$4" "$5"
}

rollback_kharej_firewall_transition() {
  transition_kharej_firewall_rules rollback "$1" 1 "" 0 "$2" "$3"
}

add_kharej_firewall_rules() {
  local number="$1"
  local sources="$2"
  local port="$3"
  mutate_kharej_firewall_rules "${number}" "${port}" "${sources}" 1 || return 1
  cat <<'WARN'
Warning: iptables rules are not persistent by default.
They may be lost after reboot unless saved with netfilter-persistent or your server firewall system.
WARN
}

restore_kharej_firewall_rules() {
  local number="$1"
  local snapshot="$2"
  mutate_kharej_firewall_rules "${number}" 1 "" 0 "${snapshot}"
}

delete_kharej_firewall_rules() {
  local number="$1"
  command -v iptables >/dev/null 2>&1 || return 0
  mutate_kharej_firewall_rules "${number}" 1 "" 0
}

configured_port_inventory() {
  local output="$1"
  local profiles invalid_files invalid_list side number _service _unit env_file ports port
  local port_items=()
  profiles="$(mktemp)"
  invalid_files="$(mktemp)"
  discover_existing_tunnels "${profiles}"
  find_invalid_profile_env_files "${profiles}" "${invalid_files}"
  invalid_list="|"
  while IFS= read -r env_file; do invalid_list="${invalid_list}${env_file}|"; done < "${invalid_files}"
  : > "${output}"
  while IFS='|' read -r side number _service _unit env_file; do
    [[ -n "${side}" ]] || continue
    if [[ ! -f "${env_file}" || -L "${env_file}" || "${invalid_list}" == *"|${env_file}|"* ]] || ! profile_env_load "${env_file}" "${side}" 1; then
      printf 'incomplete|%s-%s\n' "${side}" "${number}" >> "${output}"
      continue
    fi
    ports="$(profile_local_ports_from_loaded "${side}" || true)"
    if [[ -z "${ports}" ]]; then
      printf 'incomplete|%s-%s\n' "${side}" "${number}" >> "${output}"
      continue
    fi
    IFS=',' read -r -a port_items <<< "${ports}"
    for port in "${port_items[@]}"; do
      printf '%s|%s-%s\n' "${port}" "${side}" "${number}" >> "${output}"
    done
  done < "${profiles}"
  rm -f "${profiles}" "${invalid_files}"
  sort -t '|' -k1,1n -k2,2 "${output}" -o "${output}"
}

validate_configured_ports() {
  local candidate_id="$1"
  local ports="$2"
  local inventory port owner candidate conflict_owner=""
  local candidates=()
  inventory="$(mktemp)"
  configured_port_inventory "${inventory}"
  IFS=',' read -r -a candidates <<< "${ports}"
  for candidate in "${candidates[@]}"; do
    while IFS='|' read -r port owner; do
      [[ "${port}" == "${candidate}" ]] || continue
      if [[ "${owner}" != "${candidate_id}" ]]; then
        conflict_owner="${owner}"
        break
      fi
    done < "${inventory}"
    [[ -z "${conflict_owner}" ]] || break
  done
  if [[ -n "${conflict_owner}" ]]; then
    printf 'Configured local port %s conflicts with %s. No files were changed.\n' "${candidate}" "${conflict_owner}" >&2
    rm -f "${inventory}"
    return 1
  fi
  rm -f "${inventory}"
}

take_bounded_ss_snapshot() {
  local output="$1"
  shift
  command -v ss >/dev/null 2>&1 || return 1
  python3 - "${output}" "$@" <<'PY'
import pathlib
import subprocess
import sys

output = pathlib.Path(sys.argv[1])
try:
    result = subprocess.run(
        ["ss", *sys.argv[2:]],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=5,
        check=False,
    )
except (OSError, subprocess.TimeoutExpired):
    raise SystemExit(1)
if result.returncode or len(result.stdout) > 4 * 1024 * 1024 or result.stdout.count(b"\n") > 100000:
    raise SystemExit(1)
output.write_bytes(result.stdout)
PY
}

take_listen_snapshot() {
  local output="$1"
  take_bounded_ss_snapshot "${output}" -H -lntp
}

take_connection_snapshot() {
  local output="$1"
  take_bounded_ss_snapshot "${output}" -H -tanp
}

capture_service_state() {
  local service="$1"
  local output="$2"
  local raw line key value load_state="" unit_state="" active_state="" main_pid="" seen="|" invalid=0
  raw="$(mktemp)"
  if ! systemctl show "${service}" --no-pager \
    --property=LoadState,UnitFileState,ActiveState,MainPID > "${raw}" 2>/dev/null; then
    rm -f "${raw}"
    return 1
  fi
  while IFS= read -r line; do
    [[ "${line}" =~ ^([A-Za-z]+)=(.*)$ ]] || { invalid=1; break; }
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    [[ "${seen}" != *"|${key}|"* ]] || { invalid=1; break; }
    seen="${seen}${key}|"
    case "${key}" in
      LoadState) load_state="${value}" ;;
      UnitFileState) unit_state="${value}" ;;
      ActiveState) active_state="${value}" ;;
      MainPID) main_pid="${value}" ;;
      *) invalid=1; break ;;
    esac
  done < "${raw}"
  rm -f "${raw}"
  [[ "${invalid}" -eq 0 ]] || return 1
  [[ "${seen}" == *"|LoadState|"* && "${seen}" == *"|UnitFileState|"* && "${seen}" == *"|ActiveState|"* && "${seen}" == *"|MainPID|"* ]] || return 1
  unit_state="${unit_state:-none}"
  [[ "${load_state}" =~ ^[A-Za-z0-9_.-]+$ ]] || return 1
  [[ "${unit_state}" =~ ^[A-Za-z0-9_.-]+$ ]] || return 1
  [[ "${active_state}" =~ ^[A-Za-z0-9_.-]+$ ]] || return 1
  [[ "${main_pid}" =~ ^[0-9]+$ ]] || return 1
  if [[ "${active_state}" == "active" ]]; then
    [[ "${main_pid}" =~ ^[1-9][0-9]*$ ]] || return 1
  elif [[ "${active_state}" == "inactive" ]]; then
    [[ "${main_pid}" == "0" ]] || return 1
  fi
  printf '%s|%s|%s|%s\n' "${load_state}" "${unit_state}" "${active_state}" "${main_pid}" > "${output}"
}

read_captured_service_state() {
  local state_file="$1"
  local extra=""
  IFS='|' read -r CAPTURED_LOAD_STATE CAPTURED_UNIT_STATE CAPTURED_ACTIVE_STATE CAPTURED_MAIN_PID extra < "${state_file}" || return 1
  [[ -z "${extra}" ]] || return 1
  [[ "${CAPTURED_LOAD_STATE}" =~ ^[A-Za-z0-9_.-]+$ ]] || return 1
  [[ "${CAPTURED_UNIT_STATE}" =~ ^[A-Za-z0-9_.-]+$ ]] || return 1
  [[ "${CAPTURED_ACTIVE_STATE}" =~ ^[A-Za-z0-9_.-]+$ ]] || return 1
  [[ "${CAPTURED_MAIN_PID}" =~ ^[0-9]+$ ]]
}

verify_service_state_matches() {
  local service="$1"
  local expected_file="$2"
  local current_file expected_load expected_unit expected_active expected_pid
  read_captured_service_state "${expected_file}" || return 1
  expected_load="${CAPTURED_LOAD_STATE}"
  expected_unit="${CAPTURED_UNIT_STATE}"
  expected_active="${CAPTURED_ACTIVE_STATE}"
  expected_pid="${CAPTURED_MAIN_PID}"
  current_file="$(mktemp)"
  if ! capture_service_state "${service}" "${current_file}" || ! read_captured_service_state "${current_file}"; then
    rm -f "${current_file}"
    return 1
  fi
  rm -f "${current_file}"
  [[ "${CAPTURED_LOAD_STATE}" == "${expected_load}" ]] || return 1
  [[ "${CAPTURED_UNIT_STATE}" == "${expected_unit}" ]] || return 1
  [[ "${CAPTURED_ACTIVE_STATE}" == "${expected_active}" ]] || return 1
  if [[ "${expected_active}" == "active" ]]; then
    [[ "${CAPTURED_MAIN_PID}" =~ ^[1-9][0-9]*$ ]]
  elif [[ "${expected_pid}" == "0" ]]; then
    [[ "${CAPTURED_MAIN_PID}" == "0" ]]
  fi
}

service_is_inactive_disabled() {
  local service="$1"
  local state_file
  state_file="$(mktemp)"
  if ! capture_service_state "${service}" "${state_file}" || ! read_captured_service_state "${state_file}"; then
    rm -f "${state_file}"
    return 1
  fi
  rm -f "${state_file}"
  [[ "${CAPTURED_ACTIVE_STATE}" == "inactive" && "${CAPTURED_UNIT_STATE}" == "disabled" && "${CAPTURED_MAIN_PID}" == "0" ]]
}

service_is_inactive() {
  local service="$1"
  local state_file
  state_file="$(mktemp)"
  if ! capture_service_state "${service}" "${state_file}" || ! read_captured_service_state "${state_file}"; then
    rm -f "${state_file}"
    return 1
  fi
  rm -f "${state_file}"
  [[ "${CAPTURED_ACTIVE_STATE}" == "inactive" && "${CAPTURED_MAIN_PID}" == "0" ]]
}

stop_disable_service_verified() {
  local service="$1"
  if ! systemctl disable "${service}"; then
    :
  fi
  if ! systemctl stop "${service}"; then
    :
  fi
  service_is_inactive_disabled "${service}"
}

stop_service_verified() {
  local service="$1"
  if ! systemctl stop "${service}"; then
    :
  fi
  service_is_inactive "${service}"
}

restore_service_state() {
  local service="$1"
  local expected_file="$2"
  local expected_unit expected_active
  read_captured_service_state "${expected_file}" || return 1
  expected_unit="${CAPTURED_UNIT_STATE}"
  expected_active="${CAPTURED_ACTIVE_STATE}"
  case "${expected_unit}" in
    enabled) if ! systemctl enable "${service}" >/dev/null 2>&1; then :; fi ;;
    enabled-runtime) if ! systemctl enable --runtime "${service}" >/dev/null 2>&1; then :; fi ;;
    disabled) if ! systemctl disable "${service}" >/dev/null 2>&1; then :; fi ;;
  esac
  case "${expected_active}" in
    active) if ! systemctl start "${service}" >/dev/null 2>&1; then :; fi ;;
    inactive) if ! systemctl stop "${service}" >/dev/null 2>&1; then :; fi ;;
    *) return 1 ;;
  esac
  verify_service_state_matches "${service}" "${expected_file}"
}

csv_contains() {
  local csv="$1"
  local wanted="$2"
  [[ ",${csv}," == *",${wanted},"* ]]
}

ss_line_local_port() {
  local line="$1"
  local state _recvq _sendq local_address _peer_address _remainder port
  read -r state _recvq _sendq local_address _peer_address _remainder <<< "${line}"
  [[ "${state}" == "LISTEN" ]] || return 1
  port="${local_address##*:}"
  [[ "${port}" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "${port}"
}

validate_live_ports_snapshot() {
  local snapshot="$1"
  local ports="$2"
  local selected_id="${3:-}"
  local unchanged_ports="${4:-}"
  local selected_parts side number selected_pid line line_port candidate owner needs_selected_pid=0
  local candidates=()
  selected_pid=""
  IFS=',' read -r -a candidates <<< "${ports}"
  if [[ -n "${selected_id}" ]]; then
    for candidate in "${candidates[@]}"; do
      if csv_contains "${unchanged_ports}" "${candidate}"; then
        needs_selected_pid=1
        break
      fi
    done
  fi
  if [[ "${needs_selected_pid}" -eq 1 ]]; then
    selected_parts="$(profile_id_parts "${selected_id}")" || return 1
    side="${selected_parts%% *}"
    number="${selected_parts#* }"
    selected_pid="$(systemctl show -p MainPID --value "$(service_name "${side}" "${number}")" 2>/dev/null || true)"
  fi
  for candidate in "${candidates[@]}"; do
    while IFS= read -r line; do
      line_port="$(ss_line_local_port "${line}" || true)"
      [[ "${line_port}" == "${candidate}" ]] || continue
      if [[ -n "${selected_id}" ]] && csv_contains "${unchanged_ports}" "${candidate}" && [[ "${selected_pid}" =~ ^[1-9][0-9]*$ ]] && [[ "${line}" == *"pid=${selected_pid},"* ]]; then
        continue
      fi
      owner="unknown ownership"
      if [[ "${line}" =~ pid=([1-9][0-9]*), ]]; then
        owner="pid=${BASH_REMATCH[1]}"
      fi
      printf 'Live local port %s is occupied (%s). No files were changed.\n' "${candidate}" "${owner}" >&2
      return 1
    done < "${snapshot}"
  done
}

validate_profile_ports_before_write() {
  local candidate_id="$1"
  local ports="$2"
  local selected_id="${3:-}"
  local unchanged_ports="${4:-}"
  local snapshot candidate_copy identity side result
  identity="$(profile_id_parts "${candidate_id}")" || return 1
  side="${identity%% *}"
  candidate_copy="$(mktemp)"
  chmod 600 "${candidate_copy}"
  profile_env_write_lines "${side}" "${candidate_copy}" || { rm -f "${candidate_copy}"; return 1; }
  if ! validate_configured_ports "${candidate_id}" "${ports}"; then
    profile_env_load "${candidate_copy}" "${side}" || true
    rm -f "${candidate_copy}"
    return 1
  fi
  profile_env_load "${candidate_copy}" "${side}" || { rm -f "${candidate_copy}"; return 1; }
  rm -f "${candidate_copy}"
  snapshot="$(mktemp)"
  if ! take_listen_snapshot "${snapshot}"; then
    rm -f "${snapshot}"
    printf 'Cannot verify live local ports; ownership is unavailable. No files were changed.\n' >&2
    return 1
  fi
  validate_live_ports_snapshot "${snapshot}" "${ports}" "${selected_id}" "${unchanged_ports}"
  result=$?
  rm -f "${snapshot}"
  return "${result}"
}

verify_profile_listeners() {
  local service="$1"
  local ports="$2"
  local strict_pid="${3:-0}"
  local pid snapshot _attempt line line_port expected found all_found
  local expected_ports=()
  pid="$(systemctl show -p MainPID --value "${service}" 2>/dev/null || true)"
  if [[ ! "${pid}" =~ ^[1-9][0-9]*$ ]]; then
    [[ "${strict_pid}" == "0" ]]
    return $?
  fi
  IFS=',' read -r -a expected_ports <<< "${ports}"
  snapshot="$(mktemp)"
  for _attempt in 1 2 3 4 5; do
    if take_listen_snapshot "${snapshot}"; then
      all_found=1
      for expected in "${expected_ports[@]}"; do
        found=0
        while IFS= read -r line; do
          line_port="$(ss_line_local_port "${line}" || true)"
          if [[ "${line_port}" == "${expected}" && "${line}" == *"pid=${pid},"* ]]; then
            found=1
            break
          fi
        done < "${snapshot}"
        [[ "${found}" -eq 1 ]] || all_found=0
      done
      if [[ "${all_found}" -eq 1 ]]; then
        rm -f "${snapshot}"
        return 0
      fi
    fi
    if [[ "${GOST_MANAGER_TESTING:-0}" != "1" ]]; then sleep 1; fi
  done
  rm -f "${snapshot}"
  return 1
}

verify_active_profile_listener() {
  local service="$1"
  local ports="$2"
  local state_file
  state_file="$(mktemp)"
  if ! capture_service_state "${service}" "${state_file}" || ! read_captured_service_state "${state_file}"; then
    rm -f "${state_file}"
    return 1
  fi
  rm -f "${state_file}"
  [[ "${CAPTURED_ACTIVE_STATE}" == "active" && "${CAPTURED_MAIN_PID}" =~ ^[1-9][0-9]*$ ]] || return 1
  verify_profile_listeners "${service}" "${ports}" 1
}

check_mapping_ports_free_or_die() {
  local mappings="$1"
  local ports pair
  local pairs=()
  IFS=',' read -r -a pairs <<< "${mappings}"
  ports=""
  for pair in "${pairs[@]}"; do
    ports="${ports}${ports:+,}${pair%%:*}"
  done
  validate_profile_ports_before_write "iran-0" "${ports}" || exit 1
}

ensure_profile_directories() {
  if [[ ! -e "${GOST_ETC_DIR}" ]]; then
    mkdir -p "${GOST_ETC_DIR}"
  fi
  [[ -d "${GOST_ETC_DIR}" && ! -L "${GOST_ETC_DIR}" ]] || die "unsafe GOST env directory."
  [[ -d "${SYSTEMD_DIR}" && ! -L "${SYSTEMD_DIR}" ]] || die "unsafe systemd directory."
  chmod 700 "${GOST_ETC_DIR}"
  set_production_owner "${GOST_ETC_DIR}"
}

install_new_profile_from_loaded() {
  local side="$1"
  local number="$2"
  local start_profile="${3:-1}"
  local profile_id="${side}-${number}"
  local env_file svc_file service runner ports sources firewall snapshot activation_state failure lifecycle_mutated=0 rollback_failed

  validate_side "${side}" || return 1
  validate_tunnel_number_or_die "${number}"
  profile_identity_exists "${side}" "${number}" && { printf 'Profile %s already exists; creation never overwrites it.\n' "${profile_id}" >&2; return 1; }
  validate_loaded_profile "${side}"
  if [[ "${side}" == "kharej" && "$(profile_env_value FIREWALL_ENABLED)" == "1" ]] && ! command -v iptables >/dev/null 2>&1; then
    printf 'iptables is required before creating a firewall-enabled Kharej profile.\n' >&2
    return 1
  fi
  ports="$(profile_local_ports_from_loaded "${side}")" || return 1
  validate_profile_ports_before_write "${profile_id}" "${ports}" || return 1

  ensure_profile_directories
  env_file="$(env_path "${side}" "${number}")"
  svc_file="$(service_path "${side}" "${number}")"
  service="$(service_name "${side}" "${number}")"
  if [[ "${side}" == "iran" ]]; then
    runner="${RUNNER_IRAN}"
  else
    runner="${RUNNER_KHAREJ}"
  fi

  snapshot="$(mktemp)"
  activation_state="$(mktemp)"
  chmod 600 "${snapshot}"
  if [[ "${side}" == "kharej" ]]; then
    snapshot_kharej_firewall_rules "${number}" "${snapshot}" || { rm -f "${snapshot}" "${activation_state}"; return 1; }
  fi

  failure=0
  write_loaded_profile_env "${env_file}" "${side}" || failure=1
  if [[ "${failure}" -eq 0 ]]; then
    write_service_file "${svc_file}" "GOST ${side} Direct profile ${number}" "${env_file}" "${runner}" || failure=1
  fi
  if [[ "${failure}" -eq 0 && "${side}" == "kharej" ]]; then
    firewall="$(profile_env_value FIREWALL_ENABLED)"
    sources="$(profile_sources_from_loaded)"
    if [[ "${firewall}" == "1" ]]; then
      add_kharej_firewall_rules "${number}" "${sources}" "${ports}" || failure=1
    fi
  fi
  if [[ "${failure}" -eq 0 ]]; then
    systemctl daemon-reload || failure=1
  fi
  if [[ "${failure}" -eq 0 && "${start_profile}" == "1" ]]; then
    capture_service_state "${service}" "${activation_state}" || failure=1
  fi
  if [[ "${failure}" -eq 0 && "${start_profile}" == "1" ]]; then
    lifecycle_mutated=1
    systemctl enable --now "${service}" || failure=1
    if [[ "${failure}" -eq 0 ]]; then
      verify_active_profile_listener "${service}" "${ports}" || failure=1
    fi
  fi

  if [[ "${failure}" -ne 0 ]]; then
    if [[ "${lifecycle_mutated}" -eq 1 ]] && { ! stop_disable_service_verified "${service}" || ! verify_service_state_matches "${service}" "${activation_state}"; }; then
      printf 'Profile %s activation failed and inactive/disabled rollback could not be proven. Retained recovery files: %s %s %s %s\n' \
        "${profile_id}" "${env_file}" "${svc_file}" "${snapshot}" "${activation_state}" >&2
      return 1
    fi
    rollback_failed=0
    if [[ "${side}" == "kharej" ]]; then
      restore_kharej_firewall_rules "${number}" "${snapshot}" || rollback_failed=1
    fi
    if [[ "${rollback_failed}" -ne 0 ]]; then
      printf 'Profile %s activation failed and firewall rollback could not be proven. Retained recovery files: %s %s %s\n' \
        "${profile_id}" "${env_file}" "${svc_file}" "${snapshot}" >&2
      rm -f "${activation_state}"
      return 1
    fi
    if ! rm -f "${svc_file}" "${env_file}" || ! systemctl daemon-reload >/dev/null 2>&1; then
      printf 'Profile %s activation failed; service is proven inactive/disabled but file cleanup needs operator recovery: %s %s\n' \
        "${profile_id}" "${env_file}" "${svc_file}" >&2
      rm -f "${snapshot}" "${activation_state}"
      return 1
    fi
    rm -f "${snapshot}" "${activation_state}"
    printf 'Profile %s creation failed; only its new files and managed firewall rules were rolled back.\n' "${profile_id}" >&2
    return 1
  fi

  rm -f "${snapshot}" "${activation_state}"
  info "Profile ${profile_id} created successfully."
  info "Service: ${service}"
  info "Env: ${env_file}"
  info "Local ports: ${ports}"
  if [[ "${start_profile}" != "1" ]]; then
    info "Profile created without starting its service."
  fi
}

create_kharej_tunnel() {
  require_root
  ensure_commands systemctl ss python3

  local suggested number label port user password sources firewall_enabled
  suggested="$(next_free_profile_number kharej)" || die "cannot allocate a Kharej profile number."
  number="$(prompt_default "Kharej profile number" "${suggested}")"
  validate_tunnel_number_or_die "${number}"
  profile_identity_exists kharej "${number}" && die "profile kharej-${number} already exists. Use Edit instead."
  label="$(prompt_default "Profile label (optional)" "")"
  validate_profile_label_or_die "${label}"
  port="$(prompt_default "SOCKS listen port" "28420")"
  validate_port_or_die "${port}"
  user="$(prompt_default "GOST username" "maya")"
  validate_token_or_die "GOST username" "${user}"
  password="$(prompt_secret_confirmed "GOST password")"
  validate_token_or_die "GOST password" "${password}"
  sources="$(prompt_required "Allowed Iran IPv4/CIDRs, comma-separated (for example 198.51.100.10)")"
  sources="$(canonicalize_allowed_sources "${sources}")" || die "allowed sources must be 1-64 canonical IPv4 networks from /8 through /32."
  if confirm "Apply profile-scoped iptables firewall rules?"; then
    firewall_enabled=1
    ensure_commands iptables
  else
    firewall_enabled=0
  fi

  profile_env_reset
  profile_env_set GOST_USER "${user}"
  profile_env_set GOST_PASS "${password}"
  profile_env_set TUNNEL_PORT "${port}"
  profile_env_set ALLOWED_IRAN_SOURCES "${sources}"
  profile_env_set FIREWALL_ENABLED "${firewall_enabled}"
  [[ -z "${label}" ]] || profile_env_set PROFILE_LABEL "${label}"

  cat <<EOF_OUT
New Direct Mode profile: kharej-${number}
Label: ${label:-kharej-${number}}
Local SOCKS port: ${port}
Allowed-source count: $(printf '%s' "${sources}" | awk -F, '{print NF}')
Firewall: $(if [[ "${firewall_enabled}" == "1" ]]; then printf 'enabled'; else printf 'disabled'; fi)
Credentials: configured (redacted)
EOF_OUT
  confirm "Create and start this exact profile?" || die "creation aborted."
  install_new_profile_from_loaded kharej "${number}" 1
}

print_iran_success() {
  local number="$1"
  local env_file="$2"
  local kharej_ip="$3"
  local socks_port="$4"
  local mappings="$5"
  local service
  local IFS=','
  local pairs=()
  local pair listen target
  service="$(service_name iran "${number}")"
  read -r -a pairs <<< "${mappings}"

  cat <<EOF_OUT
Iran tunnel created successfully.

Service: ${service}
Env: ${env_file}
Kharej SOCKS: ${kharej_ip}:${socks_port}

Mappings:
EOF_OUT
  for pair in "${pairs[@]}"; do
    listen="${pair%%:*}"
    target="${pair#*:}"
    printf '  Iran :%-5s -> Kharej 127.0.0.1:%s\n' "${listen}" "${target}"
  done
  printf '\nLocal test examples:\n'
  for pair in "${pairs[@]}"; do
    listen="${pair%%:*}"
    printf '  curl -v --max-time 10 http://127.0.0.1:%s/\n' "${listen}"
  done
  printf '\nPublic/CDN test example:\n'
  if [[ "${#pairs[@]}" -gt 0 ]]; then
    listen="${pairs[0]%%:*}"
    printf '  curl -v --max-time 10 http://YOUR_DOMAIN_OR_IP:%s/\n' "${listen}"
  fi
}

create_iran_tunnel() {
  require_root
  ensure_commands systemctl ss python3

  local suggested number label kharej_ip socks_port user password mappings
  suggested="$(next_free_profile_number iran)" || die "cannot allocate an Iran profile number."
  number="$(prompt_default "Iran profile number" "${suggested}")"
  validate_tunnel_number_or_die "${number}"
  profile_identity_exists iran "${number}" && die "profile iran-${number} already exists. Use Edit instead."
  label="$(prompt_default "Profile label (optional)" "")"
  validate_profile_label_or_die "${label}"
  kharej_ip="$(prompt_required "Kharej IP, for example 203.0.113.20")"
  validate_host_or_die "Kharej IP" "${kharej_ip}"
  socks_port="$(prompt_default "Kharej SOCKS port" "28420")"
  validate_port_or_die "${socks_port}"
  user="$(prompt_default "GOST username" "maya")"
  validate_token_or_die "GOST username" "${user}"
  password="$(prompt_secret_confirmed "Matching GOST password from Kharej")"
  validate_token_or_die "GOST password" "${password}"
  info "Port mappings format: 2052:2052 or 80:80,8080:8080,8880:8880"
  mappings="$(prompt_required "Port mappings")"
  validate_mappings "${mappings}" || exit 1

  profile_env_reset
  profile_env_set GOST_USER "${user}"
  profile_env_set GOST_PASS "${password}"
  profile_env_set KHAREJ_IP "${kharej_ip}"
  profile_env_set TUNNEL_PORT "${socks_port}"
  profile_env_set MAPPINGS "${mappings}"
  [[ -z "${label}" ]] || profile_env_set PROFILE_LABEL "${label}"

  cat <<EOF_OUT
New Direct Mode profile: iran-${number}
Label: ${label:-iran-${number}}
Remote SOCKS endpoint: ${kharej_ip}:${socks_port}
Mappings: ${mappings}
Credentials: configured (redacted)
EOF_OUT
  confirm "Create and start this exact profile?" || die "creation aborted."
  install_new_profile_from_loaded iran "${number}" 1
  print_iran_success "${number}" "$(env_path iran "${number}")" "${kharej_ip}" "${socks_port}" "${mappings}"
}

discover_existing_tunnels() {
  local output_file="$1"
  local service_file env_file base service identity side number
  local tmp_file
  tmp_file="$(mktemp)"
  : > "${tmp_file}"

  for service_file in "${SYSTEMD_DIR}"/gost-iran-*.service "${SYSTEMD_DIR}"/gost-kharej-*.service; do
    [[ -e "${service_file}" || -L "${service_file}" ]] || continue
    base="${service_file##*/}"
    identity="$(parse_tunnel_service_name "${base}" || true)"
    [[ -n "${identity}" ]] || continue
    side="${identity%% *}"
    number="${identity#* }"
    service="gost-${side}-${number}.service"
    env_file="${GOST_ETC_DIR}/${side}-${number}.env"
    printf '%s|%s|%s|%s|%s\n' "${side}" "${number}" "${service}" "${service_file}" "${env_file}" >> "${tmp_file}"
  done

  for env_file in "${GOST_ETC_DIR}"/iran-*.env "${GOST_ETC_DIR}"/kharej-*.env; do
    [[ -e "${env_file}" || -L "${env_file}" ]] || continue
    identity="$(parse_tunnel_env_name "${env_file}" || true)"
    [[ -n "${identity}" ]] || continue
    side="${identity%% *}"
    number="${identity#* }"
    service="gost-${side}-${number}.service"
    service_file="${SYSTEMD_DIR}/${service}"
    printf '%s|%s|%s|%s|%s\n' "${side}" "${number}" "${service}" "${service_file}" "${env_file}" >> "${tmp_file}"
  done

  sort -t '|' -k1,1 -k2,2n -u "${tmp_file}" > "${output_file}"
  rm -f "${tmp_file}"
}

tunnel_count() {
  local tunnel_file="$1"
  if [[ ! -s "${tunnel_file}" ]]; then
    printf '0\n'
    return 0
  fi
  wc -l < "${tunnel_file}" | tr -d ' '
}

print_tunnel_selector() {
  local tunnel_file="$1"
  local index=0
  local _side _number service service_file env_file status
  printf 'Available GOST tunnels:\n\n'
  while IFS='|' read -r _side _number service service_file env_file; do
    index=$((index + 1))
    status="$(service_status_summary "${service}")"
    printf '%d) %-24s %-16s %s\n' "${index}" "${service}" "${status}" "${env_file}"
    if [[ ! -e "${service_file}" ]]; then
      printf '   %-24s %-16s %s\n' "" "" "(service file missing)"
    elif [[ ! -e "${env_file}" ]]; then
      printf '   %-24s %-16s %s\n' "" "" "(env file missing)"
    fi
  done < "${tunnel_file}"
  printf '\n'
}

select_existing_tunnel() {
  local tunnel_file choice count selected
  tunnel_file="$(mktemp)"
  discover_existing_tunnels "${tunnel_file}"
  count="$(tunnel_count "${tunnel_file}")"
  if [[ "${count}" -eq 0 ]]; then
    rm -f "${tunnel_file}"
    die "no managed GOST tunnels were found."
  fi

  print_tunnel_selector "${tunnel_file}"
  while true; do
    read -r -p "Select tunnel number: " choice
    if is_positive_integer "${choice}" && [[ "${choice}" -ge 1 && "${choice}" -le "${count}" ]]; then
      break
    fi
    info "Select a number between 1 and ${count}."
  done

  selected="$(sed -n "${choice}p" "${tunnel_file}")"
  rm -f "${tunnel_file}"

  SELECTED_TUNNEL_SIDE="${selected%%|*}"
  selected="${selected#*|}"
  SELECTED_TUNNEL_NUMBER="${selected%%|*}"
  selected="${selected#*|}"
  SELECTED_TUNNEL_SERVICE="${selected%%|*}"
  selected="${selected#*|}"
  SELECTED_TUNNEL_SERVICE_FILE="${selected%%|*}"
  SELECTED_TUNNEL_ENV_FILE="${selected#*|}"
}

delete_tunnel() {
  require_root
  ensure_commands systemctl

  local side number service svc_file env_file profile_id label state firewall env_snapshot unit_snapshot firewall_snapshot service_state old_ports="" failed command_failed
  select_existing_tunnel
  side="${SELECTED_TUNNEL_SIDE}"
  number="${SELECTED_TUNNEL_NUMBER}"
  service="${SELECTED_TUNNEL_SERVICE}"
  svc_file="${SELECTED_TUNNEL_SERVICE_FILE}"
  env_file="${SELECTED_TUNNEL_ENV_FILE}"
  profile_id="${side}-${number}"
  label="${profile_id}"
  firewall="not applicable"
  if [[ -f "${env_file}" && ! -L "${env_file}" ]] && profile_env_load "${env_file}" "${side}"; then
    label="$(profile_env_value PROFILE_LABEL || true)"
    label="${label:-${profile_id}}"
    if [[ "${side}" == "kharej" ]]; then
      firewall="$(profile_env_value FIREWALL_ENABLED || true)"
      firewall="${firewall:-unknown}"
    fi
    old_ports="$(profile_local_ports_from_loaded "${side}" || true)"
  fi
  state="$(service_status_summary "${service}")"
  cat <<EOF_OUT
Delete profile: ${profile_id}
Label: ${label}
Service: ${service} (${state})
Env: ${env_file}
Unit: ${svc_file}
Firewall: ${firewall}
EOF_OUT
  confirm "Stop, disable, and delete only ${profile_id}?" || die "delete aborted."

  if [[ "${side}" == "kharej" && "${firewall}" == "1" ]] && ! command -v iptables >/dev/null 2>&1; then
    printf 'iptables is unavailable; service, env, unit, and firewall were preserved.\n' >&2
    return 1
  fi

  validate_managed_destination "${env_file}" env || { [[ ! -e "${env_file}" && ! -L "${env_file}" ]] || die "unsafe env destination; nothing was deleted."; }
  validate_managed_destination "${svc_file}" unit || { [[ ! -e "${svc_file}" && ! -L "${svc_file}" ]] || die "unsafe unit destination; nothing was deleted."; }
  service_state="$(mktemp)"
  if ! capture_service_state "${service}" "${service_state}"; then
    rm -f "${service_state}"
    printf 'Could not capture exact service state; nothing was changed.\n' >&2
    return 1
  fi
  env_snapshot=""
  unit_snapshot=""
  if [[ -f "${env_file}" ]] && ! env_snapshot="$(snapshot_managed_file "${env_file}")"; then
    rm -f "${service_state}"
    printf 'Could not snapshot the selected env; nothing was changed.\n' >&2
    return 1
  fi
  if [[ -f "${svc_file}" ]] && ! unit_snapshot="$(snapshot_managed_file "${svc_file}")"; then
    rm -f "${env_snapshot}" "${service_state}"
    printf 'Could not snapshot the selected unit; nothing was changed.\n' >&2
    return 1
  fi
  firewall_snapshot="$(mktemp)"
  chmod 600 "${firewall_snapshot}"
  if [[ "${side}" == "kharej" ]] && ! snapshot_kharej_firewall_rules "${number}" "${firewall_snapshot}"; then
    rm -f "${env_snapshot}" "${unit_snapshot}" "${firewall_snapshot}" "${service_state}"
    printf 'Could not snapshot the selected firewall rules; nothing was changed.\n' >&2
    return 1
  fi
  command_failed=0
  systemctl disable --now "${service}" || command_failed=1
  if [[ "${command_failed}" -ne 0 ]] || ! service_is_inactive_disabled "${service}"; then
    failed=0
    restore_service_state "${service}" "${service_state}" || failed=1
    if [[ "${failed}" -eq 0 ]] && read_captured_service_state "${service_state}" && [[ "${CAPTURED_ACTIVE_STATE}" == "active" && -n "${old_ports}" ]]; then
      verify_profile_listeners "${service}" "${old_ports}" 1 || failed=1
    fi
    if [[ "${failed}" -eq 0 ]]; then
      rm -f "${env_snapshot}" "${unit_snapshot}" "${firewall_snapshot}" "${service_state}"
      printf 'Could not stop/disable %s; its exact prior service state was restored and files/firewall were not changed.\n' "${service}" >&2
    else
      printf 'Could not stop/disable %s and exact service-state restoration was not proven. Retained recovery snapshots: %s %s %s %s\n' \
        "${service}" "${env_snapshot:-none}" "${unit_snapshot:-none}" "${firewall_snapshot}" "${service_state}" >&2
    fi
    return 1
  fi

  if [[ "${side}" == "kharej" ]]; then
    if ! delete_kharej_firewall_rules "${number}"; then
      failed=0
      restore_kharej_firewall_rules "${number}" "${firewall_snapshot}" || failed=1
      restore_service_state "${service}" "${service_state}" || failed=1
      if [[ "${failed}" -eq 0 ]] && read_captured_service_state "${service_state}" && [[ "${CAPTURED_ACTIVE_STATE}" == "active" && -n "${old_ports}" ]]; then
        verify_profile_listeners "${service}" "${old_ports}" 1 || failed=1
      fi
      if [[ "${failed}" -eq 0 ]]; then
        rm -f "${env_snapshot}" "${unit_snapshot}" "${firewall_snapshot}" "${service_state}"
        printf 'Firewall removal failed; the profile and service state were restored.\n' >&2
      else
        printf 'Firewall removal failed and restoration could not be proven. Retained recovery snapshots: %s %s %s %s\n' \
          "${env_snapshot:-none}" "${unit_snapshot:-none}" "${firewall_snapshot}" "${service_state}" >&2
      fi
      return 1
    fi
  fi

  failed=0
  [[ ! -e "${env_file}" ]] || rm -f "${env_file}" || failed=1
  [[ ! -e "${svc_file}" ]] || rm -f "${svc_file}" || failed=1
  if [[ "${failed}" -eq 0 && -n "${unit_snapshot}" ]]; then
    systemctl daemon-reload || failed=1
  fi
  if [[ "${failed}" -ne 0 ]]; then
    [[ -z "${env_snapshot}" ]] || restore_managed_snapshot "${env_snapshot}" "${env_file}" env || failed=2
    [[ -z "${unit_snapshot}" ]] || restore_managed_snapshot "${unit_snapshot}" "${svc_file}" unit || failed=2
    if [[ "${side}" == "kharej" ]]; then
      restore_kharej_firewall_rules "${number}" "${firewall_snapshot}" || failed=2
    fi
    systemctl daemon-reload >/dev/null 2>&1 || failed=2
    restore_service_state "${service}" "${service_state}" || failed=2
    if [[ "${failed}" -ne 2 ]] && read_captured_service_state "${service_state}" && [[ "${CAPTURED_ACTIVE_STATE}" == "active" && -n "${old_ports}" ]]; then
      verify_profile_listeners "${service}" "${old_ports}" 1 || failed=2
    fi
    if [[ "${failed}" -eq 2 ]]; then
      if [[ "${side}" == "iran" ]]; then
        rm -f "${firewall_snapshot}"
        firewall_snapshot=""
      fi
      printf 'Deletion failed and automatic restoration could not be proven. Recovery snapshots: %s %s %s %s\n' \
        "${env_snapshot:-none}" "${unit_snapshot:-none}" "${firewall_snapshot:-none}" "${service_state}" >&2
    else
      rm -f "${env_snapshot}" "${unit_snapshot}" "${firewall_snapshot}" "${service_state}"
      printf 'Deletion failed; the selected profile was restored.\n' >&2
    fi
    return 1
  fi

  rm -f "${env_snapshot}" "${unit_snapshot}" "${firewall_snapshot}" "${service_state}"
  info "Deleted only ${profile_id}."
}

show_related_listen_ports() {
  local side="$1"
  local env_file="$2"
  local service="$3"
  local ports snapshot

  if ! command -v ss >/dev/null 2>&1; then
    info "unknown (ss is unavailable)"
    return 0
  fi
  if [[ ! -f "${env_file}" || -L "${env_file}" ]] || ! profile_env_load "${env_file}" "${side}" || ! loaded_profile_is_valid "${side}"; then
    info "unknown (profile env is missing or malformed)"
    return 0
  fi
  ports="$(profile_local_ports_from_loaded "${side}" || true)"
  [[ -n "${ports}" ]] || { info "unknown (configured ports are invalid)"; return 0; }
  snapshot="$(mktemp)"
  if ! take_listen_snapshot "${snapshot}"; then
    rm -f "${snapshot}"
    info "unknown (socket ownership is unavailable)"
    return 0
  fi
  observed_listener_summary "${snapshot}" "${service}" 1 "${ports}"
  rm -f "${snapshot}"
}

show_safe_service_status() {
  local service="$1"
  printf 'Safe service status for %s:\n' "${service}"
  systemctl show "${service}" --no-pager \
    --property=Id,LoadState,ActiveState,SubState,UnitFileState,NRestarts,MainPID || true
}

show_status() {
  local side service env_file
  select_existing_tunnel
  side="${SELECTED_TUNNEL_SIDE}"
  service="${SELECTED_TUNNEL_SERVICE}"
  env_file="${SELECTED_TUNNEL_ENV_FILE}"
  show_safe_service_status "${service}"
  printf '\nRelated listen ports:\n'
  show_related_listen_ports "${side}" "${env_file}" "${service}"
}

show_logs() {
  local side service env_file user password line journal_output
  select_existing_tunnel
  side="${SELECTED_TUNNEL_SIDE}"
  service="${SELECTED_TUNNEL_SERVICE}"
  env_file="${SELECTED_TUNNEL_ENV_FILE}"
  if [[ ! -f "${env_file}" || -L "${env_file}" ]] || ! profile_env_load "${env_file}" "${side}" || ! loaded_profile_is_valid "${side}"; then
    printf 'Cannot safely redact logs for %s because its env is missing or malformed.\n' "${service}" >&2
    return 1
  fi
  user="$(profile_env_value GOST_USER || true)"
  password="$(profile_env_value GOST_PASS || true)"
  [[ -n "${user}" && -n "${password}" ]] || { printf 'Cannot safely redact logs for %s.\n' "${service}" >&2; return 1; }
  journal_output="$(mktemp)"
  chmod 600 "${journal_output}"
  if ! journalctl -u "${service}" -n 100 --no-pager > "${journal_output}"; then
    rm -f "${journal_output}"
    return 1
  fi
  while IFS= read -r line; do
    line="${line//${user}/[redacted-user]}"
    line="${line//${password}/[redacted-password]}"
    printf '%s\n' "${line}"
  done < "${journal_output}"
  rm -f "${journal_output}"
}

restart_tunnel() {
  require_root
  ensure_commands systemctl

  local service
  select_existing_tunnel
  service="${SELECTED_TUNNEL_SERVICE}"
  systemctl restart "${service}"
  show_safe_service_status "${service}"
}

summarize_iran_env() {
  local file="$1"
  local number="$2"
  local service status mappings kharej_ip socks_port ports IFS pair pairs
  service="$(service_name iran "${number}")"
  status="$(service_status_summary "${service}")"
  mappings="$(env_get MAPPINGS "${file}")"
  kharej_ip="$(env_get KHAREJ_IP "${file}")"
  socks_port="$(env_get TUNNEL_PORT "${file}")"
  IFS=','
  read -r -a pairs <<< "${mappings}"
  ports=""
  for pair in "${pairs[@]}"; do
    if [[ -z "${ports}" ]]; then
      ports="${pair%%:*}"
    else
      ports="${ports}/${pair%%:*}"
    fi
  done
  if [[ "${#pairs[@]}" -eq 1 ]]; then
    printf '%-24s %-16s Port %s -> 127.0.0.1:%s via %s:%s\n' "${service}" "${status:-unknown}" "${pairs[0]%%:*}" "${pairs[0]#*:}" "${kharej_ip}" "${socks_port}"
  else
    printf '%-24s %-16s Ports %s via %s:%s\n' "${service}" "${status:-unknown}" "${ports}" "${kharej_ip}" "${socks_port}"
  fi
}

summarize_kharej_env() {
  local file="$1"
  local number="$2"
  local service status port iran_ip
  service="$(service_name kharej "${number}")"
  status="$(service_status_summary "${service}")"
  port="$(env_get TUNNEL_PORT "${file}")"
  iran_ip="$(env_get IRAN_IP "${file}")"
  printf '%-24s %-16s SOCKS 0.0.0.0:%s, allowed IP %s\n' "${service}" "${status:-unknown}" "${port:-unknown}" "${iran_ip:-unknown}"
}

service_status_summary() {
  local service="$1"
  local active substate
  if ! command -v systemctl >/dev/null 2>&1; then
    printf 'unknown\n'
    return 0
  fi
  active="$(systemctl is-active "${service}" 2>/dev/null || true)"
  substate="$(systemctl show -p SubState --value "${service}" 2>/dev/null || true)"
  if [[ -n "${active}" && -n "${substate}" ]]; then
    printf '%s/%s\n' "${active}" "${substate}"
  elif [[ -n "${active}" ]]; then
    printf '%s\n' "${active}"
  else
    printf 'unknown\n'
  fi
}

established_socket_count() {
  local snapshot="$1"
  local service="$2"
  local authoritative="$3"
  local pid line count=0
  [[ "${authoritative}" == "1" ]] || { printf 'unknown\n'; return 0; }
  pid="$(systemctl show -p MainPID --value "${service}" 2>/dev/null || true)"
  [[ "${pid}" =~ ^[1-9][0-9]*$ ]] || { printf 'unknown\n'; return 0; }
  while IFS= read -r line; do
    if [[ "${line}" == ESTAB* && "${line}" == *"pid=${pid},"* ]]; then
      count=$((count + 1))
    fi
  done < "${snapshot}"
  printf '%s\n' "${count}"
}

observed_listener_summary() {
  local snapshot="$1"
  local service="$2"
  local authoritative="$3"
  local ports="$4"
  local pid line line_port result port
  [[ "${authoritative}" == "1" ]] || { printf 'unknown\n'; return 0; }
  pid="$(systemctl show -p MainPID --value "${service}" 2>/dev/null || true)"
  [[ "${pid}" =~ ^[1-9][0-9]*$ ]] || { printf 'unknown\n'; return 0; }
  result=""
  while IFS= read -r line; do
    [[ "${line}" == LISTEN* && "${line}" == *"pid=${pid},"* ]] || continue
    line_port="$(ss_line_local_port "${line}" || true)"
    [[ -n "${line_port}" ]] || continue
    if csv_contains "${ports}" "${line_port}" && ! csv_contains "${result}" "${line_port}"; then
      result="${result}${result:+,}${line_port}"
    fi
  done < "${snapshot}"
  if [[ -n "${result}" ]]; then
    printf '%s\n' "${result}"
  else
    printf 'none\n'
  fi
}

profile_source_count() {
  local sources="$1"
  local items=()
  [[ -n "${sources}" ]] || { printf '0\n'; return 0; }
  IFS=',' read -r -a items <<< "${sources}"
  printf '%s\n' "${#items[@]}"
}

list_profiles() {
  local profiles sockets invalid_files invalid_list socket_authoritative side number service unit env_file profile_id status label ports remote firewall sources source_count established env_state unit_state
  profiles="$(mktemp)"
  sockets="$(mktemp)"
  invalid_files="$(mktemp)"
  discover_existing_tunnels "${profiles}"
  find_invalid_profile_env_files "${profiles}" "${invalid_files}"
  invalid_list="|"
  while IFS= read -r env_file; do invalid_list="${invalid_list}${env_file}|"; done < "${invalid_files}"
  if take_connection_snapshot "${sockets}"; then
    socket_authoritative=1
  else
    socket_authoritative=0
    : > "${sockets}"
  fi

  printf '%-12s %-8s %-18s %-24s %-16s %-13s %-24s %-9s %-7s %-7s %-7s\n' \
    "PROFILE" "SIDE" "LABEL" "SERVICE" "STATE" "LOCAL PORTS" "REMOTE/SOURCES" "FIREWALL" "ESTAB" "ENV" "UNIT"
  if [[ ! -s "${profiles}" ]]; then
    info "No managed Direct Mode profiles found."
    rm -f "${profiles}" "${sockets}" "${invalid_files}"
    return 0
  fi

  while IFS='|' read -r side number service unit env_file; do
    profile_id="${side}-${number}"
    env_state="missing"
    unit_state="missing"
    status="unit-missing"
    if [[ -e "${unit}" || -L "${unit}" ]]; then
      unit_state="present"
      status="$(service_status_summary "${service}")"
    fi
    label="${profile_id}"
    ports="incomplete"
    remote="incomplete"
    firewall="n/a"
    if [[ -f "${env_file}" && ! -L "${env_file}" && "${invalid_list}" != *"|${env_file}|"* ]] && profile_env_load "${env_file}" "${side}" 1 && loaded_profile_is_valid "${side}"; then
      env_state="present"
      label="$(profile_env_value PROFILE_LABEL || true)"
      label="${label:-${profile_id}}"
      ports="$(profile_local_ports_from_loaded "${side}" || true)"
      ports="${ports:-incomplete}"
      if [[ "${side}" == "iran" ]]; then
        remote="$(profile_env_value KHAREJ_IP || true):$(profile_env_value TUNNEL_PORT || true)"
      else
        sources="$(profile_env_value ALLOWED_IRAN_SOURCES || true)"
        sources="${sources:-$(profile_env_value IRAN_IP || true)}"
        source_count="$(profile_source_count "${sources}")"
        remote="sources=${source_count}"
        firewall="$(profile_env_value FIREWALL_ENABLED || true)"
        firewall="${firewall:-unknown}"
      fi
    elif [[ -e "${env_file}" || -L "${env_file}" ]]; then
      env_state="invalid"
    fi
    established="unknown"
    if [[ "${unit_state}" == "present" ]]; then
      established="$(established_socket_count "${sockets}" "${service}" "${socket_authoritative}")"
    fi
    printf '%-12s %-8s %-18s %-24s %-16s %-13s %-24s %-9s %-7s %-7s %-7s\n' \
      "${profile_id}" "${side}" "${label}" "${service}" "${status}" "${ports}" "${remote}" "${firewall}" "${established}" "${env_state}" "${unit_state}"
  done < "${profiles}"
  rm -f "${profiles}" "${sockets}" "${invalid_files}"
}

show_selected_profile_detail() {
  local side="$1"
  local number="$2"
  local service env_file unit profile_id status label ports sockets socket_authoritative established listeners remote mappings sources firewall
  service="$(service_name "${side}" "${number}")"
  env_file="$(env_path "${side}" "${number}")"
  unit="$(service_path "${side}" "${number}")"
  profile_id="${side}-${number}"
  [[ -f "${env_file}" && ! -L "${env_file}" ]] || { printf 'Profile %s has a missing or unsafe env file.\n' "${profile_id}" >&2; return 1; }
  profile_env_load "${env_file}" "${side}" || { printf 'Profile %s has a malformed env file.\n' "${profile_id}" >&2; return 1; }
  validate_loaded_profile "${side}"
  label="$(profile_env_value PROFILE_LABEL || true)"
  label="${label:-${profile_id}}"
  ports="$(profile_local_ports_from_loaded "${side}")"
  status="$(service_status_summary "${service}")"
  sockets="$(mktemp)"
  if take_connection_snapshot "${sockets}"; then socket_authoritative=1; else socket_authoritative=0; : > "${sockets}"; fi
  established="$(established_socket_count "${sockets}" "${service}" "${socket_authoritative}")"
  listeners="$(observed_listener_summary "${sockets}" "${service}" "${socket_authoritative}" "${ports}")"
  rm -f "${sockets}"
  cat <<EOF_OUT
Profile ID: ${profile_id}
Label: ${label}
Side: ${side}
Env: ${env_file}
Unit: ${unit}
Service: ${service}
State: ${status}
Configured local ports: ${ports}
Observed listeners: ${listeners}
Established sockets: ${established}
EOF_OUT
  if [[ "${side}" == "iran" ]]; then
    remote="$(profile_env_value KHAREJ_IP):$(profile_env_value TUNNEL_PORT)"
    mappings="$(profile_env_value MAPPINGS)"
    info "Remote SOCKS endpoint: ${remote}"
    info "Target mappings: ${mappings}"
  else
    sources="$(profile_sources_from_loaded)"
    firewall="$(profile_env_value FIREWALL_ENABLED)"
    info "Allowed Iran sources: ${sources}"
    info "Allowed-source count: $(profile_source_count "${sources}")"
    info "Firewall: ${firewall}"
  fi
  if [[ -x "${MONITOR_BIN}" ]]; then
    printf '\nLast Monitoring Lite observation:\n'
    "${MONITOR_BIN}" tunnel "${profile_id}" --window 10m || info "Monitoring observation unavailable."
  else
    info "Last Monitoring Lite observation: unavailable"
  fi
}

show_profile_detail() {
  select_existing_tunnel
  show_selected_profile_detail "${SELECTED_TUNNEL_SIDE}" "${SELECTED_TUNNEL_NUMBER}"
}

verify_listener_for_captured_state() {
  local service="$1"
  local state_file="$2"
  local ports="$3"
  read_captured_service_state "${state_file}" || return 1
  if [[ "${CAPTURED_ACTIVE_STATE}" == "active" ]]; then
    [[ -n "${ports}" ]] || return 1
    verify_profile_listeners "${service}" "${ports}" 1
  fi
}

rollback_protected_kharej_edit() {
  local number="$1"
  local service="$2"
  local env_snapshot="$3"
  local env_file="$4"
  local firewall_snapshot="$5"
  local service_state="$6"
  local old_ports="$7"
  local new_firewall="$8"
  local candidate_count="$9"
  local failed=0

  read_captured_service_state "${service_state}" || return 1
  restore_managed_snapshot "${env_snapshot}" "${env_file}" env || failed=1
  if ! stop_service_verified "${service}"; then
    return 1
  fi
  if [[ "${new_firewall}" == "1" ]]; then
    rollback_kharej_firewall_transition "${number}" "${candidate_count}" "${firewall_snapshot}" || failed=1
  else
    restore_kharej_firewall_rules "${number}" "${firewall_snapshot}" || failed=1
  fi
  [[ "${failed}" -eq 0 ]] || return 1
  restore_service_state "${service}" "${service_state}" || return 1
  verify_listener_for_captured_state "${service}" "${service_state}" "${old_ports}"
}

rollback_kharej_firewall_edit() {
  local number="$1"
  local env_snapshot="$2"
  local env_file="$3"
  local firewall_snapshot="$4"
  local new_firewall="$5"
  local candidate_count="$6"
  local failed=0
  restore_managed_snapshot "${env_snapshot}" "${env_file}" env || failed=1
  if [[ "${new_firewall}" == "1" ]]; then
    rollback_kharej_firewall_transition "${number}" "${candidate_count}" "${firewall_snapshot}" || failed=1
  else
    restore_kharej_firewall_rules "${number}" "${firewall_snapshot}" || failed=1
  fi
  [[ "${failed}" -eq 0 ]]
}

edit_profile() {
  require_root
  ensure_commands systemctl ss python3
  local side number service env_file profile_id old_label old_user old_password label user password
  local old_host old_port old_mappings host port mappings old_sources sources old_source_style old_firewall firewall
  local old_ports new_ports changed=0 credentials_changed=0 iran_runtime_changed=0 port_changed=0 sources_changed=0 firewall_state_changed=0
  local firewall_rules_changed=0 protected_port_migration=0 needs_runtime_restart=0 env_snapshot firewall_snapshot service_state
  local candidate_count=0 candidate_count_file prepare_status restore_failed migration_failed previous_active transition_prepared=0

  select_existing_tunnel
  side="${SELECTED_TUNNEL_SIDE}"
  number="${SELECTED_TUNNEL_NUMBER}"
  service="${SELECTED_TUNNEL_SERVICE}"
  env_file="${SELECTED_TUNNEL_ENV_FILE}"
  profile_id="${side}-${number}"
  [[ -f "${env_file}" && ! -L "${env_file}" ]] || die "selected profile has a missing or unsafe env file."
  profile_env_load "${env_file}" "${side}" || die "selected profile env is malformed; no values were displayed or changed."
  validate_loaded_profile "${side}"

  old_label="$(profile_env_value PROFILE_LABEL || true)"
  old_user="$(profile_env_value GOST_USER)"
  old_password="$(profile_env_value GOST_PASS)"
  old_ports="$(profile_local_ports_from_loaded "${side}")"

  label="$(prompt_default "Profile label (empty removes it)" "${old_label}")"
  validate_profile_label_or_die "${label}"
  read -r -p "New GOST username (blank retains current): " user
  user="${user:-${old_user}}"
  validate_token_or_die "GOST username" "${user}"
  password="$(prompt_secret_confirmed "New GOST password (blank retains current)" 1)"
  password="${password:-${old_password}}"
  validate_token_or_die "GOST password" "${password}"

  [[ "${label}" == "${old_label}" ]] || changed=1
  [[ "${user}" == "${old_user}" ]] || { changed=1; credentials_changed=1; }
  [[ "${password}" == "${old_password}" ]] || { changed=1; credentials_changed=1; }
  profile_env_set GOST_USER "${user}"
  profile_env_set GOST_PASS "${password}"
  if [[ -n "${label}" ]]; then profile_env_set PROFILE_LABEL "${label}"; else profile_env_unset PROFILE_LABEL; fi

  if [[ "${side}" == "iran" ]]; then
    old_host="$(profile_env_value KHAREJ_IP)"
    old_port="$(profile_env_value TUNNEL_PORT)"
    old_mappings="$(profile_env_value MAPPINGS)"
    host="$(prompt_default "Kharej host" "${old_host}")"
    port="$(prompt_default "Kharej SOCKS port" "${old_port}")"
    mappings="$(prompt_default "Port mappings" "${old_mappings}")"
    [[ "${host}" == "${old_host}" ]] || { changed=1; iran_runtime_changed=1; }
    [[ "${port}" == "${old_port}" ]] || { changed=1; iran_runtime_changed=1; }
    [[ "${mappings}" == "${old_mappings}" ]] || { changed=1; iran_runtime_changed=1; }
    profile_env_set KHAREJ_IP "${host}"
    profile_env_set TUNNEL_PORT "${port}"
    profile_env_set MAPPINGS "${mappings}"
  else
    old_port="$(profile_env_value TUNNEL_PORT)"
    old_sources="$(profile_sources_from_loaded)"
    if profile_env_has_key ALLOWED_IRAN_SOURCES; then old_source_style="ALLOWED_IRAN_SOURCES"; else old_source_style="IRAN_IP"; fi
    old_firewall="$(profile_env_value FIREWALL_ENABLED)"
    port="$(prompt_default "SOCKS listen port" "${old_port}")"
    sources="$(prompt_default "Allowed Iran IPv4/CIDRs" "${old_sources}")"
    sources="$(canonicalize_allowed_sources "${sources}")" || die "invalid allowed-source list."
    firewall="$(prompt_default "Firewall enabled (0 or 1)" "${old_firewall}")"
    if [[ "${sources}" != "${old_sources}" ]]; then
      profile_env_unset IRAN_IP
      profile_env_unset ALLOWED_IRAN_SOURCES
      profile_env_set ALLOWED_IRAN_SOURCES "${sources}"
      changed=1
      sources_changed=1
    elif [[ "${old_source_style}" == "IRAN_IP" ]]; then
      :
    fi
    [[ "${port}" == "${old_port}" ]] || { changed=1; port_changed=1; }
    [[ "${firewall}" == "${old_firewall}" ]] || { changed=1; firewall_state_changed=1; }
    profile_env_set TUNNEL_PORT "${port}"
    profile_env_set FIREWALL_ENABLED "${firewall}"
  fi

  if [[ "${changed}" -eq 0 ]]; then
    info "No changes detected; no file, backup, firewall rule, or service was touched."
    return 0
  fi

  validate_loaded_profile "${side}"
  new_ports="$(profile_local_ports_from_loaded "${side}")"
  validate_profile_ports_before_write "${profile_id}" "${new_ports}" "${profile_id}" "${old_ports}" || return 1
  if [[ "${side}" == "kharej" ]]; then
    if [[ "${sources_changed}" -eq 1 || "${firewall_state_changed}" -eq 1 || ( "${port_changed}" -eq 1 && ( "${old_firewall}" == "1" || "${firewall}" == "1" ) ) ]]; then
      firewall_rules_changed=1
    fi
    if [[ "${port_changed}" -eq 1 && ( "${old_firewall}" == "1" || "${firewall}" == "1" ) ]]; then
      protected_port_migration=1
    fi
    if [[ "${credentials_changed}" -eq 1 || "${port_changed}" -eq 1 ]]; then
      needs_runtime_restart=1
    fi
  elif [[ "${credentials_changed}" -eq 1 || "${iran_runtime_changed}" -eq 1 ]]; then
    needs_runtime_restart=1
  fi
  cat <<EOF_OUT
Redacted edit for ${profile_id}
Label: ${old_label:-${profile_id}} -> ${label:-${profile_id}}
Local ports: ${old_ports} -> ${new_ports}
Credentials: $(if [[ "${user}" == "${old_user}" && "${password}" == "${old_password}" ]]; then printf 'unchanged'; else printf 'changed (redacted)'; fi)
Connectivity fields: validated
EOF_OUT
  confirm "Save this exact profile change?" || { info "Edit cancelled; no files were changed."; return 0; }
  if [[ "${protected_port_migration}" -eq 1 ]]; then
    confirm "This protected Kharej port migration requires an exact service restart. Continue transactionally?" || {
      info "Protected port migration cancelled; env, firewall, and service were not changed."
      return 0
    }
  fi
  if [[ "${firewall_rules_changed}" -eq 1 ]]; then
    ensure_commands iptables
  fi

  env_snapshot="$(snapshot_managed_file "${env_file}")" || return 1
  firewall_snapshot="$(mktemp)"
  service_state="$(mktemp)"
  chmod 600 "${firewall_snapshot}"
  if [[ "${side}" == "kharej" ]]; then
    snapshot_kharej_firewall_rules "${number}" "${firewall_snapshot}" || { rm -f "${env_snapshot}" "${firewall_snapshot}" "${service_state}"; return 1; }
  fi
  if [[ "${needs_runtime_restart}" -eq 1 ]]; then
    capture_service_state "${service}" "${service_state}" || {
      rm -f "${env_snapshot}" "${firewall_snapshot}" "${service_state}"
      printf 'Could not capture exact service state; no profile mutation was made.\n' >&2
      return 1
    }
  fi

  if [[ "${side}" == "kharej" && "${firewall_rules_changed}" -eq 1 ]]; then
    transition_prepared=1
    if [[ "${firewall}" == "1" ]]; then
      candidate_count_file="$(mktemp)"
      if prepare_kharej_firewall_transition "${number}" "${port}" "${sources}" "${firewall}" > "${candidate_count_file}"; then
        candidate_count="$(cat "${candidate_count_file}")"
        rm -f "${candidate_count_file}"
      else
        prepare_status=$?
        rm -f "${candidate_count_file}" "${env_snapshot}" "${service_state}"
        if [[ "${prepare_status}" -eq 1 ]]; then
          rm -f "${firewall_snapshot}"
          printf 'Candidate firewall preparation failed; exact prior rules remain unchanged.\n' >&2
        else
          printf 'Candidate firewall preparation and rollback could not be proven. Retained firewall snapshot: %s\n' "${firewall_snapshot}" >&2
        fi
        return 1
      fi
    fi
  fi

  if ! write_loaded_profile_env "${env_file}" "${side}"; then
    restore_failed=0
    if [[ "${side}" == "kharej" && "${transition_prepared}" -eq 1 ]]; then
      rollback_kharej_firewall_edit "${number}" "${env_snapshot}" "${env_file}" "${firewall_snapshot}" "${firewall}" "${candidate_count}" || restore_failed=1
    fi
    if [[ "${restore_failed}" -eq 0 ]]; then
      rm -f "${env_snapshot}" "${firewall_snapshot}" "${service_state}"
    else
      printf 'Env write failed and firewall restoration could not be proven. Retained recovery snapshots: %s %s\n' "${env_snapshot}" "${firewall_snapshot}" >&2
      rm -f "${service_state}"
    fi
    return 1
  fi

  if [[ "${protected_port_migration}" -eq 1 ]]; then
    migration_failed=0
    read_captured_service_state "${service_state}" || migration_failed=1
    previous_active="${CAPTURED_ACTIVE_STATE:-unknown}"
    if [[ "${migration_failed}" -eq 0 && "${previous_active}" == "active" ]]; then
      systemctl restart "${service}" || migration_failed=1
      if [[ "${migration_failed}" -eq 0 ]]; then
        verify_active_profile_listener "${service}" "${new_ports}" || migration_failed=1
      fi
    elif [[ "${migration_failed}" -eq 0 && "${previous_active}" == "inactive" ]]; then
      verify_service_state_matches "${service}" "${service_state}" || migration_failed=1
    else
      migration_failed=1
    fi
    if [[ "${migration_failed}" -eq 0 ]]; then
      if [[ "${firewall}" == "1" ]]; then
        finalize_kharej_firewall_transition "${number}" "${port}" "${sources}" "${firewall}" "${candidate_count}" || migration_failed=1
      else
        delete_kharej_firewall_rules "${number}" || migration_failed=1
      fi
    fi
    if [[ "${migration_failed}" -ne 0 ]]; then
      if rollback_protected_kharej_edit "${number}" "${service}" "${env_snapshot}" "${env_file}" "${firewall_snapshot}" "${service_state}" "${old_ports}" "${firewall}" "${candidate_count}"; then
        rm -f "${env_snapshot}" "${firewall_snapshot}" "${service_state}"
        printf 'Protected port migration failed; exact prior env, firewall, service state, and listener were restored.\n' >&2
      else
        printf 'Protected port migration failed and restoration could not be proven. Retained recovery snapshots: %s %s %s\n' \
          "${env_snapshot}" "${firewall_snapshot}" "${service_state}" >&2
      fi
      return 1
    fi
    rm -f "${env_snapshot}" "${firewall_snapshot}" "${service_state}"
    info "Saved and transactionally migrated only ${profile_id}; obsolete old-port rules were removed after listener verification."
    return 0
  fi

  if [[ "${side}" == "kharej" && "${firewall_rules_changed}" -eq 1 ]]; then
    if [[ "${firewall}" == "1" ]]; then
      finalize_kharej_firewall_transition "${number}" "${port}" "${sources}" "${firewall}" "${candidate_count}" || migration_failed=1
    else
      delete_kharej_firewall_rules "${number}" || migration_failed=1
    fi
    if [[ "${migration_failed:-0}" -ne 0 ]]; then
      if rollback_kharej_firewall_edit "${number}" "${env_snapshot}" "${env_file}" "${firewall_snapshot}" "${firewall}" "${candidate_count}"; then
        rm -f "${env_snapshot}" "${firewall_snapshot}" "${service_state}"
        printf 'Edit failed while applying firewall changes; the prior profile was restored.\n' >&2
      else
        rm -f "${service_state}"
        printf 'Firewall edit and automatic restoration could not be proven. Retained recovery snapshots: %s %s\n' "${env_snapshot}" "${firewall_snapshot}" >&2
      fi
      return 1
    fi
  fi

  if [[ "${needs_runtime_restart}" -eq 0 ]]; then
    rm -f "${env_snapshot}" "${firewall_snapshot}" "${service_state}"
    info "Saved ${profile_id}; no GOST restart was required."
    return 0
  fi
  if ! confirm "Restart only ${service} now?"; then
    rm -f "${env_snapshot}" "${firewall_snapshot}" "${service_state}"
    info "Saved ${profile_id}; restart required for the running process to load it."
    return 0
  fi
  if systemctl restart "${service}" && verify_active_profile_listener "${service}" "${new_ports}"; then
    rm -f "${env_snapshot}" "${firewall_snapshot}" "${service_state}"
    info "Saved and restarted only ${profile_id}."
    return 0
  fi

  restore_failed=0
  if [[ "${side}" == "kharej" && "${firewall_rules_changed}" -eq 1 ]]; then
    rollback_protected_kharej_edit "${number}" "${service}" "${env_snapshot}" "${env_file}" "${firewall_snapshot}" "${service_state}" "${old_ports}" "${firewall}" "${candidate_count}" || restore_failed=1
  else
    restore_managed_snapshot "${env_snapshot}" "${env_file}" env || restore_failed=1
    stop_service_verified "${service}" || restore_failed=1
    if [[ "${restore_failed}" -eq 0 ]]; then
      restore_service_state "${service}" "${service_state}" || restore_failed=1
      verify_listener_for_captured_state "${service}" "${service_state}" "${old_ports}" || restore_failed=1
    fi
  fi
  if [[ "${restore_failed}" -eq 0 ]]; then
    rm -f "${env_snapshot}" "${firewall_snapshot}" "${service_state}"
    printf 'Restart verification failed; the prior env, firewall, and service state were restored.\n' >&2
  else
    printf 'Restart and automatic restoration could not be proven. Retained recovery snapshots: %s %s %s\n' \
      "${env_snapshot}" "${firewall_snapshot}" "${service_state}" >&2
  fi
  return 1
}

clone_profile() {
  require_root
  ensure_commands systemctl ss python3
  local source_side source_number source_id suggested number label user password start_profile
  local host port mappings sources firewall
  select_existing_tunnel
  source_side="${SELECTED_TUNNEL_SIDE}"
  source_number="${SELECTED_TUNNEL_NUMBER}"
  source_id="${source_side}-${source_number}"
  profile_env_load "${SELECTED_TUNNEL_ENV_FILE}" "${source_side}" || die "source profile env is malformed."
  validate_loaded_profile "${source_side}"
  user="$(profile_env_value GOST_USER)"
  password="$(profile_env_value GOST_PASS)"
  if ! confirm "Reuse the source credentials without displaying them?"; then
    user="$(prompt_required "New GOST username")"
    password="$(prompt_secret_confirmed "New GOST password")"
  fi
  validate_token_or_die "GOST username" "${user}"
  validate_token_or_die "GOST password" "${password}"
  suggested="$(next_free_profile_number "${source_side}")"
  number="$(prompt_default "New ${source_side} profile number" "${suggested}")"
  validate_tunnel_number_or_die "${number}"
  profile_identity_exists "${source_side}" "${number}" && die "profile ${source_side}-${number} already exists."
  label="$(prompt_default "New profile label (optional)" "")"
  validate_profile_label_or_die "${label}"

  if [[ "${source_side}" == "iran" ]]; then
    host="$(prompt_default "Kharej host" "$(profile_env_value KHAREJ_IP)")"
    port="$(prompt_default "Kharej SOCKS port" "$(profile_env_value TUNNEL_PORT)")"
    mappings="$(prompt_default "New unique local mappings" "$(profile_env_value MAPPINGS)")"
    profile_env_reset
    profile_env_set GOST_USER "${user}"
    profile_env_set GOST_PASS "${password}"
    profile_env_set KHAREJ_IP "${host}"
    profile_env_set TUNNEL_PORT "${port}"
    profile_env_set MAPPINGS "${mappings}"
  else
    sources="$(profile_sources_from_loaded)"
    firewall="$(profile_env_value FIREWALL_ENABLED)"
    port="$(prompt_default "New unique SOCKS listen port" "$(profile_env_value TUNNEL_PORT)")"
    sources="$(prompt_default "Allowed Iran IPv4/CIDRs" "${sources}")"
    sources="$(canonicalize_allowed_sources "${sources}")" || die "invalid allowed-source list."
    firewall="$(prompt_default "Firewall enabled (0 or 1)" "${firewall}")"
    if [[ "${firewall}" == "1" ]]; then
      if confirm "Apply firewall rules to the cloned profile?"; then
        ensure_commands iptables
      else
        firewall=0
      fi
    fi
    profile_env_reset
    profile_env_set GOST_USER "${user}"
    profile_env_set GOST_PASS "${password}"
    profile_env_set TUNNEL_PORT "${port}"
    profile_env_set ALLOWED_IRAN_SOURCES "${sources}"
    profile_env_set FIREWALL_ENABLED "${firewall}"
  fi
  [[ -z "${label}" ]] || profile_env_set PROFILE_LABEL "${label}"
  validate_loaded_profile "${source_side}"
  cat <<EOF_OUT
Clone source: ${source_id}
New profile: ${source_side}-${number}
Label: ${label:-${source_side}-${number}}
Local ports: $(profile_local_ports_from_loaded "${source_side}")
Credentials: configured (redacted)
EOF_OUT
  confirm "Create this clone?" || { info "Clone cancelled; source unchanged."; return 0; }
  if confirm "Start the cloned service now?"; then start_profile=1; else start_profile=0; fi
  install_new_profile_from_loaded "${source_side}" "${number}" "${start_profile}" || return 1
  info "Source ${source_id} was not changed."
}

restart_profile_selection() {
  local selection="$1"
  local strong="${2:-0}"
  local profiles side number service item identity seen failures=0
  local requested=()
  local selected=()
  profiles="$(mktemp)"
  discover_existing_tunnels "${profiles}"
  if [[ "${selection}" == "all" ]]; then
    while IFS='|' read -r side number service _unit _env; do
      [[ -n "${side}" ]] && requested+=("${side}-${number}")
    done < "${profiles}"
  else
    [[ "${selection}" != *[[:space:]]* && "${selection}" != ,* && "${selection}" != *, && "${selection}" != *,,* ]] || { rm -f "${profiles}"; printf 'Invalid profile selection.\n' >&2; return 1; }
    IFS=',' read -r -a requested <<< "${selection}"
  fi
  if [[ "${#requested[@]}" -eq 0 ]]; then
    rm -f "${profiles}"
    printf 'No managed profiles are available to restart.\n' >&2
    return 1
  fi
  seen="|"
  for item in "${requested[@]}"; do
    identity="$(profile_id_parts "${item}")" || { rm -f "${profiles}"; printf 'Invalid profile ID: %s\n' "${item}" >&2; return 1; }
    side="${identity%% *}"
    number="${identity#* }"
    profile_identity_exists "${side}" "${number}" || { rm -f "${profiles}"; printf 'Unknown profile ID: %s\n' "${item}" >&2; return 1; }
    if [[ "${seen}" != *"|${item}|"* ]]; then
      selected+=("${item}")
      seen="${seen}${item}|"
    fi
  done
  rm -f "${profiles}"
  [[ "${#selected[@]}" -gt 0 ]] || { printf 'No profiles selected.\n' >&2; return 1; }
  info "Selected exact services:"
  for item in "${selected[@]}"; do
    identity="$(profile_id_parts "${item}")"
    info "  $(service_name "${identity%% *}" "${identity#* }")"
  done
  if [[ "${strong}" == "1" ]]; then
    confirm "Restart ALL listed Direct Mode profiles?" || { info "Restart cancelled."; return 0; }
  else
    confirm "Restart only the listed profiles?" || { info "Restart cancelled."; return 0; }
  fi
  ensure_commands systemctl
  for item in "${selected[@]}"; do
    identity="$(profile_id_parts "${item}")"
    service="$(service_name "${identity%% *}" "${identity#* }")"
    if systemctl restart "${service}"; then
      info "Restarted ${item}."
    else
      printf 'Failed to restart %s.\n' "${item}" >&2
      failures=$((failures + 1))
    fi
  done
  [[ "${failures}" -eq 0 ]]
}

restart_selected_profiles() {
  local selection
  read -r -p "Profile IDs (comma-separated) or all: " selection
  restart_profile_selection "${selection}" 0
}

restart_all_profiles() {
  restart_profile_selection all 1
}

show_direct_profiles_menu() {
  cat <<'MENU'
Direct Mode profiles
====================

1) List all profiles
2) Show profile detail
3) Edit a profile
4) Clone a profile
5) Restart selected profiles
6) Restart all profiles
0) Back
MENU
}

direct_profiles_menu() {
  local choice
  list_profiles
  while true; do
    printf '\n'
    show_direct_profiles_menu
    read -r -p "Choose a profile action: " choice
    case "${choice}" in
      1) list_profiles ;;
      2) show_profile_detail ;;
      3) edit_profile ;;
      4) clone_profile ;;
      5) restart_selected_profiles ;;
      6) restart_all_profiles ;;
      0) return 0 ;;
      *) info "Invalid profile option." ;;
    esac
  done
}

list_active_gost_services() {
  direct_profiles_menu
}

collect_cleanup_candidates() {
  local tmp="$1"
  local file base side number env_file svc_file service state enabled
  : > "${tmp}"

  for file in "${SYSTEMD_DIR}"/gost-iran-*.service "${SYSTEMD_DIR}"/gost-kharej-*.service; do
    [[ -e "${file}" ]] || continue
    base="$(basename "${file}")"
    side="${base#gost-}"
    side="${side%-*.service}"
    number="${base%.service}"
    number="${number##*-}"
    env_file="$(env_path "${side}" "${number}")"
    service="$(service_name "${side}" "${number}")"
    if [[ ! -f "${env_file}" ]]; then
      printf 'service-with-missing-env|%s|%s\n' "${file}" "${env_file}" >> "${tmp}"
    fi
    state="$(systemctl is-failed "${service}" 2>/dev/null || true)"
    if [[ "${state}" == "failed" ]]; then
      printf 'failed-service|%s|%s\n' "${file}" "${env_file}" >> "${tmp}"
    fi
    enabled="$(systemctl is-enabled "${service}" 2>/dev/null || true)"
    state="$(systemctl is-active "${service}" 2>/dev/null || true)"
    if [[ "${enabled}" == "disabled" && "${state}" != "active" ]]; then
      printf 'disabled-orphan-service|%s|%s\n' "${file}" "${env_file}" >> "${tmp}"
    fi
  done

  for file in "${GOST_ETC_DIR}"/iran-*.env "${GOST_ETC_DIR}"/kharej-*.env; do
    [[ -e "${file}" ]] || continue
    base="$(basename "${file}")"
    side="${base%-*.env}"
    number="${base%.env}"
    number="${number##*-}"
    svc_file="$(service_path "${side}" "${number}")"
    if [[ ! -f "${svc_file}" ]]; then
      printf 'env-with-missing-service|%s|%s\n' "${file}" "${svc_file}" >> "${tmp}"
    fi
  done

  for file in "${GOST_ETC_DIR}"/*.bak.* "${SYSTEMD_DIR}"/gost-iran-*.service.bak.* "${SYSTEMD_DIR}"/gost-kharej-*.service.bak.*; do
    [[ -e "${file}" ]] || continue
    printf 'old-backup|%s|\n' "${file}" >> "${tmp}"
  done
}

clean_old_broken_configs() {
  require_root
  ensure_commands systemctl

  local tmp reason primary secondary
  tmp="$(mktemp)"
  collect_cleanup_candidates "${tmp}"
  if [[ ! -s "${tmp}" ]]; then
    info "No old or broken managed GOST configs found."
    rm -f "${tmp}"
    return 0
  fi

  info "Cleanup candidates:"
  while IFS='|' read -r reason primary secondary; do
    case "${reason}" in
      service-with-missing-env) printf '  service exists but env is missing: %s (missing %s)\n' "${primary}" "${secondary}" ;;
      failed-service) printf '  failed managed service: %s\n' "${primary}" ;;
      disabled-orphan-service) printf '  disabled orphan managed service: %s\n' "${primary}" ;;
      env-with-missing-service) printf '  env exists but service is missing: %s (missing %s)\n' "${primary}" "${secondary}" ;;
      old-backup) printf '  old backup file: %s\n' "${primary}" ;;
    esac
  done < "${tmp}"

  info "Only files matching managed patterns will be deleted."
  info "Unrelated services such as gost.service or gost-old.service are not touched."
  confirm "Delete these cleanup candidates?" || { rm -f "${tmp}"; die "cleanup aborted."; }

  while IFS='|' read -r reason primary secondary; do
    case "${reason}" in
      service-with-missing-env|failed-service|disabled-orphan-service)
        if [[ "${primary}" == "${SYSTEMD_DIR}"/gost-iran-*.service || "${primary}" == "${SYSTEMD_DIR}"/gost-kharej-*.service ]]; then
          rm -f "${primary}"
        fi
        if [[ -n "${secondary}" && -e "${secondary}" && ( "${secondary}" == "${GOST_ETC_DIR}"/iran-*.env || "${secondary}" == "${GOST_ETC_DIR}"/kharej-*.env ) ]]; then
          rm -f "${secondary}"
        fi
        ;;
      env-with-missing-service|old-backup)
        if [[ "${primary}" == "${GOST_ETC_DIR}"/iran-*.env || "${primary}" == "${GOST_ETC_DIR}"/kharej-*.env || "${primary}" == "${GOST_ETC_DIR}"/*.bak.* || "${primary}" == "${SYSTEMD_DIR}"/gost-iran-*.service.bak.* || "${primary}" == "${SYSTEMD_DIR}"/gost-kharej-*.service.bak.* ]]; then
          rm -f "${primary}"
        fi
        ;;
    esac
  done < "${tmp}"

  systemctl daemon-reload
  systemctl reset-failed
  rm -f "${tmp}"
  info "Cleanup complete."
}

render_stability_sysctl_config() {
  cat <<EOF_OUT
${STABILITY_MANAGED_MARKER}
fs.file-max = 2097152

net.core.somaxconn = 65535
net.core.netdev_max_backlog = 250000

net.ipv4.ip_local_port_range = 10000 65000

net.ipv4.tcp_max_syn_backlog = 65535

net.ipv4.tcp_fin_timeout = 15

net.ipv4.tcp_keepalive_time = 60
net.ipv4.tcp_keepalive_intvl = 10
net.ipv4.tcp_keepalive_probes = 6

net.ipv4.tcp_slow_start_after_idle = 0
EOF_OUT
}

render_stability_systemd_override() {
  cat <<EOF_OUT
${STABILITY_MANAGED_MARKER}
[Service]
LimitNOFILE=1048576
TasksMax=infinity
OOMScoreAdjust=-500
Restart=always
RestartSec=3
EOF_OUT
}

stability_service_name_is_managed() {
  local service="$1"
  parse_tunnel_service_name "${service}" >/dev/null 2>&1
}

stability_override_path() {
  local service="$1"
  stability_service_name_is_managed "${service}" || return 1
  printf '%s/%s.d/stability.conf\n' "${SYSTEMD_DIR}" "${service}"
}

normalize_stability_value() {
  local raw="$1"
  local fields=()
  read -r -a fields <<< "${raw}"
  [[ "${#fields[@]}" -gt 0 ]] || return 1
  local IFS=' '
  printf '%s\n' "${fields[*]}"
}

read_stability_sysctl_value() {
  local key="$1"
  local raw
  raw="$(sysctl -n "${key}" 2>/dev/null)" || return 1
  normalize_stability_value "${raw}"
}

stability_file_matches() {
  local path="$1"
  local renderer="$2"
  [[ -f "${path}" && ! -L "${path}" ]] || return 1
  cmp -s "${path}" <("${renderer}")
}

stability_file_is_managed() {
  local path="$1"
  [[ -f "${path}" && ! -L "${path}" ]] || return 1
  IFS= read -r first_line < "${path}" || return 1
  [[ "${first_line}" == "${STABILITY_MANAGED_MARKER}" ]]
}

backup_stability_file() {
  local path="$1"
  local backup
  backup="$(mktemp "${path}.bak.XXXXXX")" || return 1
  if ! cp -p "${path}" "${backup}"; then
    rm -f "${backup}"
    return 1
  fi
  fsync_file_or_directory "${backup}"
  info "Managed stability backup: ${backup}"
}

write_stability_file() {
  local path="$1"
  local mode="$2"
  local renderer="$3"
  local directory base tmp
  directory="$(dirname "${path}")"
  base="$(basename "${path}")"
  tmp="$(mktemp "${directory}/.${base}.tmp.XXXXXX")" || return 1
  if ! chmod "${mode}" "${tmp}" ||
     ! set_production_owner "${tmp}" ||
     ! "${renderer}" > "${tmp}" ||
     ! chmod "${mode}" "${tmp}"; then
    rm -f "${tmp}"
    return 1
  fi
  fsync_file_or_directory "${tmp}"
  if ! mv -f "${tmp}" "${path}"; then
    rm -f "${tmp}"
    return 1
  fi
  fsync_file_or_directory "${directory}"
}

validate_stability_sysctl_destination() {
  local path="$1"
  local directory
  directory="$(dirname "${path}")"
  [[ "${path}" == "${STABILITY_SYSCTL_FILE}" ]] || return 1
  [[ -d "${directory}" && ! -L "${directory}" ]] || return 1
  [[ ! -L "${path}" ]] || return 1
  [[ ! -e "${path}" || -f "${path}" ]] || return 1
}

prepare_stability_sysctl_file() {
  local path="${STABILITY_SYSCTL_FILE}"
  STABILITY_SYSCTL_CHANGED=0
  if ! validate_stability_sysctl_destination "${path}"; then
    STABILITY_SYSCTL_FILE_RESULT="Failed: unsafe path or symlink"
    return 1
  fi
  if stability_file_matches "${path}" render_stability_sysctl_config; then
    STABILITY_SYSCTL_FILE_RESULT="Already optimized"
    return 0
  fi
  if [[ -e "${path}" ]]; then
    if ! stability_file_is_managed "${path}"; then
      STABILITY_SYSCTL_FILE_RESULT="Failed: existing file is not managed by GOST Manager"
      return 1
    fi
    if ! backup_stability_file "${path}"; then
      STABILITY_SYSCTL_FILE_RESULT="Failed: managed file backup could not be created"
      return 1
    fi
    STABILITY_SYSCTL_FILE_RESULT="Updated"
  else
    STABILITY_SYSCTL_FILE_RESULT="Applied"
  fi
  if ! write_stability_file "${path}" 644 render_stability_sysctl_config ||
     ! stability_file_matches "${path}" render_stability_sysctl_config; then
    STABILITY_SYSCTL_FILE_RESULT="Failed: atomic write verification failed"
    return 1
  fi
  STABILITY_SYSCTL_CHANGED=1
}

discover_stability_services() {
  local output="$1"
  local unit service
  : > "${output}"
  for unit in "${SYSTEMD_DIR}"/gost-iran-*.service "${SYSTEMD_DIR}"/gost-kharej-*.service; do
    [[ -e "${unit}" || -L "${unit}" ]] || continue
    service="${unit##*/}"
    stability_service_name_is_managed "${service}" || continue
    printf '%s\n' "${service}" >> "${output}"
  done
  LC_ALL=C sort -u -o "${output}" "${output}"
}

detect_stability_services() {
  local inventory service
  STABILITY_SERVICES=()
  inventory="$(mktemp)"
  discover_stability_services "${inventory}"
  while IFS= read -r service; do
    [[ -n "${service}" ]] && STABILITY_SERVICES+=("${service}")
  done < "${inventory}"
  rm -f "${inventory}"
}

validate_stability_override_destination() {
  local service="$1"
  local path="$2"
  local unit dropin_dir expected
  stability_service_name_is_managed "${service}" || return 1
  unit="${SYSTEMD_DIR}/${service}"
  expected="$(stability_override_path "${service}")" || return 1
  dropin_dir="$(dirname "${expected}")"
  [[ "${path}" == "${expected}" ]] || return 1
  [[ -d "${SYSTEMD_DIR}" && ! -L "${SYSTEMD_DIR}" ]] || return 1
  [[ -f "${unit}" && ! -L "${unit}" ]] || return 1
  [[ ! -L "${dropin_dir}" ]] || return 1
  [[ ! -e "${dropin_dir}" || -d "${dropin_dir}" ]] || return 1
  [[ ! -L "${path}" ]] || return 1
  [[ ! -e "${path}" || -f "${path}" ]] || return 1
}

prepare_stability_override() {
  local service="$1"
  local path dropin_dir
  STABILITY_SYSCTL_CHANGED=0
  path="$(stability_override_path "${service}")" || return 1
  dropin_dir="$(dirname "${path}")"
  if ! validate_stability_override_destination "${service}" "${path}"; then
    return 1
  fi
  if [[ ! -d "${dropin_dir}" ]]; then
    if ! mkdir "${dropin_dir}" ||
       ! chmod 755 "${dropin_dir}" ||
       ! set_production_owner "${dropin_dir}"; then
      return 1
    fi
    fsync_file_or_directory "${SYSTEMD_DIR}"
  fi
  [[ -d "${dropin_dir}" && ! -L "${dropin_dir}" ]] || return 1
  if stability_file_matches "${path}" render_stability_systemd_override; then
    STABILITY_SYSCTL_CHANGED=0
    return 0
  fi
  if [[ -e "${path}" ]]; then
    stability_file_is_managed "${path}" || return 1
    backup_stability_file "${path}" || return 1
  fi
  if ! write_stability_file "${path}" 644 render_stability_systemd_override ||
     ! stability_file_matches "${path}" render_stability_systemd_override; then
    return 1
  fi
  STABILITY_SYSCTL_CHANGED=1
}

reset_stability_state() {
  STABILITY_CURRENT_VALUES=()
  STABILITY_FINAL_VALUES=()
  STABILITY_KERNEL_RESULTS=()
  STABILITY_SERVICES=()
  STABILITY_SERVICE_RESULTS=()
  STABILITY_RESTART_REQUIRED=()
  STABILITY_SYSCTL_FILE_RESULT=""
  STABILITY_SYSCTL_CHANGED=0
  STABILITY_SYSCTL_APPLY_COUNT=0
  STABILITY_DAEMON_RELOAD_COUNT=0
  STABILITY_OPTIMIZED_COUNT=0
  STABILITY_FAILURE_COUNT=0
}

capture_stability_kernel_state() {
  local index key value
  for ((index = 0; index < ${#STABILITY_SYSCTL_KEYS[@]}; index++)); do
    key="${STABILITY_SYSCTL_KEYS[${index}]}"
    if value="$(read_stability_sysctl_value "${key}")"; then
      STABILITY_CURRENT_VALUES[index]="${value}"
    else
      STABILITY_CURRENT_VALUES[index]="unavailable"
    fi
  done
}

stability_kernel_needs_apply() {
  local index
  for ((index = 0; index < ${#STABILITY_SYSCTL_KEYS[@]}; index++)); do
    if [[ "${STABILITY_CURRENT_VALUES[${index}]}" != "${STABILITY_SYSCTL_VALUES[${index}]}" ]]; then
      return 0
    fi
  done
  return 1
}

apply_stability_kernel_settings() {
  local config_ready=0 apply_needed=0 index key value verified=1
  if prepare_stability_sysctl_file; then
    config_ready=1
  else
    STABILITY_FAILURE_COUNT=$((STABILITY_FAILURE_COUNT + 1))
  fi
  if [[ "${config_ready}" -eq 1 ]]; then
    if [[ "${STABILITY_SYSCTL_CHANGED}" -eq 1 ]] || stability_kernel_needs_apply; then
      apply_needed=1
    fi
    if [[ "${apply_needed}" -eq 1 ]]; then
      STABILITY_SYSCTL_APPLY_COUNT=$((STABILITY_SYSCTL_APPLY_COUNT + 1))
      if ! sysctl --system >/dev/null; then
        STABILITY_FAILURE_COUNT=$((STABILITY_FAILURE_COUNT + 1))
      fi
    fi
  fi
  for ((index = 0; index < ${#STABILITY_SYSCTL_KEYS[@]}; index++)); do
    key="${STABILITY_SYSCTL_KEYS[${index}]}"
    if value="$(read_stability_sysctl_value "${key}")"; then
      STABILITY_FINAL_VALUES[index]="${value}"
    else
      STABILITY_FINAL_VALUES[index]="unavailable"
    fi
    if [[ "${STABILITY_FINAL_VALUES[${index}]}" == "${STABILITY_SYSCTL_VALUES[${index}]}" ]]; then
      STABILITY_KERNEL_RESULTS[index]="OK"
    else
      STABILITY_KERNEL_RESULTS[index]="MISSING"
      verified=0
    fi
  done
  if [[ "${verified}" -eq 0 ]]; then
    STABILITY_FAILURE_COUNT=$((STABILITY_FAILURE_COUNT + 1))
  fi
}

apply_stability_service_overrides() {
  local service changed_count=0
  STABILITY_SERVICE_RESULTS=()
  if [[ "${#STABILITY_SERVICES[@]}" -eq 0 ]]; then
    return 0
  fi
  for service in "${STABILITY_SERVICES[@]}"; do
    STABILITY_SYSCTL_CHANGED=0
    if prepare_stability_override "${service}"; then
      STABILITY_OPTIMIZED_COUNT=$((STABILITY_OPTIMIZED_COUNT + 1))
      if [[ "${STABILITY_SYSCTL_CHANGED}" -eq 1 ]]; then
        STABILITY_SERVICE_RESULTS+=("Applied")
        STABILITY_RESTART_REQUIRED+=("${service}")
        changed_count=$((changed_count + 1))
      else
        STABILITY_SERVICE_RESULTS+=("Already optimized")
      fi
    else
      STABILITY_SERVICE_RESULTS+=("Failed: unsafe or unmanaged override path")
      STABILITY_FAILURE_COUNT=$((STABILITY_FAILURE_COUNT + 1))
    fi
  done
  if [[ "${changed_count}" -gt 0 ]]; then
    STABILITY_DAEMON_RELOAD_COUNT=$((STABILITY_DAEMON_RELOAD_COUNT + 1))
    if ! systemctl daemon-reload; then
      STABILITY_FAILURE_COUNT=$((STABILITY_FAILURE_COUNT + 1))
    fi
  fi
}

print_stability_report() {
  local index key service result
  printf '\nServer Stability Report\n'
  printf '=======================\n\n'
  printf 'Kernel configuration: %s\n\n' "${STABILITY_SYSCTL_FILE_RESULT}"
  for ((index = 0; index < ${#STABILITY_SYSCTL_KEYS[@]}; index++)); do
    key="${STABILITY_SYSCTL_KEYS[${index}]}"
    printf '%s:\n' "${key}"
    printf '  Current: %s\n' "${STABILITY_CURRENT_VALUES[${index}]}"
    printf '  Recommended: %s\n' "${STABILITY_SYSCTL_VALUES[${index}]}"
    printf '  Final: %s\n' "${STABILITY_FINAL_VALUES[${index}]}"
    printf '  Result: %s\n\n' "${STABILITY_KERNEL_RESULTS[${index}]}"
  done
  printf 'GOST Services:\n\n'
  if [[ "${#STABILITY_SERVICES[@]}" -eq 0 ]]; then
    printf 'No managed GOST services detected.\n\n'
  else
    for ((index = 0; index < ${#STABILITY_SERVICES[@]}; index++)); do
      service="${STABILITY_SERVICES[${index}]}"
      result="${STABILITY_SERVICE_RESULTS[${index}]}"
      printf '%s\n' "${service}"
      printf '  Override: %s\n' "${result}"
      if [[ "${result}" != Failed:* ]]; then
        printf '  [OK] LimitNOFILE 1048576\n'
        printf '  [OK] TasksMax infinity\n'
        printf '  [OK] Restart always\n'
        printf '  [OK] RestartSec 3\n'
        printf '  [OK] OOMScoreAdjust -500\n'
      fi
      printf '\n'
    done
  fi
  printf 'Restart required:\n\n'
  if [[ "${#STABILITY_RESTART_REQUIRED[@]}" -eq 0 ]]; then
    printf 'None from this run.\n\n'
  else
    for service in "${STABILITY_RESTART_REQUIRED[@]}"; do
      printf '%s\n' "${service%.service}"
    done
    printf '\nReason:\nNew systemd limits apply after the next service restart.\n\n'
  fi
  if [[ "${STABILITY_FAILURE_COUNT}" -gt 0 ]]; then
    printf 'Some stability settings could not be installed.\n'
  elif [[ "${STABILITY_SYSCTL_FILE_RESULT}" == "Applied" ||
          "${STABILITY_SYSCTL_FILE_RESULT}" == "Updated" ||
          "${#STABILITY_RESTART_REQUIRED[@]}" -gt 0 ]]; then
    printf 'Changes were installed.\n'
  else
    printf 'Server stability settings are already optimized.\n'
  fi
  printf 'Existing GOST services were not restarted.\n'
  printf 'New limits apply after next service restart.\n\n'
  printf '[OK] No GOST service restarted\n'
  printf '[OK] Existing connections were not interrupted\n'
  printf 'Services optimized: %s\n' "${STABILITY_OPTIMIZED_COUNT}"
  printf 'Restart count: 0\n'
  printf 'Daemon reload count: %s\n' "${STABILITY_DAEMON_RELOAD_COUNT}"
  if [[ "${STABILITY_FAILURE_COUNT}" -gt 0 ]]; then
    printf 'Result: completed with %s failure(s)\n' "${STABILITY_FAILURE_COUNT}"
  else
    printf 'Result: successful\n'
  fi
}

run_server_stability() {
  local index
  reset_stability_state
  printf '=================================\n'
  printf '       Server Stability\n'
  printf '=================================\n\n'
  printf 'Checking system...\n\n'
  capture_stability_kernel_state
  detect_stability_services
  printf '[OK] Kernel parameters checked\n'
  printf '[OK] GOST services detected: %s\n' "${#STABILITY_SERVICES[@]}"
  printf '[OK] Current limits checked\n\n'
  printf 'Current kernel values:\n'
  for ((index = 0; index < ${#STABILITY_SYSCTL_KEYS[@]}; index++)); do
    printf '  %s = %s\n' "${STABILITY_SYSCTL_KEYS[${index}]}" "${STABILITY_CURRENT_VALUES[${index}]}"
  done
  printf '\nApplying stability settings...\n\n'
  apply_stability_kernel_settings
  apply_stability_service_overrides
  if [[ "${STABILITY_SYSCTL_FILE_RESULT}" == Failed:* ]]; then
    printf '[FAILED] Kernel tuning was not installed safely\n'
  elif [[ "${STABILITY_SYSCTL_FILE_RESULT}" == "Already optimized" ]]; then
    printf '[OK] Kernel tuning already optimized\n'
  else
    printf '[OK] Kernel tuning processed\n'
  fi
  printf '[OK] GOST service limits checked: %s/%s optimized\n' \
    "${STABILITY_OPTIMIZED_COUNT}" "${#STABILITY_SERVICES[@]}"
  if [[ "${STABILITY_FAILURE_COUNT}" -gt 0 ]]; then
    printf '[FAILED] Verification found incomplete settings\n'
  else
    printf '[OK] Verification completed\n'
  fi
  print_stability_report
  [[ "${STABILITY_FAILURE_COUNT}" -eq 0 ]]
}

server_stability_wizard() {
  local status=0
  require_root
  ensure_commands sysctl systemctl cmp
  if run_server_stability; then
    status=0
  else
    status=$?
  fi
  if [[ "${status}" -ne 0 ]]; then
    info "Server Stability completed with failures. Review the report above."
  fi
  if ! read -r -p "Press Enter to return..."; then
    :
  fi
  return 0
}

execute_monitor_command() {
  local status
  if "$@"; then
    return 0
  else
    status=$?
  fi
  if [[ "${status}" -eq 130 ]]; then
    info "Monitoring view closed."
  else
    info "Monitoring command failed with exit code ${status}."
  fi
  return "${status}"
}

run_monitor_command() {
  execute_monitor_command "$@" || true
}

monitor_query() {
  local -a command=("${MONITOR_BIN}" --config "${MONITOR_CONFIG}" "$@")
  run_monitor_command "${command[@]}"
}

monitor_admin() {
  local -a command=("${MONITOR_ADMIN_BIN}" "$@" --config "${MONITOR_CONFIG}")
  run_monitor_command "${command[@]}"
}

monitor_config_value() {
  local field="$1"
  local value
  if ! value="$("${MONITOR_ADMIN_BIN}" config --format value --field "${field}" --config "${MONITOR_CONFIG}")"; then
    info "Monitoring config could not be resolved safely; no database action was taken." >&2
    return 1
  fi
  if [[ "${field}" == "database_path" && "${value}" != /* ]]; then
    info "Monitoring config returned an unsafe database path; no database action was taken." >&2
    return 1
  fi
  printf '%s\n' "${value}"
}

monitor_custom_summary() {
  local duration
  read -r -p "Custom duration (for example 90s, 15m, 2h, 24h): " duration
  if [[ ! "${duration}" =~ ^[1-9][0-9]*(s|m|h|d)$ ]]; then
    info "Invalid duration."
    return 0
  fi
  monitor_query summary --window "${duration}"
}

monitor_service_detail() {
  local service
  monitor_query services --window 10m
  read -r -p "Exact service name (empty to return): " service
  [[ -n "${service}" ]] || return 0
  if [[ ! "${service}" =~ ^gost-(iran|kharej)-[1-9][0-9]*\.service$ ]]; then
    info "Invalid managed service name."
    return 0
  fi
  monitor_query service "${service}" --window 30m
}

monitor_tunnel_detail() {
  local tunnel
  monitor_query tunnels --window 10m
  read -r -p "Exact tunnel ID (empty to return): " tunnel
  [[ -n "${tunnel}" ]] || return 0
  if [[ ! "${tunnel}" =~ ^(iran|kharej)-[1-9][0-9]*$ ]]; then
    info "Invalid managed tunnel ID."
    return 0
  fi
  monitor_query tunnel "${tunnel}" --window 1h
}

monitor_recent_events() {
  local window severity
  window="$(prompt_default "Event window" "1h")"
  read -r -p "Severity filter (comma-separated, empty for all): " severity
  if [[ -n "${severity}" ]]; then
    monitor_query events --window "${window}" --severity "${severity}"
  else
    monitor_query events --window "${window}"
  fi
}

monitor_export() {
  local format="$1"
  local window output extension timestamp
  window="$(prompt_default "Export window" "1h")"
  extension="${format}"
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  output="$(prompt_default "Output path" "${MONITOR_EXPORT_DIR}/gost-monitor-export-${timestamp}.${extension}")"
  if [[ -e "${output}" ]] && ! confirm "Replace existing export ${output}?"; then
    info "Export cancelled."
    return 0
  fi
  monitor_query export --window "${window}" --format "${format}" --granularity auto --output "${output}"
}

monitoring_service_status() {
  local database_path
  database_path="$(monitor_config_value database_path)" || return 0
  info "Collector service: ${MONITOR_SERVICE}"
  info "Config: ${MONITOR_CONFIG}"
  info "History: ${database_path}"
  systemctl --no-pager status "${MONITOR_SERVICE}" || true
  if systemctl is-enabled --quiet "${MONITOR_SERVICE}"; then
    info "Enabled: yes"
  else
    info "Enabled: no"
  fi
  if systemctl is-active --quiet "${MONITOR_SERVICE}"; then
    info "Active: yes"
  else
    info "Active: no"
  fi
  monitor_admin status
}

monitoring_service_action() {
  local action="$1"
  require_root
  case "${action}" in
    start) ;;
    stop|restart)
      confirm "${action^} monitoring collector only?" || { info "Action cancelled."; return 0; }
      ;;
    *) info "Unsupported collector action."; return 0 ;;
  esac
  if systemctl "${action}" "${MONITOR_SERVICE}"; then
    info "Monitoring collector ${action} completed."
  else
    info "Monitoring collector ${action} failed."
  fi
}

monitor_one_shot() {
  require_root
  local database_path was_active=0 status=0
  database_path="$(monitor_config_value database_path)" || return 0
  info "One-shot database: ${database_path}"
  if systemctl is-active --quiet "${MONITOR_SERVICE}"; then
    confirm "Temporarily stop the monitoring collector for one-shot diagnostics?" || {
      info "One-shot diagnostic cancelled."
      return 0
    }
    if ! systemctl stop "${MONITOR_SERVICE}"; then
      info "Could not stop monitoring collector; one-shot diagnostic was not run."
      return 0
    fi
    was_active=1
  fi
  local -a command=("${MONITOR_COLLECTOR_BIN}" --once)
  if execute_monitor_command "${command[@]}"; then
    status=0
  else
    status=$?
  fi
  if [[ "${was_active}" -eq 1 ]]; then
    systemctl start "${MONITOR_SERVICE}" || info "One-shot finished, but collector restart failed."
  fi
  [[ "${status}" -eq 0 ]] || info "One-shot diagnostic did not complete successfully."
  return 0
}

monitor_maintenance() {
  require_root
  monitor_config_value database_path >/dev/null || return 0
  monitor_admin maintenance
}

monitor_purge_history() {
  local phrase was_active=0 status database_path
  require_root
  database_path="$(monitor_config_value database_path)" || return 0
  info "This deletes only monitoring history: ${database_path}"
  info "Traffic services and ${GOST_ETC_DIR} are not affected."
  read -r -p "Type DELETE MONITORING HISTORY to continue: " phrase
  if [[ "${phrase}" != "DELETE MONITORING HISTORY" ]]; then
    info "History deletion cancelled."
    return 0
  fi
  if systemctl is-active --quiet "${MONITOR_SERVICE}"; then
    was_active=1
    if ! systemctl stop "${MONITOR_SERVICE}"; then
      info "Could not stop monitoring collector; history was not changed."
      return 0
    fi
  fi
  local -a command=("${MONITOR_ADMIN_BIN}" purge-history --yes --config "${MONITOR_CONFIG}")
  if execute_monitor_command "${command[@]}"; then
    status=0
  else
    status=$?
  fi
  if [[ "${was_active}" -eq 1 ]]; then
    systemctl start "${MONITOR_SERVICE}" || info "Monitoring history changed, but collector restart failed."
  fi
  [[ "${status}" -eq 0 ]] && info "Monitoring history deleted safely."
  return 0
}

show_monitoring_menu() {
  cat <<'MENU'
Monitoring
==========

1) Live resources
2) Last 10 minutes
3) Last 30 minutes
4) Last 1 hour
5) Services and tunnels
6) Collector status
7) Advanced tools
0) Back
MENU
}

show_services_and_tunnels_menu() {
  cat <<'MENU'
Services and tunnels
====================

1) Service list
2) Service detail
3) Tunnel list
4) Tunnel detail
0) Back
MENU
}

services_and_tunnels_menu() {
  local choice
  while true; do
    show_services_and_tunnels_menu
    read -r -p "Choose a service or tunnel option: " choice
    case "${choice}" in
      1) monitor_query services --window 10m ;;
      2) monitor_service_detail ;;
      3) monitor_query tunnels --window 10m ;;
      4) monitor_tunnel_detail ;;
      0) return 0 ;;
      *) info "Invalid service or tunnel option." ;;
    esac
    printf '\n'
  done
}

show_monitoring_advanced_menu() {
  cat <<'MENU'
Advanced tools
==============

1) Plain current snapshot
2) Host detail
3) Network detail
4) Collector/database detail
5) Recent events
6) Custom time-window summary
7) Export JSON
8) Export CSV
9) Run one-shot collector diagnostic
10) Run maintenance now
11) Delete monitoring history
12) Start collector
13) Stop collector
14) Restart collector
0) Back
MENU
}

monitoring_advanced_menu() {
  local choice
  while true; do
    show_monitoring_advanced_menu
    read -r -p "Choose an advanced monitoring option: " choice
    case "${choice}" in
      1) monitor_query snapshot ;;
      2) monitor_query host --window 30m ;;
      3) monitor_query network --window 30m ;;
      4) monitor_query collector --window 1h ;;
      5) monitor_recent_events ;;
      6) monitor_custom_summary ;;
      7) monitor_export json ;;
      8) monitor_export csv ;;
      9) monitor_one_shot ;;
      10) monitor_maintenance ;;
      11) monitor_purge_history ;;
      12) monitoring_service_action start ;;
      13) monitoring_service_action stop ;;
      14) monitoring_service_action restart ;;
      0) return 0 ;;
      *) info "Invalid advanced monitoring option." ;;
    esac
    printf '\n'
  done
}

monitoring_menu() {
  local choice
  while true; do
    show_monitoring_menu
    read -r -p "Choose a monitoring option: " choice
    case "${choice}" in
      1) monitor_query live ;;
      2) monitor_query summary --window 10m ;;
      3) monitor_query summary --window 30m ;;
      4) monitor_query summary --window 1h ;;
      5) services_and_tunnels_menu ;;
      6) monitoring_service_status ;;
      7) monitoring_advanced_menu ;;
      0) return 0 ;;
      *) info "Invalid monitoring option." ;;
    esac
    printf '\n'
  done
}

run_watchdog_command() {
  local status
  if "${WATCHDOG_ADMIN_BIN}" "$@"; then
    return 0
  else
    status=$?
  fi
  info "Watchdog command failed safely with exit code ${status}."
  return 0
}

watchdog_select_profile() {
  local profile_id
  run_watchdog_command profiles
  read -r -p "Iran profile ID (iran-N, empty to return): " profile_id
  [[ -n "${profile_id}" ]] || return 1
  if [[ ! "${profile_id}" =~ ^iran-[1-9][0-9]*$ ]]; then
    info "Invalid Iran profile ID."
    return 1
  fi
  WATCHDOG_SELECTED_PROFILE="${profile_id}"
}

watchdog_effective_values() {
  local profile_id="$1"
  local payload parsed
  if ! payload="$("${WATCHDOG_ADMIN_BIN}" effective "${profile_id}" --json)"; then
    info "Unable to read current Watchdog profile values."
    return 1
  fi
  if ! parsed="$(printf '%s' "${payload}" | python3 -c '
import json, sys
try:
    value = json.load(sys.stdin)
    keys = ("check_interval_seconds", "ping_timeout_seconds", "failure_threshold", "success_threshold", "recovery_hold_seconds", "recovery_jitter_max_seconds")
    mode = value["mode"]
    items = [value[key] for key in keys]
    if mode not in ("disabled", "monitor", "auto") or any(type(item) is not int for item in items):
        raise ValueError
    print("\t".join([mode] + [str(item) for item in items]))
except (KeyError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
')"; then
    info "Watchdog returned invalid profile JSON; no configuration was changed."
    return 1
  fi
  printf '%s\n' "${parsed}"
}

watchdog_global_values() {
  local payload parsed
  if ! payload="$("${WATCHDOG_ADMIN_BIN}" effective-global --json)"; then
    info "Unable to read current global Watchdog values."
    return 1
  fi
  if ! parsed="$(printf '%s' "${payload}" | python3 -c '
import json, sys
try:
    value = json.load(sys.stdin)
    keys = ("check_interval_seconds", "ping_timeout_seconds", "failure_threshold", "success_threshold", "recovery_hold_seconds", "recovery_jitter_max_seconds")
    items = [value[key] for key in keys]
    if value.get("check_mode") != "ping" or any(type(item) is not int for item in items):
        raise ValueError
    print("\t".join(str(item) for item in items))
except (KeyError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
')"; then
    info "Watchdog returned invalid global JSON; no configuration was changed."
    return 1
  fi
  printf '%s\n' "${parsed}"
}

watchdog_mode_state() {
  local profile_id="$1"
  local payload parsed
  if ! payload="$("${WATCHDOG_ADMIN_BIN}" status --profile "${profile_id}" --json)"; then
    info "Unable to read current Watchdog ownership state."
    return 1
  fi
  if ! parsed="$(printf '%s' "${payload}" | python3 -c '
import json, sys
try:
    value = json.load(sys.stdin)
    profiles = value["profiles"]
    if not isinstance(profiles, list) or len(profiles) != 1:
        raise ValueError
    item = profiles[0]
    mode = item["mode"]
    owned = item["stopped_by_watchdog"]
    health = item["watchdog_state"]
    check = item["check_status"]
    if mode not in ("disabled", "monitor", "auto") or type(owned) is not bool or not isinstance(health, str) or check not in ("unknown", "success", "unreachable", "probe_error"):
        raise ValueError
    if check != "success":
        health = check
    print("\t".join((mode, "true" if owned else "false", health)))
except (KeyError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
')"; then
    info "Watchdog returned invalid status JSON; no mode was changed."
    return 1
  fi
  printf '%s\n' "${parsed}"
}

watchdog_apply_mode_change() {
  local profile_id="$1"
  local mode="$2"
  local current_mode owned health parsed choice
  local -a arguments=(set-mode "${profile_id}" "${mode}")
  parsed="$(watchdog_mode_state "${profile_id}")" || return 0
  IFS=$'\t' read -r current_mode owned health <<< "${parsed}"
  if [[ "${current_mode}" == "auto" && "${mode}" != "auto" && "${owned}" == "true" ]]; then
    info "${profile_id} is currently stopped by Watchdog."
    info "Leaving Auto mode also disables automatic recovery."
    cat <<'MENU'
1) Change mode and keep the service stopped
2) Start now if upstream is healthy, then change mode
3) Cancel
MENU
    read -r -p "Choose how to handle the stopped service: " choice
    case "${choice}" in
      1) arguments+=(--owned-action keep-stopped) ;;
      2)
        if [[ "${health}" != "healthy" ]]; then
          info "Upstream is not healthy; the service was not started and mode was not changed."
          return 0
        fi
        arguments+=(--owned-action start-if-healthy)
        ;;
      3) info "Mode change cancelled."; return 0 ;;
      *) info "Invalid ownership choice."; return 0 ;;
    esac
  fi
  if [[ "${mode}" == "auto" ]]; then
    info "Auto Protect may stop and later start only ${profile_id} after the configured thresholds."
  fi
  confirm "Set ${profile_id} Watchdog mode to ${mode}?" || { info "Mode change cancelled."; return 0; }
  run_watchdog_command "${arguments[@]}"
}

watchdog_change_mode() {
  local profile_id choice mode
  watchdog_select_profile || return 0
  profile_id="${WATCHDOG_SELECTED_PROFILE}"
  run_watchdog_command effective "${profile_id}"
  cat <<'MENU'
1) Monitor Only
2) Auto Protect
3) Disabled
MENU
  read -r -p "Choose Watchdog mode: " choice
  case "${choice}" in
    1) mode=monitor ;;
    2) mode=auto ;;
    3) mode=disabled ;;
    *) info "Invalid Watchdog mode."; return 0 ;;
  esac
  watchdog_apply_mode_change "${profile_id}" "${mode}"
}

watchdog_disable_profile() {
  local profile_id
  watchdog_select_profile || return 0
  profile_id="${WATCHDOG_SELECTED_PROFILE}"
  watchdog_apply_mode_change "${profile_id}" disabled
}

watchdog_integer() {
  local label="$1"
  local default="$2"
  local minimum="$3"
  local maximum="$4"
  local value
  value="$(prompt_default "${label}" "${default}")"
  if [[ ! "${value}" =~ ^[0-9]+$ ]] || ((10#${value} < minimum || 10#${value} > maximum)); then
    info "${label} must be between ${minimum} and ${maximum}." >&2
    return 1
  fi
  printf '%s\n' "${value}"
}

watchdog_configure_profile() {
  local profile_id current_mode current_interval current_timeout current_failures
  local current_successes current_hold current_jitter parsed
  local interval timeout failures successes hold jitter
  local -a arguments
  watchdog_select_profile || return 0
  profile_id="${WATCHDOG_SELECTED_PROFILE}"
  parsed="$(watchdog_effective_values "${profile_id}")" || return 0
  IFS=$'\t' read -r current_mode current_interval current_timeout current_failures \
    current_successes current_hold current_jitter <<< "${parsed}"
  interval="$(watchdog_integer "Check interval seconds" "${current_interval}" 1 300)" || return 0
  timeout="$(watchdog_integer "Ping timeout seconds" "${current_timeout}" 1 60)" || return 0
  if ((10#${timeout} > 10#${interval})); then
    info "Ping timeout must not exceed the check interval."
    return 0
  fi
  failures="$(watchdog_integer "Failure threshold" "${current_failures}" 1 1000)" || return 0
  successes="$(watchdog_integer "Success threshold" "${current_successes}" 1 1000)" || return 0
  hold="$(watchdog_integer "Recovery hold seconds" "${current_hold}" 0 3600)" || return 0
  jitter="$(watchdog_integer "Recovery jitter maximum seconds" "${current_jitter}" 0 300)" || return 0
  arguments=(configure-profile "${profile_id}")
  [[ "${interval}" == "${current_interval}" ]] || arguments+=(--check-interval "${interval}")
  [[ "${timeout}" == "${current_timeout}" ]] || arguments+=(--ping-timeout "${timeout}")
  [[ "${failures}" == "${current_failures}" ]] || arguments+=(--failure-threshold "${failures}")
  [[ "${successes}" == "${current_successes}" ]] || arguments+=(--success-threshold "${successes}")
  [[ "${hold}" == "${current_hold}" ]] || arguments+=(--recovery-hold "${hold}")
  [[ "${jitter}" == "${current_jitter}" ]] || arguments+=(--recovery-jitter "${jitter}")
  if [[ "${#arguments[@]}" -eq 2 ]]; then
    info "No profile values changed."
    return 0
  fi
  info "Effective candidate: interval=${interval}s timeout=${timeout}s failures=${failures} successes=${successes} hold=${hold}s jitter=0-${jitter}s"
  confirm "Save these overrides for ${profile_id}?" || { info "Override change cancelled."; return 0; }
  run_watchdog_command "${arguments[@]}"
}

watchdog_reset_profile() {
  local profile_id
  watchdog_select_profile || return 0
  profile_id="${WATCHDOG_SELECTED_PROFILE}"
  confirm "Reset ${profile_id} timing overrides and return its mode to Disabled?" || { info "Reset cancelled."; return 0; }
  run_watchdog_command reset-profile "${profile_id}"
}

watchdog_test_ping() {
  local profile_id
  watchdog_select_profile || return 0
  profile_id="${WATCHDOG_SELECTED_PROFILE}"
  run_watchdog_command ping "${profile_id}"
}

watchdog_maintenance_menu() {
  local profile_id choice action
  watchdog_select_profile || return 0
  profile_id="${WATCHDOG_SELECTED_PROFILE}"
  cat <<'MENU'
1) Enter maintenance and keep current service state
2) Enter maintenance and stop service now
3) Exit maintenance without starting
4) Exit maintenance and start when upstream is healthy
MENU
  read -r -p "Choose maintenance action: " choice
  case "${choice}" in
    1) action=enter-keep ;;
    2) action=enter-stop ;;
    3) action=exit-no-start ;;
    4) action=exit-start ;;
    *) info "Invalid maintenance action."; return 0 ;;
  esac
  if [[ "${action}" == "enter-stop" || "${action}" == "exit-start" ]]; then
    confirm "This action may change only gost-${profile_id}.service. Continue?" || { info "Maintenance action cancelled."; return 0; }
  fi
  run_watchdog_command maintenance "${profile_id}" "${action}"
}

watchdog_configure_global() {
  local current_interval current_timeout current_failures current_successes current_hold current_jitter parsed
  local interval timeout failures successes hold jitter
  local -a arguments
  parsed="$(watchdog_global_values)" || return 0
  IFS=$'\t' read -r current_interval current_timeout current_failures \
    current_successes current_hold current_jitter <<< "${parsed}"
  interval="$(watchdog_integer "Global check interval seconds" "${current_interval}" 1 300)" || return 0
  timeout="$(watchdog_integer "Global Ping timeout seconds" "${current_timeout}" 1 60)" || return 0
  if ((10#${timeout} > 10#${interval})); then
    info "Ping timeout must not exceed the check interval."
    return 0
  fi
  failures="$(watchdog_integer "Global failure threshold" "${current_failures}" 1 1000)" || return 0
  successes="$(watchdog_integer "Global success threshold" "${current_successes}" 1 1000)" || return 0
  hold="$(watchdog_integer "Global recovery hold seconds" "${current_hold}" 0 3600)" || return 0
  jitter="$(watchdog_integer "Global recovery jitter maximum seconds" "${current_jitter}" 0 300)" || return 0
  arguments=(set-global)
  [[ "${interval}" == "${current_interval}" ]] || arguments+=(--check-interval "${interval}")
  [[ "${timeout}" == "${current_timeout}" ]] || arguments+=(--ping-timeout "${timeout}")
  [[ "${failures}" == "${current_failures}" ]] || arguments+=(--failure-threshold "${failures}")
  [[ "${successes}" == "${current_successes}" ]] || arguments+=(--success-threshold "${successes}")
  [[ "${hold}" == "${current_hold}" ]] || arguments+=(--recovery-hold "${hold}")
  [[ "${jitter}" == "${current_jitter}" ]] || arguments+=(--recovery-jitter "${jitter}")
  if [[ "${#arguments[@]}" -eq 1 ]]; then
    info "No global values changed."
    return 0
  fi
  info "Global candidate: Ping every ${interval}s, timeout ${timeout}s, down after ${failures}, recover after ${successes} + ${hold}s + 0-${jitter}s."
  confirm "Save these global Watchdog defaults?" || { info "Global change cancelled."; return 0; }
  run_watchdog_command "${arguments[@]}"
}

watchdog_service_status() {
  systemctl --no-pager status "${WATCHDOG_SERVICE}" || true
  run_watchdog_command status
}

watchdog_restart_service() {
  confirm "Restart only ${WATCHDOG_SERVICE}?" || { info "Watchdog restart cancelled."; return 0; }
  if systemctl restart "${WATCHDOG_SERVICE}"; then
    info "Upstream Watchdog restarted. No GOST traffic service was restarted."
  else
    info "Upstream Watchdog restart failed."
  fi
}

watchdog_rearm_profile() {
  local profile_id
  watchdog_select_profile || return 0
  profile_id="${WATCHDOG_SELECTED_PROFILE}"
  confirm "Clear manual override and re-arm Auto Protect for ${profile_id}?" || { info "Re-arm cancelled."; return 0; }
  run_watchdog_command rearm "${profile_id}"
}

show_watchdog_menu() {
  cat <<'MENU'
Upstream Watchdog
=================

Default Ping interval: 2 seconds

1) Show all profile status
2) Enable or change profile mode
3) Disable watchdog for profile
4) Configure profile overrides
5) Reset profile overrides to global defaults
6) Test profile ping
7) Maintenance mode
8) Show last 24-hour events
9) Show 24-hour outage summary
10) Configure global defaults
11) Show watchdog service status
12) Restart watchdog service
13) Re-arm manual override
14) Back
MENU
}

watchdog_menu() {
  local choice
  while true; do
    show_watchdog_menu
    read -r -p "Choose an Upstream Watchdog option: " choice
    case "${choice}" in
      1) run_watchdog_command status ;;
      2) watchdog_change_mode ;;
      3) watchdog_disable_profile ;;
      4) watchdog_configure_profile ;;
      5) watchdog_reset_profile ;;
      6) watchdog_test_ping ;;
      7) watchdog_maintenance_menu ;;
      8) run_watchdog_command events --limit 200 ;;
      9) run_watchdog_command summary ;;
      10) watchdog_configure_global ;;
      11) watchdog_service_status ;;
      12) watchdog_restart_service ;;
      13) watchdog_rearm_profile ;;
      14) return 0 ;;
      *) info "Invalid Upstream Watchdog option." ;;
    esac
    printf '\n'
  done
}

show_menu() {
  manager_banner
  cat <<'MENU'
====================

1) Install / Update GOST
2) Create Kharej tunnel
3) Create Iran tunnel
4) Delete tunnel
5) Show status
6) Show logs
7) Restart tunnel
8) List active GOST services
9) Clean old/broken GOST configs
10) Monitoring
11) Server Stability
12) Upstream Watchdog
0) Exit
MENU
}

main_menu() {
  local choice
  while true; do
    show_menu
    read -r -p "Choose an option: " choice
    case "${choice}" in
      1) install_or_update_gost ;;
      2) create_kharej_tunnel ;;
      3) create_iran_tunnel ;;
      4) delete_tunnel ;;
      5) show_status ;;
      6) show_logs ;;
      7) restart_tunnel ;;
      8) list_active_gost_services ;;
      9) clean_old_broken_configs ;;
      10) monitoring_menu ;;
      11) server_stability_wizard ;;
      12) watchdog_menu ;;
      0) exit 0 ;;
      *) info "Invalid option." ;;
    esac
    printf '\n'
  done
}

run_manager() {
  if [[ "${1:-}" == "--version" ]]; then
    [[ "$#" -eq 1 ]] || die "--version does not accept additional arguments."
    manager_banner
    return 0
  fi
  [[ "$#" -eq 0 ]] || die "unknown manager option: $1"
  main_menu
}

if [[ "${GOST_MANAGER_TESTING:-0}" != "1" ]]; then
  run_manager "$@"
fi
