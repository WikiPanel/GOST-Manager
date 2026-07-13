#!/usr/bin/env bash
set -Eeuo pipefail

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
GATEWAY_BIN="/usr/local/sbin/gost-gateway"
GATEWAY_RUNTIME_BIN="/usr/local/sbin/gost-gateway-runtime"
GATEWAY_NGINX_BIN="/usr/local/sbin/gost-gateway-nginx"

if [[ "${GOST_MANAGER_TESTING:-0}" == "1" ]]; then
  MONITOR_CONFIG="${GOST_MONITOR_CONFIG_TEST:-${MONITOR_CONFIG}}"
  MONITOR_EXPORT_DIR="${GOST_MONITOR_EXPORT_DIR_TEST:-${MONITOR_EXPORT_DIR}}"
  MONITOR_BIN="${GOST_MONITOR_BIN_TEST:-${MONITOR_BIN}}"
  MONITOR_COLLECTOR_BIN="${GOST_MONITOR_COLLECTOR_BIN_TEST:-${MONITOR_COLLECTOR_BIN}}"
  MONITOR_ADMIN_BIN="${GOST_MONITOR_ADMIN_BIN_TEST:-${MONITOR_ADMIN_BIN}}"
  GATEWAY_BIN="${GOST_GATEWAY_BIN_TEST:-${GATEWAY_BIN}}"
  GATEWAY_RUNTIME_BIN="${GOST_GATEWAY_RUNTIME_BIN_TEST:-${GATEWAY_RUNTIME_BIN}}"
  GATEWAY_NGINX_BIN="${GOST_GATEWAY_NGINX_BIN_TEST:-${GATEWAY_NGINX_BIN}}"
fi

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

