#!/usr/bin/env bash
set -Eeuo pipefail

GOST_MANAGER_ROOT="${GOST_MANAGER_ROOT:-}"
GOST_MANAGER_TESTING="${GOST_MANAGER_TESTING:-0}"
GOST_MANAGER_SOURCE_ONLY="${GOST_MANAGER_SOURCE_ONLY:-0}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
RM_BIN="${RM_BIN:-rm}"
RMDIR_BIN="${RMDIR_BIN:-rmdir}"
MONITOR_ADMIN_BIN="${MONITOR_ADMIN_BIN:-/usr/local/sbin/gost-monitor-admin}"
MONITOR_SERVICE="gost-monitor-collector.service"

REMOVE_MANAGER=0
REMOVE_MONITOR_SERVICE=0
REMOVE_MONITOR_CODE=0
REMOVE_MONITOR_CONFIG=0
REMOVE_MONITOR_HISTORY=0
REMOVE_TRAFFIC=0
REMOVE_CREDENTIALS=0
REMOVE_GOST_BINARY=0
REMOVE_GATEWAY_RUNTIME=0
REMOVE_GATEWAY_STATE=0
REMOVE_GATEWAY_SECRETS=0
REMOVE_GATEWAY_PACKAGE=0
UNIT_CHANGED=0
FAILED_ACTIONS=0
CONFIG_CAPTURED=0
CONFIGURED_DB_PRODUCTION=""
CONFIGURED_DB_ACTUAL=""

die() {
  printf 'Error: %s\n' "$*" >&2
  return 1
}

info() {
  printf '%s\n' "$*"
}

confirm() {
  local prompt="$1"
  local answer
  read -r -p "${prompt} [y/N]: " answer
  case "${answer}" in
    y|Y|yes|YES|Yes) return 0 ;;
    *) return 1 ;;
  esac
}

