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
  local allowed_config allowed_credentials allowed_package
  allowed_config="$(path_for /etc/gost-manager)"
  allowed_credentials="$(path_for /etc/gost)"
  allowed_package="$(path_for /usr/local/lib/gost-manager/monitoring)"
  case "${path}" in
    "${allowed_config}"|"${allowed_credentials}"|"${allowed_package}") ;;
    *)
      die "refusing unapproved recursive removal path: ${path}"
      return 1
      ;;
  esac
  reject_symlink_path "${path}" || return 1
  [[ ! -e "${path}" ]] || "${RM_BIN}" -rf -- "${path}"
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
  return 0
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
    remove_file "$(path_for /usr/local/lib/gost-manager/VERSION)" || return 1
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
  if [[ "${REMOVE_MANAGER}${REMOVE_MONITOR_SERVICE}${REMOVE_MONITOR_CODE}${REMOVE_MONITOR_CONFIG}${REMOVE_MONITOR_HISTORY}${REMOVE_TRAFFIC}${REMOVE_CREDENTIALS}${REMOVE_GOST_BINARY}" == "00000000" ]]; then
    info "No components selected; nothing changed."
    return 0
  fi
  confirm "Apply this exact removal plan?" || die "uninstall aborted."
  apply_plan
}

if [[ "${GOST_MANAGER_SOURCE_ONLY}" != "1" ]]; then
  main "$@"
fi