info() {
  printf '%s\n' "$*"
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
  base="$(basename "${env_file}")"
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

confirm_overwrite_file() {
  local path="$1"
  if [[ -e "${path}" ]]; then
    confirm "File exists: ${path}. Overwrite it?" || die "aborted by user."
  fi
}

write_secure_env_file() {
  local path="$1"
  shift
  local tmp
  tmp="$(mktemp)"
  chmod 600 "${tmp}"
  while [[ "$#" -gt 0 ]]; do
    printf '%s=%s\n' "$1" "$2" >> "${tmp}"
    shift 2
  done
  backup_existing_file "${path}"
  install -m 600 -o root -g root "${tmp}" "${path}"
  rm -f "${tmp}"
}

write_service_file() {
  local path="$1"
  local description="$2"
  local env_file="$3"
  local runner="$4"
  local tmp
  tmp="$(mktemp)"
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
  backup_existing_file "${path}"
  install -m 644 -o root -g root "${tmp}" "${path}"
  rm -f "${tmp}"
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

generate_password() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 18
    return 0
  fi
  od -An -N18 -tx1 /dev/urandom | tr -d ' \n'
  printf '\n'
}

add_kharej_firewall_rules() {
  local number="$1"
  local iran_ip="$2"
  local port="$3"
  local allow_comment="gost-manager:kharej-${number}:allow"
  local drop_comment="gost-manager:kharej-${number}:drop"

  ensure_commands iptables

  if ! iptables -C INPUT -p tcp -s "${iran_ip}" --dport "${port}" -m comment --comment "${allow_comment}" -j ACCEPT >/dev/null 2>&1; then
    iptables -I INPUT 1 -p tcp -s "${iran_ip}" --dport "${port}" -m comment --comment "${allow_comment}" -j ACCEPT
  fi
  if ! iptables -C INPUT -p tcp --dport "${port}" -m comment --comment "${drop_comment}" -j DROP >/dev/null 2>&1; then
    iptables -I INPUT 2 -p tcp --dport "${port}" -m comment --comment "${drop_comment}" -j DROP
  fi

  cat <<'WARN'
Warning: iptables rules are not persistent by default.
They may be lost after reboot unless saved with netfilter-persistent or your server firewall system.
WARN
}

delete_iptables_rule_loop() {
  local args=("$@")
  while iptables "${args[@]}" >/dev/null 2>&1; do
    :
  done
}

delete_iptables_rules_by_comment() {
  local comment="$1"
  if ! command -v iptables >/dev/null 2>&1; then
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 - "${comment}" <<'PY' || true
import shlex
import subprocess
import sys

comment = sys.argv[1]
for _ in range(50):
    result = subprocess.run(["iptables", "-S", "INPUT"], text=True, capture_output=True, check=False)
    line = None
    for candidate in result.stdout.splitlines():
        if "--comment" in candidate and comment in candidate:
            line = candidate
            break
    if not line:
        break
    args = shlex.split(line)
    if not args or args[0] != "-A":
        break
    args[0] = "-D"
    subprocess.run(["iptables"] + args, check=False)
PY
  fi
}

delete_kharej_firewall_rules() {
  local number="$1"
  local env_file="$2"
  local iran_ip port
  local allow_comment="gost-manager:kharej-${number}:allow"
  local drop_comment="gost-manager:kharej-${number}:drop"

  if ! command -v iptables >/dev/null 2>&1; then
    return 0
  fi

  if [[ -f "${env_file}" ]]; then
    iran_ip="$(env_get IRAN_IP "${env_file}")"
    port="$(env_get TUNNEL_PORT "${env_file}")"
    if [[ -n "${iran_ip}" && -n "${port}" ]]; then
      delete_iptables_rule_loop -D INPUT -p tcp -s "${iran_ip}" --dport "${port}" -m comment --comment "${allow_comment}" -j ACCEPT
      delete_iptables_rule_loop -D INPUT -p tcp --dport "${port}" -m comment --comment "${drop_comment}" -j DROP
    fi
  fi

  delete_iptables_rules_by_comment "${allow_comment}"
  delete_iptables_rules_by_comment "${drop_comment}"
}

port_busy_detail() {
  local port="$1"
  local detail
  if ! command -v ss >/dev/null 2>&1; then
    return 1
  fi
  detail="$(ss -H -lntp "sport = :${port}" 2>/dev/null || true)"
  if [[ -z "${detail}" ]]; then
    detail="$(ss -lntp 2>/dev/null | awk -v port=":${port}" '$0 ~ port {print}' || true)"
  fi
  if [[ -n "${detail}" ]]; then
    printf '%s\n' "${detail}"
    return 0
  fi
  return 1
}

check_mapping_ports_free_or_die() {
  local mappings="$1"
  local IFS=','
  local pairs=()
  local pair listen busy_output detail
  read -r -a pairs <<< "${mappings}"

  ensure_commands ss

  busy_output=""
  for pair in "${pairs[@]}"; do
    listen="${pair%%:*}"
    detail="$(port_busy_detail "${listen}" || true)"
    if [[ -n "${detail}" ]]; then
      busy_output="${busy_output}"$'\n'"Port ${listen}:"$'\n'"${detail}"$'\n'
    fi
  done

  if [[ -n "${busy_output}" ]]; then
    printf 'Cannot create Iran tunnel. These listen ports are already in use:\n%s\nNo files were changed.\n' "${busy_output}" >&2
    exit 1
  fi
}

create_kharej_tunnel() {
  require_root
  ensure_commands systemctl

  local number port user password iran_ip firewall_answer firewall_enabled env_file svc_file service
  number="$(prompt_required "Tunnel number, e.g. 1")"
  validate_tunnel_number_or_die "${number}"
  port="$(prompt_default "SOCKS listen port" "28420")"
  validate_port_or_die "${port}"
  user="$(prompt_default "GOST username" "maya")"
  validate_token_or_die "GOST username" "${user}"
  read -r -p "GOST password (leave empty to generate random): " password
  if [[ -z "${password}" ]]; then
    password="$(generate_password)"
  fi
  validate_token_or_die "GOST password" "${password}"
  iran_ip="$(prompt_required "Iran IP allowed, e.g. 88.218.18.13")"
  validate_iptables_source_or_die "${iran_ip}"
  read -r -p "Apply iptables firewall rule? [y/N]: " firewall_answer
  case "${firewall_answer}" in
    y|Y|yes|YES|Yes) firewall_enabled=1 ;;
    *) firewall_enabled=0 ;;
  esac

  mkdir -p "${GOST_ETC_DIR}"
  chmod 700 "${GOST_ETC_DIR}"

  env_file="$(env_path kharej "${number}")"
  svc_file="$(service_path kharej "${number}")"
  service="$(service_name kharej "${number}")"
  confirm_overwrite_file "${env_file}"
  confirm_overwrite_file "${svc_file}"

  write_secure_env_file "${env_file}" \
    GOST_USER "${user}" \
    GOST_PASS "${password}" \
    TUNNEL_PORT "${port}" \
    IRAN_IP "${iran_ip}" \
    FIREWALL_ENABLED "${firewall_enabled}"

  write_service_file "${svc_file}" "GOST Kharej Tunnel ${number}" "${env_file}" "${RUNNER_KHAREJ}"

  if [[ "${firewall_enabled}" == "1" ]]; then
    add_kharej_firewall_rules "${number}" "${iran_ip}" "${port}"
  fi

  systemctl daemon-reload
  systemctl enable --now "${service}"

  cat <<EOF_OUT