path_for() {
  local absolute="$1"
  [[ "${absolute}" == /* ]] || die "managed path must be absolute: ${absolute}"
  if [[ -n "${GOST_MANAGER_ROOT}" ]]; then
    printf '%s%s\n' "${GOST_MANAGER_ROOT}" "${absolute}"
  else
    printf '%s\n' "${absolute}"
  fi
}

require_safe_root() {
  local resolved
  if [[ "${GOST_MANAGER_TESTING}" == "1" ]]; then
    [[ -n "${GOST_MANAGER_ROOT}" ]] || die "testing mode requires GOST_MANAGER_ROOT."
    [[ "${GOST_MANAGER_ROOT}" == /* && "${GOST_MANAGER_ROOT}" != "/" ]] || die "testing root must be an absolute non-root path."
    [[ -d "${GOST_MANAGER_ROOT}" && ! -L "${GOST_MANAGER_ROOT}" ]] || die "testing root must be an existing real directory."
    resolved="$(cd "${GOST_MANAGER_ROOT}" && pwd -P)"
    [[ "${resolved}" != "/" ]] || die "testing root may not resolve to /."
    GOST_MANAGER_ROOT="${resolved}"
    return 0
  fi
  [[ -z "${GOST_MANAGER_ROOT}" ]] || die "GOST_MANAGER_ROOT is allowed only in testing mode."
  [[ "${EUID}" -eq 0 ]] || die "uninstall.sh must be run as root. Try: sudo bash uninstall.sh"
}

reject_symlink_path() {
  local path="$1"
  local boundary current relative part
  local -a parts=()
  boundary="${GOST_MANAGER_ROOT:-/}"
  current="${boundary}"
  relative="${path#"${boundary}"}"
  IFS='/' read -r -a parts <<< "${relative#/}"
  for part in "${parts[@]+"${parts[@]}"}"; do
    [[ -n "${part}" ]] || continue
    current="${current%/}/${part}"
    if [[ -L "${current}" ]]; then
      die "managed removal path may not traverse a symlink: ${current}"
      return 1
    fi
  done
  if [[ -n "${GOST_MANAGER_ROOT}" ]]; then
    if [[ "${path}" != "${GOST_MANAGER_ROOT}"/* ]]; then
      die "managed removal path escaped the testing root: ${path}"
      return 1
    fi
  fi
}

remove_file() {
  local path="$1"
  reject_symlink_path "${path}" || return 1
  [[ ! -e "${path}" ]] || "${RM_BIN}" -f "${path}"
}

remove_exact_tree() {
  local path="$1"
  local allowed_config allowed_credentials allowed_package allowed_gateway_package
  local allowed_gateway_state_backups allowed_gateway_runtime_backups
  allowed_config="$(path_for /etc/gost-manager)"
  allowed_credentials="$(path_for /etc/gost)"
  allowed_package="$(path_for /usr/local/lib/gost-manager/monitoring)"
  allowed_gateway_package="$(path_for /usr/local/lib/gost-manager/gateway)"
  allowed_gateway_state_backups="$(path_for /etc/gost-manager/backups/gateway)"
  allowed_gateway_runtime_backups="$(path_for /etc/gost-manager/backups/gateway-runtime)"
  case "${path}" in
    "${allowed_config}"|"${allowed_credentials}"|"${allowed_package}"| \
    "${allowed_gateway_package}"|"${allowed_gateway_state_backups}"| \
    "${allowed_gateway_runtime_backups}") ;;
    *)
      die "refusing unapproved recursive removal path: ${path}"
      return 1
      ;;
  esac
  reject_symlink_path "${path}" || return 1
  [[ ! -e "${path}" ]] || "${RM_BIN}" -rf -- "${path}"
}

managed_gateway_units() {
  local directory file base
  directory="$(path_for /etc/systemd/system)"
  [[ -d "${directory}" && ! -L "${directory}" ]] || return 0
  shopt -s nullglob
  for file in "${directory}"/gost-gateway-exit-*.service; do
    base="${file##*/}"
    if [[ "${base}" =~ ^gost-gateway-exit-([a-z][a-z0-9-]{0,62})\.service$ && -f "${file}" && ! -L "${file}" ]]; then
      printf '%s|%s|%s\n' "${base}" "${file}" "${BASH_REMATCH[1]}"
    fi
  done
  shopt -u nullglob
}

gateway_candidate_services() {
  local record directory file base manifest output line name
  if ! output="$("${SYSTEMCTL_BIN}" list-units --all --type=service --no-legend 'gost-gateway-exit-*.service' 2>/dev/null)"; then
    return 1
  fi
  while IFS= read -r line; do
    name="${line%%[[:space:]]*}"
    [[ "${name}" =~ ^gost-gateway-exit-[a-z][a-z0-9-]{0,62}\.service$ ]] && printf '%s\n' "${name}"
  done <<< "${output}"
  if ! output="$("${SYSTEMCTL_BIN}" list-unit-files --no-legend 'gost-gateway-exit-*.service' 2>/dev/null)"; then
    return 1
  fi
  while IFS= read -r line; do
    name="${line%%[[:space:]]*}"
    [[ "${name}" =~ ^gost-gateway-exit-[a-z][a-z0-9-]{0,62}\.service$ ]] && printf '%s\n' "${name}"
  done <<< "${output}"
  while IFS= read -r record; do
    [[ -n "${record}" ]] || continue
    printf '%s\n' "${record%%|*}"
  done < <(managed_gateway_units)
  directory="$(path_for /etc/gost-manager/generated/gateway/exits)"
  if [[ -d "${directory}" && ! -L "${directory}" ]]; then
    shopt -s nullglob
    for file in "${directory}"/*.env; do
      base="${file##*/}"
      if [[ "${base}" =~ ^([a-z][a-z0-9-]{0,62})\.env$ && -f "${file}" && ! -L "${file}" ]]; then
        printf 'gost-gateway-exit-%s.service\n' "${BASH_REMATCH[1]}"
      fi
    done
    shopt -u nullglob
  fi
  manifest="$(path_for /etc/gost-manager/generated/gateway/runtime.json)"
  if [[ -f "${manifest}" && ! -L "${manifest}" ]]; then
    python3 - "${manifest}" <<'PY' 2>/dev/null || true
import json, re, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
for item in value.get("services", []):
    name = item.get("service_name", "") if isinstance(item, dict) else ""
    if re.fullmatch(r"gost-gateway-exit-[a-z][a-z0-9-]{0,62}\.service", name):
        print(name)
PY
  fi
}

gateway_service_present() {
  local service="$1"
  local unit state
  [[ "${service}" =~ ^gost-gateway-exit-[a-z][a-z0-9-]{0,62}\.service$ ]] || return 1
  unit="$(path_for "/etc/systemd/system/${service}")"
  [[ -e "${unit}" ]] && return 0
  "${SYSTEMCTL_BIN}" is-active --quiet "${service}" >/dev/null 2>&1 && return 0
  "${SYSTEMCTL_BIN}" is-enabled --quiet "${service}" >/dev/null 2>&1 && return 0
  state="$(${SYSTEMCTL_BIN} show "${service}" --property=LoadState --value 2>/dev/null || true)"
  [[ -n "${state}" && "${state}" != "not-found" ]]
}

