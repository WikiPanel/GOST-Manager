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

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
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
  for part in "${parts[@]}"; do
    [[ -n "${part}" ]] || continue
    current="${current%/}/${part}"
    [[ ! -L "${current}" ]] || die "managed removal path may not traverse a symlink: ${current}"
  done
  if [[ -n "${GOST_MANAGER_ROOT}" ]]; then
    [[ "${path}" == "${GOST_MANAGER_ROOT}"/* ]] || die "managed removal path escaped the testing root: ${path}"
  fi
}

remove_file() {
  local path="$1"
  reject_symlink_path "${path}"
  [[ ! -e "${path}" ]] || "${RM_BIN}" -f "${path}"
}

remove_exact_tree() {
  local path="$1"
  local allowed_state allowed_config allowed_credentials allowed_package
  allowed_state="$(path_for /var/lib/gost-manager)"
  allowed_config="$(path_for /etc/gost-manager)"
  allowed_credentials="$(path_for /etc/gost)"
  allowed_package="$(path_for /usr/local/lib/gost-manager/monitoring)"
  case "${path}" in
    "${allowed_state}"|"${allowed_config}"|"${allowed_credentials}"|"${allowed_package}") ;;
    *) die "refusing unapproved recursive removal path: ${path}" ;;
  esac
  reject_symlink_path "${path}"
  [[ ! -e "${path}" ]] || "${RM_BIN}" -rf -- "${path}"
}

managed_traffic_units() {
  local directory file base
  directory="$(path_for /etc/systemd/system)"
  [[ -d "${directory}" && ! -L "${directory}" ]] || return 0
  shopt -s nullglob
  for file in "${directory}"/gost-iran-*.service "${directory}"/gost-kharej-*.service; do
    base="$(basename "${file}")"
    if [[ "${base}" =~ ^gost-(iran|kharej)-[1-9][0-9]*\.service$ && -f "${file}" && ! -L "${file}" ]]; then
      printf '%s|%s\n' "${base}" "${file}"
    fi
  done
  shopt -u nullglob
}

traffic_units_exist() {
  [[ -n "$(managed_traffic_units)" ]]
}

validate_dependencies() {
  local unit_path
  unit_path="$(path_for /etc/systemd/system/${MONITOR_SERVICE})"
  if [[ "${REMOVE_MONITOR_CODE}" == "1" && "${REMOVE_MONITOR_SERVICE}" != "1" && -e "${unit_path}" ]]; then
    die "monitoring code cannot be removed while the collector service remains."
  fi
  if [[ "${REMOVE_MONITOR_CONFIG}" == "1" && "${REMOVE_MONITOR_SERVICE}" != "1" && -e "${unit_path}" ]]; then
    die "monitoring config cannot be removed while the collector service remains."
  fi
  if [[ "${REMOVE_CREDENTIALS}" == "1" && "${REMOVE_TRAFFIC}" != "1" ]] && traffic_units_exist; then
    die "credentials cannot be removed while managed traffic services remain."
  fi
  if [[ "${REMOVE_MONITOR_HISTORY}" == "1" && "${REMOVE_MONITOR_SERVICE}" != "1" && ! -x "${MONITOR_ADMIN_BIN}" ]]; then
    die "history purge requires the installed monitoring admin while the collector remains."
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
  confirm "Delete monitoring history under /var/lib/gost-manager?" && REMOVE_MONITOR_HISTORY=1
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

remove_monitor_service() {
  local unit_path
  unit_path="$(path_for /etc/systemd/system/${MONITOR_SERVICE})"
  if [[ -e "${unit_path}" ]]; then
    if ! "${SYSTEMCTL_BIN}" disable --now "${MONITOR_SERVICE}"; then
      info "Failed to stop/remove monitoring service; dependent monitoring files were preserved."
      REMOVE_MONITOR_SERVICE=0
      REMOVE_MONITOR_CODE=0
      REMOVE_MONITOR_CONFIG=0
      FAILED_ACTIONS=$((FAILED_ACTIONS + 1))
      return 0
    fi
    remove_file "${unit_path}"
    UNIT_CHANGED=1
  fi
}

remove_monitor_code() {
  remove_file "$(path_for /usr/local/sbin/gost-monitor)"
  remove_file "$(path_for /usr/local/sbin/gost-monitor-admin)"
  remove_file "$(path_for /usr/local/sbin/gost-monitor-collector)"
  remove_exact_tree "$(path_for /usr/local/lib/gost-manager/monitoring)"
}

remove_monitor_config() {
  local directory
  remove_file "$(path_for /etc/gost-manager/monitoring.env)"
  directory="$(path_for /etc/gost-manager)"
  if [[ -d "${directory}" && ! -L "${directory}" ]]; then
    "${RMDIR_BIN}" "${directory}" >/dev/null 2>&1 || true
  fi
}

delete_monitor_history() {
  local state_dir was_active=0 status=0
  state_dir="$(path_for /var/lib/gost-manager)"
  if [[ "${REMOVE_MONITOR_SERVICE}" == "1" ]]; then
    remove_exact_tree "${state_dir}"
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
  if ! "${MONITOR_ADMIN_BIN}" purge-history --yes --db "${state_dir}/metrics.sqlite3"; then
    info "History purge failed; original monitoring database was preserved."
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
  local record service file
  while IFS= read -r record; do
    [[ -n "${record}" ]] || continue
    service="${record%%|*}"
    file="${record#*|}"
    if "${SYSTEMCTL_BIN}" disable --now "${service}"; then
      remove_file "${file}"
      UNIT_CHANGED=1
    else
      info "Failed to remove managed traffic service: ${service}"
      FAILED_ACTIONS=$((FAILED_ACTIONS + 1))
    fi
  done < <(managed_traffic_units)
  if ! traffic_units_exist; then
    remove_file "$(path_for /usr/local/lib/gost-manager/gost-run-iran.sh)"
    remove_file "$(path_for /usr/local/lib/gost-manager/gost-run-kharej.sh)"
  else
    info "Managed traffic remains; runner scripts were preserved."
  fi
}

apply_plan() {
  validate_dependencies
  [[ "${REMOVE_MANAGER}" == "1" ]] && remove_file "$(path_for /usr/local/sbin/gost-manager)"
  [[ "${REMOVE_MONITOR_SERVICE}" == "1" ]] && remove_monitor_service
  [[ "${REMOVE_MONITOR_CODE}" == "1" ]] && remove_monitor_code
  [[ "${REMOVE_MONITOR_CONFIG}" == "1" ]] && remove_monitor_config
  [[ "${REMOVE_MONITOR_HISTORY}" == "1" ]] && delete_monitor_history || true
  [[ "${REMOVE_TRAFFIC}" == "1" ]] && remove_traffic_services
  [[ "${REMOVE_CREDENTIALS}" == "1" ]] && remove_exact_tree "$(path_for /etc/gost)"
  [[ "${REMOVE_GOST_BINARY}" == "1" ]] && remove_file "$(path_for /usr/local/bin/gost)"
  if [[ "${UNIT_CHANGED}" == "1" ]]; then
    "${SYSTEMCTL_BIN}" daemon-reload || {
      info "systemd daemon-reload failed."
      FAILED_ACTIONS=$((FAILED_ACTIONS + 1))
    }
  fi
  if [[ "${REMOVE_MONITOR_HISTORY}" != "1" && -d "$(path_for /var/lib/gost-manager)" ]]; then
    info "Monitoring history retained at $(path_for /var/lib/gost-manager)."
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