Kharej tunnel created successfully.

Service: ${service}
Env: ${env_file}
SOCKS: 0.0.0.0:${port}
GOST_USER=${user}
GOST_PASS=${password}
Allowed Iran IP: ${iran_ip}
Firewall: $(if [[ "${firewall_enabled}" == "1" ]]; then printf 'enabled'; else printf 'disabled'; fi)
EOF_OUT
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
  ensure_commands systemctl ss

  local number kharej_ip socks_port user password mappings env_file svc_file service
  number="$(prompt_required "Tunnel number, e.g. 1")"
  validate_tunnel_number_or_die "${number}"
  kharej_ip="$(prompt_required "Kharej IP, e.g. 37.252.4.65")"
  validate_host_or_die "Kharej IP" "${kharej_ip}"
  socks_port="$(prompt_default "Kharej SOCKS port" "28420")"
  validate_port_or_die "${socks_port}"
  user="$(prompt_default "GOST username" "maya")"
  validate_token_or_die "GOST username" "${user}"
  password="$(prompt_required "GOST password from Kharej side")"
  validate_token_or_die "GOST password" "${password}"
  info "Port mappings format: 2052:2052 or 80:80,8080:8080,8880:8880"
  mappings="$(prompt_required "Port mappings")"
  validate_mappings "${mappings}" || exit 1
  check_mapping_ports_free_or_die "${mappings}"

  mkdir -p "${GOST_ETC_DIR}"
  chmod 700 "${GOST_ETC_DIR}"

  env_file="$(env_path iran "${number}")"
  svc_file="$(service_path iran "${number}")"
  service="$(service_name iran "${number}")"
  confirm_overwrite_file "${env_file}"
  confirm_overwrite_file "${svc_file}"

  write_secure_env_file "${env_file}" \
    GOST_USER "${user}" \
    GOST_PASS "${password}" \
    KHAREJ_IP "${kharej_ip}" \
    TUNNEL_PORT "${socks_port}" \
    MAPPINGS "${mappings}"

  write_service_file "${svc_file}" "GOST Iran Tunnel ${number}" "${env_file}" "${RUNNER_IRAN}"

  systemctl daemon-reload
  systemctl enable --now "${service}"

  print_iran_success "${number}" "${env_file}" "${kharej_ip}" "${socks_port}" "${mappings}"
}