gateway_runtime_services_exist() {
  local service candidates
  if ! candidates="$(gateway_candidate_services)"; then
    return 0
  fi
  while IFS= read -r service; do
    [[ -n "${service}" ]] || continue
    if gateway_service_present "${service}"; then
      return 0
    fi
  done <<< "$(printf '%s\n' "${candidates}" | LC_ALL=C sort -u)"
  return 1
}

gateway_state_secret_status() {
  local node_file package status
  node_file="$(path_for /etc/gost-manager/node.json)"
  if [[ -L "${node_file}" ]]; then
    printf 'unsafe_or_invalid_state\n'
    return 0
  fi
  if [[ ! -e "${node_file}" ]]; then
    printf 'missing_state\n'
    return 0
  fi
  if [[ ! -f "${node_file}" || ! -r "${node_file}" ]]; then
    printf 'unsafe_or_invalid_state\n'
    return 0
  fi
  package="$(path_for /usr/local/lib/gost-manager)"
  if [[ ! -d "${package}/gateway" || -L "${package}/gateway" ]]; then
    printf 'unsafe_or_invalid_state\n'
    return 0
  fi
  if ! status="$(PYTHONPATH="${package}" python3 - "${node_file}" <<'PY' 2>/dev/null
import pathlib
import sys

from gateway.models import MAX_NODE_BYTES
from gateway.paths import read_regular_file
from gateway.serialization import parse_node

path = pathlib.Path(sys.argv[1])
node = parse_node(read_regular_file(path, MAX_NODE_BYTES, "node state"))
print("references_present" if any(item.secret_ref for item in node.bindings) else "no_references")
PY
)"; then
    printf 'unsafe_or_invalid_state\n'
    return 0
  fi
  case "${status}" in
    references_present|no_references) printf '%s\n' "${status}" ;;
    *) printf 'unsafe_or_invalid_state\n' ;;
  esac
}

managed_traffic_units() {
  local directory file base
  directory="$(path_for /etc/systemd/system)"
  [[ -d "${directory}" && ! -L "${directory}" ]] || return 0
  shopt -s nullglob
  for file in "${directory}"/gost-iran-*.service "${directory}"/gost-kharej-*.service; do
    base="${file##*/}"
    if [[ "${base}" =~ ^gost-(iran|kharej)-[1-9][0-9]*\.service$ && -f "${file}" && ! -L "${file}" ]]; then
      printf '%s|%s\n' "${base}" "${file}"
    fi
  done
  shopt -u nullglob
}

traffic_units_exist() {
  [[ -n "$(managed_traffic_units)" ]]
}

surviving_traffic_services() {
  local record
  while IFS= read -r record; do
    [[ -n "${record}" ]] || continue
    printf '%s\n' "${record%%|*}"
  done < <(managed_traffic_units)
}

monitor_service_loaded() {
  local state
  if ! state="$("${SYSTEMCTL_BIN}" show "${MONITOR_SERVICE}" --property=LoadState --value 2>/dev/null)"; then
    return 0
  fi
  [[ -n "${state}" && "${state}" != "not-found" ]]
}

monitor_service_present() {
  local unit_path
  unit_path="$(path_for /etc/systemd/system/${MONITOR_SERVICE})"
  [[ -e "${unit_path}" ]] && return 0
  "${SYSTEMCTL_BIN}" is-active --quiet "${MONITOR_SERVICE}" >/dev/null 2>&1 && return 0
  "${SYSTEMCTL_BIN}" is-enabled --quiet "${MONITOR_SERVICE}" >/dev/null 2>&1 && return 0
  monitor_service_loaded
}

capture_monitor_config() {
  local config_path value
  config_path="$(path_for /etc/gost-manager/monitoring.env)"
  [[ -f "${config_path}" && ! -L "${config_path}" ]] || return 1
  [[ -x "${MONITOR_ADMIN_BIN}" ]] || return 1
  if ! value="$("${MONITOR_ADMIN_BIN}" config --format value --field database_path --config "${config_path}")"; then
    return 1
  fi
  [[ "${value}" == /var/lib/gost-manager/* && "${value}" != *".."* ]] || return 1
  CONFIGURED_DB_PRODUCTION="${value}"
  CONFIGURED_DB_ACTUAL="$(path_for "${value}")"
  reject_symlink_path "${CONFIGURED_DB_ACTUAL}" || return 1
  CONFIG_CAPTURED=1
}

validate_dependencies() {
  local gateway_secret_status
  if [[ "${REMOVE_MONITOR_CODE}" == "1" || "${REMOVE_MONITOR_CONFIG}" == "1" ]]; then
    if [[ "${REMOVE_MONITOR_SERVICE}" != "1" ]] && monitor_service_present; then
      die "monitoring code/config cannot be removed while the collector service is active, enabled, or loaded."
      return 1
    fi
  fi
  if traffic_units_exist && [[ "${REMOVE_TRAFFIC}" != "1" ]]; then
    if [[ "${REMOVE_CREDENTIALS}" == "1" ]]; then
      die "credentials cannot be removed while managed traffic services remain."
      return 1
    fi
    if [[ "${REMOVE_GOST_BINARY}" == "1" ]]; then
      die "the GOST binary cannot be removed while managed traffic services remain."
      return 1
    fi
  fi
  if [[ "${REMOVE_MONITOR_HISTORY}" == "1" && ! -x "${MONITOR_ADMIN_BIN}" ]]; then
    die "history removal requires the installed monitoring admin to resolve the configured database safely."
    return 1
  fi
  if [[ "${REMOVE_GATEWAY_PACKAGE}" == "1" && "${REMOVE_GATEWAY_RUNTIME}" != "1" ]] && gateway_runtime_services_exist; then
    die "gateway package and runner cannot be removed while gateway Exit services remain."
    return 1
  fi
  if [[ "${REMOVE_GATEWAY_SECRETS}" == "1" && "${REMOVE_GATEWAY_STATE}" != "1" ]] && ! gateway_secret_deletion_allowed; then
    gateway_secret_status="$(gateway_state_secret_status 2>/dev/null || printf 'unsafe_or_invalid_state')"
    case "${gateway_secret_status}" in
      references_present)
        die "gateway secrets cannot be removed while remaining Bindings reference them."
        ;;
      *)
        die "gateway secrets cannot be removed while gateway state is missing or invalid."
        ;;
    esac
    return 1
  fi
}

gateway_secret_deletion_allowed() {
  local status
  if [[ "${REMOVE_GATEWAY_STATE}" == "1" && "${REMOVE_GATEWAY_RUNTIME}" == "1" ]]; then
    return 0
  fi
  status="$(gateway_state_secret_status 2>/dev/null || printf 'unsafe_or_invalid_state')"
  [[ "${status}" == "no_references" ]]
}

yes_no() {
  [[ "$1" == "1" ]] && printf 'YES\n' || printf 'no\n'
}

show_plan() {
  cat <<PLAN
Removal plan
============
Main manager CLI:              $(yes_no "${REMOVE_MANAGER}")
Monitoring service/unit:       $(yes_no "${REMOVE_MONITOR_SERVICE}")
Monitoring code/launchers:     $(yes_no "${REMOVE_MONITOR_CODE}")
Monitoring config:             $(yes_no "${REMOVE_MONITOR_CONFIG}")
Monitoring history:            $(yes_no "${REMOVE_MONITOR_HISTORY}")
Managed traffic services:      $(yes_no "${REMOVE_TRAFFIC}")
/etc/gost credentials/backups: $(yes_no "${REMOVE_CREDENTIALS}")
GOST binary:                   $(yes_no "${REMOVE_GOST_BINARY}")
Gateway runtime/files:         $(yes_no "${REMOVE_GATEWAY_RUNTIME}")
Gateway desired state/backups: $(yes_no "${REMOVE_GATEWAY_STATE}")
Gateway private secrets:       $(yes_no "${REMOVE_GATEWAY_SECRETS}")
Gateway package/launchers:     $(yes_no "${REMOVE_GATEWAY_PACKAGE}")
PLAN
}

collect_plan() {
  local phrase
  confirm "Remove the main gost-manager CLI?" && REMOVE_MANAGER=1
  confirm "Stop and remove the monitoring collector service/unit?" && REMOVE_MONITOR_SERVICE=1
  confirm "Remove monitoring launchers and Python code?" && REMOVE_MONITOR_CODE=1
  confirm "Remove monitoring config under /etc/gost-manager?" && REMOVE_MONITOR_CONFIG=1
  confirm "Delete monitoring history from the configured database?" && REMOVE_MONITOR_HISTORY=1
  confirm "Stop and remove exact managed traffic tunnel services?" && REMOVE_TRAFFIC=1
  if confirm "Delete /etc/gost env files, credentials, and backups?"; then
    read -r -p "Type DELETE GOST CREDENTIALS to confirm: " phrase
    if [[ "${phrase}" == "DELETE GOST CREDENTIALS" ]]; then
      REMOVE_CREDENTIALS=1
    else
      info "Credential deletion cancelled."
    fi
  fi
  confirm "Delete /usr/local/bin/gost?" && REMOVE_GOST_BINARY=1
  confirm "Remove gateway Exit services and generated runtime files?" && REMOVE_GATEWAY_RUNTIME=1
  confirm "Remove gateway desired state and state backups?" && REMOVE_GATEWAY_STATE=1
  if confirm "Delete private gateway secrets?"; then
    read -r -p "Type DELETE GATEWAY SECRETS to confirm: " phrase
    if [[ "${phrase}" == "DELETE GATEWAY SECRETS" ]]; then
      REMOVE_GATEWAY_SECRETS=1
    else
      info "Gateway secret deletion cancelled."
    fi
  fi
  confirm "Remove gateway package, launchers, and runner?" && REMOVE_GATEWAY_PACKAGE=1
  return 0
}

preserve_gateway_dependencies() {
  REMOVE_GATEWAY_STATE=0
  REMOVE_GATEWAY_SECRETS=0
  REMOVE_GATEWAY_PACKAGE=0
  info "Gateway runtime removal was incomplete; state, secrets, package, and runner were preserved."
}

remove_gateway_runtime() {
  local service unit exit_id env_file directory file base
  while IFS= read -r service; do
    [[ -n "${service}" ]] || continue
    exit_id="${service#gost-gateway-exit-}"
    exit_id="${exit_id%.service}"
    unit="$(path_for "/etc/systemd/system/${service}")"
    env_file="$(path_for "/etc/gost-manager/generated/gateway/exits/${exit_id}.env")"
    if ! gateway_service_present "${service}" || "${SYSTEMCTL_BIN}" disable --now "${service}"; then
      remove_file "${unit}" || return 1
      remove_file "${env_file}" || return 1
      UNIT_CHANGED=1
    else
      info "Failed to remove gateway Exit service: ${service}"
      FAILED_ACTIONS=$((FAILED_ACTIONS + 1))
    fi
  done < <(gateway_candidate_services | LC_ALL=C sort -u)
  if gateway_runtime_services_exist; then
    preserve_gateway_dependencies
    return 0
  fi
  directory="$(path_for /etc/gost-manager/generated/gateway/exits)"
  if [[ -d "${directory}" && ! -L "${directory}" ]]; then
    shopt -s nullglob
    for file in "${directory}"/*.env; do
      base="${file##*/}"
      [[ "${base}" =~ ^[a-z][a-z0-9-]{0,62}\.env$ ]] || continue
      remove_file "${file}" || return 1
    done
    shopt -u nullglob
  fi
  remove_file "$(path_for /etc/gost-manager/generated/gateway/runtime.json)" || return 1
  remove_exact_tree "$(path_for /etc/gost-manager/backups/gateway-runtime)" || return 1
  "${RMDIR_BIN}" "$(path_for /etc/gost-manager/generated/gateway/exits)" >/dev/null 2>&1 || true
  "${RMDIR_BIN}" "$(path_for /etc/gost-manager/generated/gateway)" >/dev/null 2>&1 || true
}

remove_gateway_state() {
  remove_file "$(path_for /etc/gost-manager/state.json)" || return 1
  remove_file "$(path_for /etc/gost-manager/node.json)" || return 1
  remove_exact_tree "$(path_for /etc/gost-manager/backups/gateway)" || return 1
}

remove_gateway_secrets() {
  local directory file base
  if gateway_runtime_services_exist; then
    info "Gateway secrets were preserved because a gateway Exit service remains."
    FAILED_ACTIONS=$((FAILED_ACTIONS + 1))
    return 0
  fi
  if ! gateway_secret_deletion_allowed; then
    info "Gateway secrets were preserved because gateway state is missing, invalid, or still references them."
    FAILED_ACTIONS=$((FAILED_ACTIONS + 1))
    return 0
  fi
  directory="$(path_for /etc/gost-manager/secrets)"
  reject_symlink_path "${directory}" || return 1
  if [[ -d "${directory}" ]]; then
    shopt -s nullglob
    for file in "${directory}"/*.env; do
      base="${file##*/}"
      [[ "${base}" =~ ^[a-z][a-z0-9-]{0,63}\.env$ ]] || continue
      remove_file "${file}" || return 1
    done
    shopt -u nullglob
    "${RMDIR_BIN}" "${directory}" >/dev/null 2>&1 || true
  fi
}