discover_existing_tunnels() {
  local output_file="$1"
  local service_file env_file base service identity side number
  local tmp_file
  tmp_file="$(mktemp)"
  : > "${tmp_file}"

  for service_file in "${SYSTEMD_DIR}"/gost-iran-*.service "${SYSTEMD_DIR}"/gost-kharej-*.service; do
    [[ -e "${service_file}" ]] || continue
    base="$(basename "${service_file}")"
    identity="$(parse_tunnel_service_name "${base}" || true)"
    [[ -n "${identity}" ]] || continue
    side="${identity%% *}"
    number="${identity#* }"
    service="$(service_name "${side}" "${number}")"
    env_file="$(env_path "${side}" "${number}")"
    printf '%s|%s|%s|%s|%s\n' "${side}" "${number}" "${service}" "${service_file}" "${env_file}" >> "${tmp_file}"
  done

  for env_file in "${GOST_ETC_DIR}"/iran-*.env "${GOST_ETC_DIR}"/kharej-*.env; do
    [[ -e "${env_file}" ]] || continue
    identity="$(parse_tunnel_env_name "${env_file}" || true)"
    [[ -n "${identity}" ]] || continue
    side="${identity%% *}"
    number="${identity#* }"
    service="$(service_name "${side}" "${number}")"
    service_file="$(service_path "${side}" "${number}")"
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

  local side number service svc_file env_file
  select_existing_tunnel
  side="${SELECTED_TUNNEL_SIDE}"
  number="${SELECTED_TUNNEL_NUMBER}"
  service="${SELECTED_TUNNEL_SERVICE}"
  svc_file="${SELECTED_TUNNEL_SERVICE_FILE}"
  env_file="${SELECTED_TUNNEL_ENV_FILE}"

  confirm "Delete ${service} and its managed files?" || die "delete aborted."

  if [[ "${side}" == "kharej" ]]; then
    delete_kharej_firewall_rules "${number}" "${env_file}"
  fi

  systemctl disable --now "${service}" || true
  rm -f "${svc_file}"
  rm -f "${env_file}"
  systemctl daemon-reload
  systemctl reset-failed
  info "Deleted ${service} and ${env_file}."
}

show_related_listen_ports() {
  local side="$1"
  local env_file="$2"
  local mappings port pair listen
  local pairs=()

  if ! command -v ss >/dev/null 2>&1; then
    info "ss is not available; cannot show listen ports."
    return 0
  fi

  if [[ ! -f "${env_file}" ]]; then
    ss -lntp 2>/dev/null | grep gost || true
    return 0
  fi

  if [[ "${side}" == "iran" ]]; then
    mappings="$(env_get MAPPINGS "${env_file}")"
    if [[ -z "${mappings}" ]]; then
      ss -lntp 2>/dev/null | grep gost || true
      return 0
    fi
    IFS=',' read -r -a pairs <<< "${mappings}"
    for pair in "${pairs[@]}"; do
      listen="${pair%%:*}"
      ss -lntp "sport = :${listen}" 2>/dev/null || true
    done
    return 0
  fi

  port="$(env_get TUNNEL_PORT "${env_file}")"
  if [[ -n "${port}" ]]; then
    ss -lntp "sport = :${port}" 2>/dev/null || true
  else
    ss -lntp 2>/dev/null | grep gost || true
  fi
}

show_status() {
  local side service env_file
  select_existing_tunnel
  side="${SELECTED_TUNNEL_SIDE}"
  service="${SELECTED_TUNNEL_SERVICE}"
  env_file="${SELECTED_TUNNEL_ENV_FILE}"
  systemctl status "${service}" --no-pager || true
  printf '\nRelated listen ports:\n'
  show_related_listen_ports "${side}" "${env_file}"
}

show_logs() {
  local service
  select_existing_tunnel
  service="${SELECTED_TUNNEL_SERVICE}"
  journalctl -u "${service}" -n 100 --no-pager
}