remove_gateway_package() {
  if gateway_runtime_services_exist; then
    info "Gateway package and runner were preserved because a gateway Exit service remains."
    FAILED_ACTIONS=$((FAILED_ACTIONS + 1))
    return 0
  fi
  remove_file "$(path_for /usr/local/sbin/gost-gateway)" || return 1
  remove_file "$(path_for /usr/local/sbin/gost-gateway-runtime)" || return 1
  remove_file "$(path_for /usr/local/lib/gost-manager/gost-run-gateway-exit.sh)" || return 1
  remove_exact_tree "$(path_for /usr/local/lib/gost-manager/gateway)" || return 1
}

preserve_monitoring_dependencies() {
  REMOVE_MONITOR_CODE=0
  REMOVE_MONITOR_CONFIG=0
  REMOVE_MONITOR_HISTORY=0
  info "Monitoring service removal failed; code, launchers, config, and history were preserved."
}

remove_monitor_service() {
  local unit_path
  unit_path="$(path_for /etc/systemd/system/${MONITOR_SERVICE})"
  if ! monitor_service_present; then
    return 0
  fi
  if ! "${SYSTEMCTL_BIN}" disable --now "${MONITOR_SERVICE}"; then
    preserve_monitoring_dependencies
    FAILED_ACTIONS=$((FAILED_ACTIONS + 1))
    return 0
  fi
  if "${SYSTEMCTL_BIN}" is-active --quiet "${MONITOR_SERVICE}" >/dev/null 2>&1 || \
     "${SYSTEMCTL_BIN}" is-enabled --quiet "${MONITOR_SERVICE}" >/dev/null 2>&1; then
    preserve_monitoring_dependencies
    FAILED_ACTIONS=$((FAILED_ACTIONS + 1))
    return 0
  fi
  UNIT_CHANGED=1
  if [[ -e "${unit_path}" ]]; then
    if ! remove_file "${unit_path}"; then
      preserve_monitoring_dependencies
      FAILED_ACTIONS=$((FAILED_ACTIONS + 1))
      return 0
    fi
  fi
  if [[ "${UNIT_CHANGED}" == "1" ]]; then
    if ! "${SYSTEMCTL_BIN}" daemon-reload; then
      preserve_monitoring_dependencies
      FAILED_ACTIONS=$((FAILED_ACTIONS + 1))
      return 0
    fi
    UNIT_CHANGED=0
  fi
  if monitor_service_present; then
    preserve_monitoring_dependencies
    FAILED_ACTIONS=$((FAILED_ACTIONS + 1))
  fi
}