restart_tunnel() {
  require_root
  ensure_commands systemctl

  local service
  select_existing_tunnel
  service="${SELECTED_TUNNEL_SERVICE}"
  systemctl restart "${service}"
  systemctl status "${service}" --no-pager --lines=20 || true
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

list_active_gost_services() {
  local file base number found=0

  systemctl list-units --type=service --all 'gost-*' || true
  printf '\n%-24s %-16s %s\n' "SERVICE" "STATUS" "DETAILS"

  for file in "${GOST_ETC_DIR}"/iran-*.env; do
    [[ -e "${file}" ]] || continue
    found=1
    base="$(basename "${file}")"
    number="${base#iran-}"
    number="${number%.env}"
    summarize_iran_env "${file}" "${number}"
  done

  for file in "${GOST_ETC_DIR}"/kharej-*.env; do
    [[ -e "${file}" ]] || continue
    found=1
    base="$(basename "${file}")"
    number="${base#kharej-}"
    number="${number%.env}"
    summarize_kharej_env "${file}" "${number}"
  done

  if [[ "${found}" -eq 0 ]]; then
    info "No managed GOST tunnels found."
  fi
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
  if [[ ! "${service}" =~ ^(nginx|gost-(iran|kharej)-[1-9][0-9]*)\.service$ ]]; then
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

gateway_command() {
  local executable="$1"
  shift
  local -a arguments=("$@")
  if ! "${executable}" "${arguments[@]}"; then
    info "Gateway command failed; no follow-up action was run."
  fi
  return 0
}

gateway_overview() {
  info "Desired state"
  gateway_command "${GATEWAY_BIN}" show
  info "Local GOST Exit runtime"
  gateway_command "${GATEWAY_RUNTIME_BIN}" runtime status
  info "NGINX plan"
  gateway_command "${GATEWAY_NGINX_BIN}" plan
  info "Dedicated NGINX service"
  gateway_command "${GATEWAY_NGINX_BIN}" status
}

gateway_initialize() {
  local gateway_id node_id listen_address listen_port status_port server_name
  local -a arguments=()
  gateway_id="$(prompt_required "Gateway ID")"
  node_id="$(prompt_required "Node ID")"
  listen_address="$(prompt_default "Public IPv4 listen address" "0.0.0.0")"
  listen_port="$(prompt_default "Public port" "80")"
  status_port="$(prompt_default "Loopback status port" "18000")"
  server_name="$(prompt_required "Server name")"
  arguments=(
    init --gateway-id "${gateway_id}" --node-id "${node_id}"
    --listen-address "${listen_address}" --listen-port "${listen_port}"
    --status-port "${status_port}" --server-name "${server_name}"
  )
  while confirm "Add another server name?"; do
    server_name="$(prompt_required "Server name")"
    arguments+=(--server-name "${server_name}")
  done
  gateway_command "${GATEWAY_BIN}" "${arguments[@]}"
}

gateway_settings_menu() {
  local choice value
  local -a arguments=()
  while true; do
    cat <<'MENU'
Gateway listener and Host settings
==================================

1) Show
2) Enable Gateway
3) Disable Gateway
4) Change listen address
5) Change public port
6) Replace server names
7) Change status port
0) Back
MENU
    read -r -p "Choose a Gateway setting: " choice
    case "${choice}" in
      1) gateway_command "${GATEWAY_BIN}" gateway show ;;
      2) gateway_command "${GATEWAY_BIN}" gateway set --enable ;;
      3) gateway_command "${GATEWAY_BIN}" gateway set --disable ;;
      4)
        value="$(prompt_required "Public IPv4 listen address")"
        gateway_command "${GATEWAY_BIN}" gateway set --listen-address "${value}"
        ;;
      5)
        value="$(prompt_required "Public port")"
        gateway_command "${GATEWAY_BIN}" gateway set --listen-port "${value}"
        ;;
      6)
        arguments=(gateway set)
        value="$(prompt_required "Server name")"
        arguments+=(--server-name "${value}")
        while confirm "Add another server name?"; do
          value="$(prompt_required "Server name")"
          arguments+=(--server-name "${value}")
        done
        gateway_command "${GATEWAY_BIN}" "${arguments[@]}"
        ;;
      7)
        value="$(prompt_required "Loopback status port")"
        gateway_command "${GATEWAY_BIN}" gateway set --status-port "${value}"
        ;;
      0) return 0 ;;
      *) info "Invalid Gateway setting." ;;
    esac
    [[ "${choice}" == "1" || "${choice}" == "0" ]] || info "Run NGINX plan before apply."
  done
}