remove_monitor_code() {
  if monitor_service_present; then
    info "Collector still exists; monitoring code and launchers were preserved."
    FAILED_ACTIONS=$((FAILED_ACTIONS + 1))
    return 0
  fi
  remove_file "$(path_for /usr/local/sbin/gost-monitor)" || return 1
  monitor_service_present && return 1
  remove_file "$(path_for /usr/local/sbin/gost-monitor-admin)" || return 1
  monitor_service_present && return 1
  remove_file "$(path_for /usr/local/sbin/gost-monitor-collector)" || return 1
  monitor_service_present && return 1
  remove_exact_tree "$(path_for /usr/local/lib/gost-manager/monitoring)" || return 1
}

remove_monitor_config() {
  local directory
  if monitor_service_present; then
    info "Collector still exists; monitoring config was preserved."
    FAILED_ACTIONS=$((FAILED_ACTIONS + 1))
    return 0
  fi
  remove_file "$(path_for /etc/gost-manager/monitoring.env)" || return 1
  directory="$(path_for /etc/gost-manager)"
  if [[ -d "${directory}" && ! -L "${directory}" ]]; then
    "${RMDIR_BIN}" "${directory}" >/dev/null 2>&1 || true
  fi
}

remove_configured_history_files() {
  local state_root directory candidate
  if [[ "${CONFIG_CAPTURED}" != "1" ]]; then
    die "configured monitoring database was not captured; history was preserved."
    return 1
  fi
  for candidate in "${CONFIGURED_DB_ACTUAL}" "${CONFIGURED_DB_ACTUAL}-wal" "${CONFIGURED_DB_ACTUAL}-shm"; do
    remove_file "${candidate}" || return 1
  done
  state_root="$(path_for /var/lib/gost-manager)"
  directory="${CONFIGURED_DB_ACTUAL%/*}"
  while [[ "${directory}" == "${state_root}"/* ]]; do
    "${RMDIR_BIN}" "${directory}" >/dev/null 2>&1 || break
    directory="${directory%/*}"
  done
  "${RMDIR_BIN}" "${state_root}" >/dev/null 2>&1 || true
}

delete_monitor_history() {
  local was_active=0 status=0 config_path
  if [[ "${CONFIG_CAPTURED}" != "1" ]]; then
    die "configured monitoring database could not be resolved; history was preserved."
    return 1
  fi
  if ! monitor_service_present; then
    remove_configured_history_files || return 1
    return 0
  fi
  if "${SYSTEMCTL_BIN}" is-active --quiet "${MONITOR_SERVICE}"; then
    was_active=1
    if ! "${SYSTEMCTL_BIN}" stop "${MONITOR_SERVICE}"; then
      info "Collector could not be stopped; history was preserved."
      FAILED_ACTIONS=$((FAILED_ACTIONS + 1))
      return 0
    fi
  fi
  config_path="$(path_for /etc/gost-manager/monitoring.env)"
  if ! "${MONITOR_ADMIN_BIN}" purge-history --yes --db "${CONFIGURED_DB_PRODUCTION}" --config "${config_path}"; then
    info "History purge failed; original configured monitoring database was preserved."
    status=1
    FAILED_ACTIONS=$((FAILED_ACTIONS + 1))
  fi
  if [[ "${was_active}" == "1" ]]; then
    "${SYSTEMCTL_BIN}" start "${MONITOR_SERVICE}" || {
      info "Collector restart failed after history operation."
      FAILED_ACTIONS=$((FAILED_ACTIONS + 1))
    }
  fi
  return "${status}"
}

remove_traffic_services() {
  local record service file survivors
  while IFS= read -r record; do
    [[ -n "${record}" ]] || continue
    service="${record%%|*}"
    file="${record#*|}"
    if "${SYSTEMCTL_BIN}" disable --now "${service}"; then
      remove_file "${file}" || return 1
      UNIT_CHANGED=1
    else
      info "Failed to remove managed traffic service: ${service}"
      FAILED_ACTIONS=$((FAILED_ACTIONS + 1))
    fi
  done < <(managed_traffic_units)
  if traffic_units_exist; then
    survivors="$(surviving_traffic_services)"
    info "Managed traffic remains; all runners, credentials, and the GOST binary were preserved."
    info "Surviving managed services: ${survivors//$'\n'/, }"
    return 0
  fi
  if ! traffic_units_exist; then
    remove_file "$(path_for /usr/local/lib/gost-manager/gost-run-iran.sh)" || return 1
  fi
  if ! traffic_units_exist; then
    remove_file "$(path_for /usr/local/lib/gost-manager/gost-run-kharej.sh)" || return 1
  fi
}

remove_selected_traffic_dependencies() {
  if traffic_units_exist; then
    [[ "${REMOVE_CREDENTIALS}" != "1" ]] || info "Credentials were selected but preserved because managed traffic remains."
    [[ "${REMOVE_GOST_BINARY}" != "1" ]] || info "GOST binary was selected but preserved because managed traffic remains."
    return 0
  fi
  if [[ "${REMOVE_CREDENTIALS}" == "1" ]]; then
    if ! traffic_units_exist; then
      remove_exact_tree "$(path_for /etc/gost)" || return 1
    fi
  fi
  if [[ "${REMOVE_GOST_BINARY}" == "1" ]]; then
    if ! traffic_units_exist; then
      remove_file "$(path_for /usr/local/bin/gost)" || return 1
    fi
  fi
}

apply_plan() {
  local failures_before
  validate_dependencies || return 1
  if [[ "${REMOVE_MONITOR_SERVICE}" == "1" || "${REMOVE_MONITOR_CODE}" == "1" || "${REMOVE_MONITOR_CONFIG}" == "1" || "${REMOVE_MONITOR_HISTORY}" == "1" ]]; then
    if ! capture_monitor_config && [[ "${REMOVE_MONITOR_HISTORY}" == "1" ]]; then
      die "monitoring config is missing or invalid; refusing to guess a database path for history removal."
      return 1
    fi
  fi
  if [[ "${REMOVE_MANAGER}" == "1" ]]; then
    remove_file "$(path_for /usr/local/sbin/gost-manager)" || return 1
  fi
  if [[ "${REMOVE_MONITOR_SERVICE}" == "1" ]]; then
    remove_monitor_service || return 1
  fi
  if [[ "${REMOVE_MONITOR_HISTORY}" == "1" ]]; then
    failures_before="${FAILED_ACTIONS}"
    if ! delete_monitor_history; then
      [[ "${FAILED_ACTIONS}" -gt "${failures_before}" ]] || FAILED_ACTIONS=$((FAILED_ACTIONS + 1))
      REMOVE_MONITOR_CODE=0
      REMOVE_MONITOR_CONFIG=0
      info "History removal failed; monitoring code and config were preserved for recovery."
    fi
  fi
  if [[ "${REMOVE_MONITOR_CODE}" == "1" ]]; then
    remove_monitor_code || return 1
  fi
  if [[ "${REMOVE_MONITOR_CONFIG}" == "1" ]]; then
    remove_monitor_config || return 1
  fi
  if [[ "${REMOVE_TRAFFIC}" == "1" ]]; then
    remove_traffic_services || return 1
  fi
  if [[ "${REMOVE_GATEWAY_RUNTIME}" == "1" ]]; then
    remove_gateway_runtime || return 1
  fi
  if [[ "${REMOVE_GATEWAY_STATE}" == "1" ]]; then
    remove_gateway_state || return 1
  fi
  if [[ "${REMOVE_GATEWAY_SECRETS}" == "1" ]]; then
    remove_gateway_secrets || return 1
  fi
  if [[ "${REMOVE_GATEWAY_PACKAGE}" == "1" ]]; then
    remove_gateway_package || return 1
  fi
  remove_selected_traffic_dependencies || return 1
  if [[ "${UNIT_CHANGED}" == "1" ]]; then
    "${SYSTEMCTL_BIN}" daemon-reload || {
      info "systemd daemon-reload failed."
      FAILED_ACTIONS=$((FAILED_ACTIONS + 1))
    }
  fi
  if [[ "${REMOVE_MONITOR_HISTORY}" != "1" ]]; then
    if [[ "${CONFIG_CAPTURED}" == "1" ]]; then
      info "Monitoring history retained at ${CONFIGURED_DB_PRODUCTION}."
    elif [[ -d "$(path_for /var/lib/gost-manager)" ]]; then
      info "Monitoring history retained; its path could not be resolved from config."
    fi
  fi
  if [[ "${REMOVE_MONITOR_CONFIG}" != "1" && -d "$(path_for /etc/gost-manager)" ]]; then
    info "Monitoring config retained at $(path_for /etc/gost-manager)."
  fi
  if [[ "${FAILED_ACTIONS}" -ne 0 ]]; then
    info "Uninstall completed with ${FAILED_ACTIONS} failed action(s)."
    return 1
  fi
  info "Selected GOST Manager components removed."
}

main() {
  require_safe_root
  collect_plan
  show_plan
  if [[ "${REMOVE_MANAGER}${REMOVE_MONITOR_SERVICE}${REMOVE_MONITOR_CODE}${REMOVE_MONITOR_CONFIG}${REMOVE_MONITOR_HISTORY}${REMOVE_TRAFFIC}${REMOVE_CREDENTIALS}${REMOVE_GOST_BINARY}${REMOVE_GATEWAY_RUNTIME}${REMOVE_GATEWAY_STATE}${REMOVE_GATEWAY_SECRETS}${REMOVE_GATEWAY_PACKAGE}" == "000000000000" ]]; then
    info "No components selected; nothing changed."
    return 0
  fi
  confirm "Apply this exact removal plan?" || die "uninstall aborted."
  apply_plan
}

if [[ "${GOST_MANAGER_SOURCE_ONLY}" != "1" ]]; then
  main "$@"
fi