gateway_exit_menu() {
  local choice exit_id display_name host socks_port target_port
  while true; do
    cat <<'MENU'
Manage Exits
============

1) List
2) Add
3) Edit endpoint
4) Enable
5) Disable
6) Delete
0) Back
MENU
    read -r -p "Choose an Exit action: " choice
    case "${choice}" in
      1) gateway_command "${GATEWAY_BIN}" exit list ;;
      2)
        exit_id="$(prompt_required "Exit ID")"
        display_name="$(prompt_required "Display name")"
        host="$(prompt_required "Kharej host")"
        socks_port="$(prompt_required "SOCKS port")"
        target_port="$(prompt_required "Target port")"
        gateway_command "${GATEWAY_BIN}" exit add --id "${exit_id}" \
          --display-name "${display_name}" --host "${host}" \
          --socks-port "${socks_port}" --target-port "${target_port}"
        ;;
      3)
        exit_id="$(prompt_required "Exit ID")"
        host="$(prompt_required "Kharej host")"
        socks_port="$(prompt_required "SOCKS port")"
        target_port="$(prompt_required "Target port")"
        gateway_command "${GATEWAY_BIN}" exit edit --id "${exit_id}" \
          --host "${host}" --socks-port "${socks_port}" --target-port "${target_port}"
        ;;
      4|5)
        exit_id="$(prompt_required "Exit ID")"
        if [[ "${choice}" == "4" ]]; then
          gateway_command "${GATEWAY_BIN}" exit edit --id "${exit_id}" --enable
        else
          gateway_command "${GATEWAY_BIN}" exit edit --id "${exit_id}" --disable
        fi
        ;;
      6)
        exit_id="$(prompt_required "Exit ID")"
        confirm "Delete this Exit from desired state?" && \
          gateway_command "${GATEWAY_BIN}" exit delete --id "${exit_id}"
        ;;
      0) return 0 ;;
      *) info "Invalid Exit action." ;;
    esac
  done
}

gateway_binding_secret_menu() {
  local choice exit_id listen_port secret_ref
  while true; do
    cat <<'MENU'
Manage local Bindings and Secrets
=================================

1) List Bindings
2) Set/enable Binding
3) Disable Binding
4) Remove Binding
5) List Secret references
6) Create/update Secret (hidden prompt)
7) Validate Secret
8) Delete unreferenced Secret
0) Back
MENU
    read -r -p "Choose a Binding or Secret action: " choice
    case "${choice}" in
      1) gateway_command "${GATEWAY_BIN}" binding list ;;
      2)
        exit_id="$(prompt_required "Exit ID")"
        listen_port="$(prompt_required "Loopback binding port")"
        secret_ref="$(prompt_required "Secret reference")"
        gateway_command "${GATEWAY_BIN}" binding set --exit-id "${exit_id}" \
          --listen-port "${listen_port}" --secret-ref "${secret_ref}" --enable
        ;;
      3)
        exit_id="$(prompt_required "Exit ID")"
        listen_port="$(prompt_required "Loopback binding port")"
        secret_ref="$(prompt_default "Secret reference (may be empty)" "unused")"
        gateway_command "${GATEWAY_BIN}" binding set --exit-id "${exit_id}" \
          --listen-port "${listen_port}" --secret-ref "${secret_ref}" --disable
        ;;
      4)
        exit_id="$(prompt_required "Exit ID")"
        gateway_command "${GATEWAY_BIN}" binding remove --exit-id "${exit_id}"
        ;;
      5) gateway_command "${GATEWAY_RUNTIME_BIN}" secret list ;;
      6)
        secret_ref="$(prompt_required "Secret reference")"
        gateway_command "${GATEWAY_RUNTIME_BIN}" secret set --ref "${secret_ref}"
        ;;
      7)
        secret_ref="$(prompt_required "Secret reference")"
        gateway_command "${GATEWAY_RUNTIME_BIN}" secret validate --ref "${secret_ref}"
        ;;
      8)
        secret_ref="$(prompt_required "Secret reference")"
        confirm "Delete this unreferenced Secret?" && \
          gateway_command "${GATEWAY_RUNTIME_BIN}" secret delete --ref "${secret_ref}" --yes
        ;;
      0) return 0 ;;
      *) info "Invalid Binding or Secret action." ;;
    esac
  done
}

gateway_route_exit_arguments() {
  local exit_id
  GATEWAY_ROUTE_EXIT_ARGS=()
  exit_id="$(prompt_required "Ordered Exit ID 1")"
  GATEWAY_ROUTE_EXIT_ARGS+=(--exit-id "${exit_id}")
  while confirm "Add another ordered Exit ID?"; do
    exit_id="$(prompt_required "Next ordered Exit ID")"
    GATEWAY_ROUTE_EXIT_ARGS+=(--exit-id "${exit_id}")
  done
}

gateway_route_menu() {
  local choice route_id display_name host path strategy
  local -a GATEWAY_ROUTE_EXIT_ARGS=()
  while true; do
    cat <<'MENU'
Manage Routes
=============

1) List
2) Add
3) Edit
4) Enable
5) Disable
6) Delete
0) Back
MENU
    read -r -p "Choose a Route action: " choice
    case "${choice}" in
      1) gateway_command "${GATEWAY_BIN}" route list ;;
      2|3)
        route_id="$(prompt_required "Route ID")"
        display_name="$(prompt_required "Display name")"
        host="$(prompt_required "Exact Host")"
        path="$(prompt_required "Exact WebSocket Path")"
        strategy="$(prompt_required "Strategy (active-active or active-passive)")"
        gateway_route_exit_arguments
        if [[ "${choice}" == "2" ]]; then
          gateway_command "${GATEWAY_BIN}" route add --id "${route_id}" \
            --display-name "${display_name}" --host "${host}" --path "${path}" \
            --strategy "${strategy}" "${GATEWAY_ROUTE_EXIT_ARGS[@]}"
        else
          gateway_command "${GATEWAY_BIN}" route edit --id "${route_id}" \
            --display-name "${display_name}" --host "${host}" --path "${path}" \
            --strategy "${strategy}" "${GATEWAY_ROUTE_EXIT_ARGS[@]}"
        fi
        ;;
      4|5)
        route_id="$(prompt_required "Route ID")"
        if [[ "${choice}" == "4" ]]; then
          gateway_command "${GATEWAY_BIN}" route edit --id "${route_id}" --enable
        else
          gateway_command "${GATEWAY_BIN}" route edit --id "${route_id}" --disable
        fi
        ;;
      6)
        route_id="$(prompt_required "Route ID")"
        confirm "Delete this Route?" && gateway_command "${GATEWAY_BIN}" route delete --id "${route_id}"
        ;;
      0) return 0 ;;
      *) info "Invalid Route action." ;;
    esac
    if [[ "${choice}" =~ ^[2-6]$ ]] && confirm "Run read-only NGINX plan now?"; then
      gateway_command "${GATEWAY_NGINX_BIN}" plan
    fi
  done
}

gateway_gost_runtime_menu() {
  local choice exit_id
  while true; do
    cat <<'MENU'
Local GOST Exit runtime
=======================

1) Plan
2) Apply
3) Status
4) Selected Exit status
5) Selected Exit start
6) Selected Exit stop
7) Selected Exit restart
0) Back
MENU
    read -r -p "Choose a local runtime action: " choice
    case "${choice}" in
      1) gateway_command "${GATEWAY_RUNTIME_BIN}" runtime plan ;;
      2) confirm "Apply local GOST Exit runtime?" && gateway_command "${GATEWAY_RUNTIME_BIN}" runtime apply --yes ;;
      3) gateway_command "${GATEWAY_RUNTIME_BIN}" runtime status ;;
      4|5|6|7)
        exit_id="$(prompt_required "Exit ID")"
        case "${choice}" in
          4) gateway_command "${GATEWAY_RUNTIME_BIN}" service status --exit-id "${exit_id}" ;;
          5) gateway_command "${GATEWAY_RUNTIME_BIN}" service start --exit-id "${exit_id}" ;;
          6) confirm "Stop this Exit service?" && gateway_command "${GATEWAY_RUNTIME_BIN}" service stop --exit-id "${exit_id}" --yes ;;
          7) confirm "Restart this Exit service and reconnect its sessions?" && gateway_command "${GATEWAY_RUNTIME_BIN}" service restart --exit-id "${exit_id}" --yes ;;
        esac
        ;;
      0) return 0 ;;
      *) info "Invalid local runtime action." ;;
    esac
  done
}

gateway_nginx_apply() {
  gateway_command "${GATEWAY_NGINX_BIN}" plan
  if confirm "Apply this NGINX Gateway plan?"; then
    info "Existing WebSocket connections should remain on old workers until they close. New connections use the new configuration."
    info "Stopping the Gateway disconnects active users."
    gateway_command "${GATEWAY_NGINX_BIN}" apply --yes
  fi
}

gateway_nginx_test_status_menu() {
  local choice
  while true; do
    read -r -p "1) NGINX test  2) NGINX status  0) Back: " choice
    case "${choice}" in
      1) gateway_command "${GATEWAY_NGINX_BIN}" test ;;
      2) gateway_command "${GATEWAY_NGINX_BIN}" status ;;
      0) return 0 ;;
      *) info "Invalid NGINX test/status action." ;;
    esac
  done
}

gateway_nginx_service_menu() {
  local choice phrase
  while true; do
    read -r -p "1) Status 2) Start 3) Stop 4) Reload 5) Restart 0) Back: " choice
    case "${choice}" in
      1) gateway_command "${GATEWAY_NGINX_BIN}" service status ;;
      2) gateway_command "${GATEWAY_NGINX_BIN}" service start ;;
      3) confirm "Stop the dedicated NGINX Gateway?" && gateway_command "${GATEWAY_NGINX_BIN}" service stop --yes ;;
      4) confirm "Gracefully reload the dedicated NGINX Gateway?" && gateway_command "${GATEWAY_NGINX_BIN}" service reload --yes ;;
      5)
        read -r -p "Type RESTART NGINX GATEWAY: " phrase
        if [[ "${phrase}" == "RESTART NGINX GATEWAY" ]]; then
          gateway_command "${GATEWAY_NGINX_BIN}" service restart --yes --acknowledge-disconnect
        else
          info "NGINX Gateway restart cancelled."
        fi
        ;;
      0) return 0 ;;
      *) info "Invalid NGINX service action." ;;
    esac
  done
}

gateway_nginx_dependency_menu() {
  local choice
  while true; do
    read -r -p "1) Dependency status  2) Install Ubuntu nginx package  0) Back: " choice
    case "${choice}" in
      1) gateway_command "${GATEWAY_NGINX_BIN}" dependency status ;;
      2)
        info "This installs Ubuntu nginx but GOST Manager does not use or overwrite /etc/nginx."
        confirm "Install the nginx package?" && gateway_command "${GATEWAY_NGINX_BIN}" dependency install --yes
        ;;
      0) return 0 ;;
      *) info "Invalid dependency action." ;;
    esac
  done
}

show_nginx_gateway_menu() {
  cat <<'MENU'
NGINX Gateway Mode
==================

1) Gateway overview
2) Initialize Gateway state
3) Gateway listener and Host settings
4) Manage Exits
5) Manage local Bindings and Secrets
6) Manage Routes
7) Local GOST Exit runtime
8) NGINX plan
9) Apply NGINX Gateway
10) NGINX test/status
11) NGINX service control
12) NGINX dependency status/install
0) Back
MENU
}

nginx_gateway_menu() {
  local choice
  while true; do
    show_nginx_gateway_menu
    read -r -p "Choose an NGINX Gateway option: " choice
    case "${choice}" in
      1) gateway_overview ;;
      2) gateway_initialize ;;
      3) gateway_settings_menu ;;
      4) gateway_exit_menu ;;
      5) gateway_binding_secret_menu ;;
      6) gateway_route_menu ;;
      7) gateway_gost_runtime_menu ;;
      8) gateway_command "${GATEWAY_NGINX_BIN}" plan ;;
      9) gateway_nginx_apply ;;
      10) gateway_nginx_test_status_menu ;;
      11) gateway_nginx_service_menu ;;
      12) gateway_nginx_dependency_menu ;;
      0) return 0 ;;
      *) info "Invalid NGINX Gateway option." ;;
    esac
    printf '\n'
  done
}

native_gost_gateway_coming_soon() {
  info "Native GOST Gateway: Coming soon."
}

show_menu() {
  cat <<'MENU'
GOST Manager
============

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
11) NGINX Gateway Mode
12) Native GOST Gateway (Coming soon)
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
      11) nginx_gateway_menu ;;
      12) native_gost_gateway_coming_soon ;;
      0) exit 0 ;;
      *) info "Invalid option." ;;
    esac
    printf '\n'
  done
}

if [[ "${GOST_MANAGER_TESTING:-0}" != "1" ]]; then
  main_menu "$@"
fi
