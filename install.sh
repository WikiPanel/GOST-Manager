#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GOST_MANAGER_ROOT="${GOST_MANAGER_ROOT:-}"
GOST_MANAGER_TESTING="${GOST_MANAGER_TESTING:-0}"
GOST_MANAGER_INSTALL_DEPS="${GOST_MANAGER_INSTALL_DEPS:-0}"
GOST_MANAGER_ALLOW_TEST_PACKAGE_STUB="${GOST_MANAGER_ALLOW_TEST_PACKAGE_STUB:-0}"
GOST_MANAGER_FAIL_PHASE="${GOST_MANAGER_FAIL_PHASE:-}"

SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
SYSTEMD_ANALYZE_BIN="${SYSTEMD_ANALYZE_BIN:-systemd-analyze}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
INSTALL_BIN="${INSTALL_BIN:-install}"
CP_BIN="${CP_BIN:-cp}"
MV_BIN="${MV_BIN:-mv}"
RM_BIN="${RM_BIN:-rm}"
CHMOD_BIN="${CHMOD_BIN:-chmod}"
CHOWN_BIN="${CHOWN_BIN:-chown}"
SYNC_BIN="${SYNC_BIN:-sync}"
CMP_BIN="${CMP_BIN:-cmp}"
APT_GET_BIN="${APT_GET_BIN:-apt-get}"

MONITOR_SERVICE="gost-monitor-collector.service"
STAGE_DIR=""
INSTALL_COMMITTED=0
SERVICE_STATE_RECORDED=0
UNIT_CHANGED=0
PREVIOUS_UNIT_EXISTED=0
PREVIOUS_ENABLED=0
PREVIOUS_ACTIVE=0
FILES_CHANGED=0
DATABASE_CREATED=0
declare -a CHANGED_DESTINATIONS=()
declare -a BACKUP_PATHS=()
declare -a BACKUP_CONTAINERS=()
declare -a CREATED_DIRECTORIES=()
declare -a PENDING_PATHS=()

die() {
  printf 'Error: %s\n' "$*" >&2
  return 1
}

info() {
  printf '%s\n' "$*"
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

inject_failure() {
  local phase="$1"
  if [[ "${GOST_MANAGER_FAIL_PHASE}" == "${phase}" ]]; then
    die "injected installer failure at phase: ${phase}"
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
  [[ "${EUID}" -eq 0 ]] || die "install.sh must be run as root. Try: sudo bash install.sh"
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
    [[ ! -L "${current}" ]] || die "managed path may not traverse a symlink: ${current}"
  done
  if [[ -n "${GOST_MANAGER_ROOT}" ]]; then
    [[ "${path}" == "${GOST_MANAGER_ROOT}"/* ]] || die "managed path escaped the testing root: ${path}"
  fi
}

parse_arguments() {
  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      --install-dependencies) GOST_MANAGER_INSTALL_DEPS=1 ;;
      *) die "unknown installer option: $1" ;;
    esac
    shift
  done
}

package_for_command() {
  case "$1" in
    python3) printf 'python3\n' ;;
    systemctl) printf 'systemd\n' ;;
    ss) printf 'iproute2\n' ;;
    *) printf 'coreutils\n' ;;
  esac
}

validate_dependencies() {
  local command_name package
  local -a required=(python3 systemctl ss install cp mv rm chmod chown sync cmp mktemp)
  local -a missing=()
  local -a packages=()
  for command_name in "${required[@]}"; do
    if ! command -v "${command_name}" >/dev/null 2>&1; then
      missing+=("${command_name}")
      package="$(package_for_command "${command_name}")"
      if [[ "${#packages[@]}" -eq 0 ]]; then
        packages+=("${package}")
      elif [[ " ${packages[*]} " != *" ${package} "* ]]; then
        packages+=("${package}")
      fi
    fi
  done
  if [[ "${#missing[@]}" -eq 0 ]]; then
    return 0
  fi
  info "Missing required commands: ${missing[*]}"
  info "Suggested packages: ${packages[*]}"
  [[ "${GOST_MANAGER_INSTALL_DEPS}" == "1" ]] || die "dependencies are missing; rerun with --install-dependencies to opt in."
  if [[ "${GOST_MANAGER_TESTING}" == "1" && "${GOST_MANAGER_ALLOW_TEST_PACKAGE_STUB}" != "1" ]]; then
    die "package installation is disabled in testing mode."
  fi
  command -v "${APT_GET_BIN}" >/dev/null 2>&1 || die "apt-get is required for dependency installation."
  info "Installing only: ${packages[*]}"
  "${APT_GET_BIN}" update
  "${APT_GET_BIN}" install -y "${packages[@]}"
  for command_name in "${missing[@]}"; do
    command -v "${command_name}" >/dev/null 2>&1 || die "dependency installation did not provide: ${command_name}"
  done
}

validate_source_manifest() {
  local path
  local -a fixed=(
    "gost-manager.sh"
    "lib/gost-run-iran.sh"
    "lib/gost-run-kharej.sh"
    "packaging/gost-monitor"
    "packaging/gost-monitor-admin"
    "packaging/gost-monitor-collector"
    "packaging/gost-monitor-collector.service"
    "packaging/monitoring.env"
    "monitoring/__init__.py"
  )
  for path in "${fixed[@]}"; do
    [[ -f "${SCRIPT_DIR}/${path}" && ! -L "${SCRIPT_DIR}/${path}" ]] || die "required source file is missing or unsafe: ${path}"
  done
  shopt -s nullglob
  local -a modules=("${SCRIPT_DIR}"/monitoring/*.py)
  shopt -u nullglob
  [[ "${#modules[@]}" -gt 1 ]] || die "complete monitoring Python package is missing."
}

stage_sources() {
  local path
  STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gost-manager-install.XXXXXX")"
  "${CHMOD_BIN}" 700 "${STAGE_DIR}"
  "${INSTALL_BIN}" -d -m 755 "${STAGE_DIR}/lib" "${STAGE_DIR}/monitoring" "${STAGE_DIR}/sbin"
  "${CP_BIN}" "${SCRIPT_DIR}/gost-manager.sh" "${STAGE_DIR}/gost-manager"
  "${CP_BIN}" "${SCRIPT_DIR}/lib/gost-run-iran.sh" "${STAGE_DIR}/lib/gost-run-iran.sh"
  "${CP_BIN}" "${SCRIPT_DIR}/lib/gost-run-kharej.sh" "${STAGE_DIR}/lib/gost-run-kharej.sh"
  for path in "${SCRIPT_DIR}"/monitoring/*.py; do
    "${CP_BIN}" "${path}" "${STAGE_DIR}/monitoring/$(basename "${path}")"
  done
  "${CP_BIN}" "${SCRIPT_DIR}/packaging/gost-monitor" "${STAGE_DIR}/sbin/gost-monitor"
  "${CP_BIN}" "${SCRIPT_DIR}/packaging/gost-monitor-admin" "${STAGE_DIR}/sbin/gost-monitor-admin"
  "${CP_BIN}" "${SCRIPT_DIR}/packaging/gost-monitor-collector" "${STAGE_DIR}/sbin/gost-monitor-collector"
  "${CP_BIN}" "${SCRIPT_DIR}/packaging/gost-monitor-collector.service" "${STAGE_DIR}/gost-monitor-collector.service"
  "${CP_BIN}" "${SCRIPT_DIR}/packaging/monitoring.env" "${STAGE_DIR}/monitoring.env"
}

validate_staged_bash() {
  bash -n \
    "${STAGE_DIR}/gost-manager" \
    "${STAGE_DIR}/lib/gost-run-iran.sh" \
    "${STAGE_DIR}/lib/gost-run-kharej.sh" \
    "${STAGE_DIR}/sbin/gost-monitor" \
    "${STAGE_DIR}/sbin/gost-monitor-admin" \
    "${STAGE_DIR}/sbin/gost-monitor-collector"
}

validate_staged_python() {
  PYTHONPYCACHEPREFIX="${STAGE_DIR}/pycache" "${PYTHON_BIN}" -m py_compile "${STAGE_DIR}"/monitoring/*.py
}

validate_config_file() {
  local config_path="$1"
  [[ ! -L "${config_path}" ]] || die "monitoring config may not be a symlink."
  PYTHONPATH="${STAGE_DIR}" "${PYTHON_BIN}" -m monitoring.admin_cli validate-config --config "${config_path}" >/dev/null
}

validate_staged_config() {
  local installed_config
  validate_config_file "${STAGE_DIR}/monitoring.env"
  installed_config="$(path_for /etc/gost-manager/monitoring.env)"
  if [[ -e "${installed_config}" || -L "${installed_config}" ]]; then
    reject_symlink_path "${installed_config}"
    validate_config_file "${installed_config}" || die "existing monitoring config is invalid and was not replaced."
  fi
}

validate_unit_content() {
  local unit="${STAGE_DIR}/gost-monitor-collector.service"
  local forbidden unit_root
  for forbidden in 'Requires=' 'PartOf=' 'BindsTo=' 'PrivateNetwork=' 'ProtectProc=' 'ProcSubset=' 'InaccessiblePaths=/proc'; do
    if grep -Fq "${forbidden}" "${unit}"; then
      die "monitoring unit contains forbidden setting: ${forbidden}"
    fi
  done
  grep -Fq 'Restart=on-failure' "${unit}" || die "monitoring unit lacks bounded restart policy."
  grep -Fq 'UMask=0077' "${unit}" || die "monitoring unit lacks private umask."
  grep -Fq 'StateDirectoryMode=0700' "${unit}" || die "monitoring unit lacks private state mode."
  if command -v "${SYSTEMD_ANALYZE_BIN}" >/dev/null 2>&1; then
    unit_root="${STAGE_DIR}/unit-root"
    "${INSTALL_BIN}" -d -m 755 \
      "${unit_root}/etc/systemd/system" \
      "${unit_root}/etc/gost-manager" \
      "${unit_root}/usr/local/sbin" \
      "${unit_root}/usr/local/lib/gost-manager"
    "${CP_BIN}" "${unit}" "${unit_root}/etc/systemd/system/${MONITOR_SERVICE}"
    "${CP_BIN}" "${STAGE_DIR}/monitoring.env" "${unit_root}/etc/gost-manager/monitoring.env"
    "${CP_BIN}" "${STAGE_DIR}/sbin/gost-monitor-admin" "${unit_root}/usr/local/sbin/gost-monitor-admin"
    "${CP_BIN}" "${STAGE_DIR}/sbin/gost-monitor-collector" "${unit_root}/usr/local/sbin/gost-monitor-collector"
    "${CP_BIN}" -R "${STAGE_DIR}/monitoring" "${unit_root}/usr/local/lib/gost-manager/monitoring"
    "${SYSTEMD_ANALYZE_BIN}" --root="${unit_root}" verify "${MONITOR_SERVICE}"
  else
    info "systemd-analyze unavailable; deterministic unit validation passed."
  fi
}

record_service_state() {
  local unit_path
  unit_path="$(path_for /etc/systemd/system/${MONITOR_SERVICE})"
  [[ -e "${unit_path}" ]] && PREVIOUS_UNIT_EXISTED=1
  if "${SYSTEMCTL_BIN}" is-enabled --quiet "${MONITOR_SERVICE}" >/dev/null 2>&1; then
    PREVIOUS_ENABLED=1
  fi
  if "${SYSTEMCTL_BIN}" is-active --quiet "${MONITOR_SERVICE}" >/dev/null 2>&1; then
    PREVIOUS_ACTIVE=1
  fi
  SERVICE_STATE_RECORDED=1
}

ensure_directory() {
  local path="$1"
  local mode="$2"
  reject_symlink_path "${path}"
  if [[ ! -d "${path}" ]]; then
    "${INSTALL_BIN}" -d -m "${mode}" "${path}"
    CREATED_DIRECTORIES+=("${path}")
  else
    "${CHMOD_BIN}" "${mode}" "${path}"
  fi
  "${CHOWN_BIN}" root:root "${path}"
}

install_managed_file() {
  local source="$1"
  local destination="$2"
  local mode="$3"
  local candidate backup backup_container
  reject_symlink_path "${destination}"
  if [[ -f "${destination}" ]] && "${CMP_BIN}" -s "${source}" "${destination}"; then
    "${CHMOD_BIN}" "${mode}" "${destination}"
    "${CHOWN_BIN}" root:root "${destination}"
    return 0
  fi
  candidate="${destination}.gost-manager-new.$$"
  backup_container="${destination}.gost-manager-backup.$$"
  PENDING_PATHS+=("${candidate}")
  "${INSTALL_BIN}" -m "${mode}" "${source}" "${candidate}"
  "${CHOWN_BIN}" root:root "${candidate}"
  if [[ -e "${destination}" ]]; then
    "${INSTALL_BIN}" -d -m 700 "${backup_container}"
    "${CHOWN_BIN}" root:root "${backup_container}"
    backup="${backup_container}/original"
    "${CP_BIN}" -p "${destination}" "${backup}"
  else
    backup=""
    backup_container=""
  fi
  CHANGED_DESTINATIONS+=("${destination}")
  BACKUP_PATHS+=("${backup}")
  BACKUP_CONTAINERS+=("${backup_container}")
  inject_failure backup
  "${MV_BIN}" "${candidate}" "${destination}"
  FILES_CHANGED=1
}

install_monitoring_package() {
  local destination candidate backup backup_container path
  destination="$(path_for /usr/local/lib/gost-manager/monitoring)"
  reject_symlink_path "${destination}"
  candidate="${destination}.gost-manager-new.$$"
  backup_container="${destination}.gost-manager-backup.$$"
  PENDING_PATHS+=("${candidate}")
  "${INSTALL_BIN}" -d -m 755 "${candidate}"
  for path in "${STAGE_DIR}"/monitoring/*.py; do
    "${INSTALL_BIN}" -m 644 "${path}" "${candidate}/$(basename "${path}")"
  done
  "${CHOWN_BIN}" -R root:root "${candidate}"
  if [[ -d "${destination}" ]]; then
    "${INSTALL_BIN}" -d -m 700 "${backup_container}"
    "${CHOWN_BIN}" root:root "${backup_container}"
    backup="${backup_container}/original"
    "${MV_BIN}" "${destination}" "${backup}"
  elif [[ -e "${destination}" ]]; then
    die "monitoring package destination is not a directory."
  else
    backup=""
    backup_container=""
  fi
  CHANGED_DESTINATIONS+=("${destination}")
  BACKUP_PATHS+=("${backup}")
  BACKUP_CONTAINERS+=("${backup_container}")
  "${MV_BIN}" "${candidate}" "${destination}"
  FILES_CHANGED=1
}

install_files() {
  local config_path unit_path
  ensure_directory "$(path_for /usr/local/sbin)" 755
  ensure_directory "$(path_for /usr/local/lib/gost-manager)" 755
  ensure_directory "$(path_for /etc/gost)" 700
  ensure_directory "$(path_for /etc/gost-manager)" 700
  ensure_directory "$(path_for /etc/systemd/system)" 755
  ensure_directory "$(path_for /var/lib/gost-manager)" 700

  install_managed_file "${STAGE_DIR}/gost-manager" "$(path_for /usr/local/sbin/gost-manager)" 755
  install_managed_file "${STAGE_DIR}/lib/gost-run-iran.sh" "$(path_for /usr/local/lib/gost-manager/gost-run-iran.sh)" 755
  install_managed_file "${STAGE_DIR}/lib/gost-run-kharej.sh" "$(path_for /usr/local/lib/gost-manager/gost-run-kharej.sh)" 755
  install_monitoring_package
  install_managed_file "${STAGE_DIR}/sbin/gost-monitor" "$(path_for /usr/local/sbin/gost-monitor)" 755
  install_managed_file "${STAGE_DIR}/sbin/gost-monitor-admin" "$(path_for /usr/local/sbin/gost-monitor-admin)" 755
  install_managed_file "${STAGE_DIR}/sbin/gost-monitor-collector" "$(path_for /usr/local/sbin/gost-monitor-collector)" 755

  config_path="$(path_for /etc/gost-manager/monitoring.env)"
  if [[ ! -e "${config_path}" ]]; then
    install_managed_file "${STAGE_DIR}/monitoring.env" "${config_path}" 600
  fi
  "${CHMOD_BIN}" 600 "${config_path}"
  "${CHOWN_BIN}" root:root "${config_path}"

  unit_path="$(path_for /etc/systemd/system/${MONITOR_SERVICE})"
  local before_count="${#CHANGED_DESTINATIONS[@]}"
  install_managed_file "${STAGE_DIR}/gost-monitor-collector.service" "${unit_path}" 644
  if [[ "${#CHANGED_DESTINATIONS[@]}" -gt "${before_count}" ]]; then
    UNIT_CHANGED=1
  fi
}

migrate_database() {
  local library db_path
  library="$(path_for /usr/local/lib/gost-manager)"
  db_path="$(path_for /var/lib/gost-manager/metrics.sqlite3)"
  if [[ ! -e "${db_path}" ]]; then
    DATABASE_CREATED=1
  fi
  PYTHONPATH="${library}" "${PYTHON_BIN}" -m monitoring.admin_cli migrate --db "${db_path}" >/dev/null
  "${CHMOD_BIN}" 600 "${db_path}"
  "${CHOWN_BIN}" root:root "${db_path}"
}

activate_collector() {
  if [[ "${UNIT_CHANGED}" == "1" ]]; then
    "${SYSTEMCTL_BIN}" daemon-reload
  fi
  inject_failure daemon_reload
  migrate_database
  inject_failure migration
  if [[ "${PREVIOUS_UNIT_EXISTED}" == "0" ]]; then
    "${SYSTEMCTL_BIN}" enable --now "${MONITOR_SERVICE}"
  elif [[ "${PREVIOUS_ACTIVE}" == "1" && "${FILES_CHANGED}" == "1" ]]; then
    "${SYSTEMCTL_BIN}" restart "${MONITOR_SERVICE}"
  fi
  inject_failure collector_start
}

rollback_files() {
  local index destination backup backup_container
  for ((index=${#CHANGED_DESTINATIONS[@]}-1; index>=0; index--)); do
    destination="${CHANGED_DESTINATIONS[index]}"
    backup="${BACKUP_PATHS[index]}"
    backup_container="${BACKUP_CONTAINERS[index]}"
    if [[ -n "${backup}" && -e "${backup}" ]]; then
      "${RM_BIN}" -rf "${destination}"
      "${MV_BIN}" "${backup}" "${destination}"
    else
      "${RM_BIN}" -rf "${destination}"
    fi
    if [[ -n "${backup_container}" && -e "${backup_container}" ]]; then
      "${RM_BIN}" -rf "${backup_container}"
    fi
  done
}

restore_service_state() {
  "${SYSTEMCTL_BIN}" daemon-reload >/dev/null 2>&1 || true
  if [[ "${PREVIOUS_UNIT_EXISTED}" == "0" ]]; then
    "${SYSTEMCTL_BIN}" disable --now "${MONITOR_SERVICE}" >/dev/null 2>&1 || true
    return 0
  fi
  if [[ "${PREVIOUS_ENABLED}" == "1" ]]; then
    "${SYSTEMCTL_BIN}" enable "${MONITOR_SERVICE}" >/dev/null 2>&1 || true
  else
    "${SYSTEMCTL_BIN}" disable "${MONITOR_SERVICE}" >/dev/null 2>&1 || true
  fi
  if [[ "${PREVIOUS_ACTIVE}" == "1" ]]; then
    "${SYSTEMCTL_BIN}" restart "${MONITOR_SERVICE}" >/dev/null 2>&1 || true
  else
    "${SYSTEMCTL_BIN}" stop "${MONITOR_SERVICE}" >/dev/null 2>&1 || true
  fi
}

cleanup() {
  local path
  if [[ -n "${STAGE_DIR}" && -d "${STAGE_DIR}" ]]; then
    "${RM_BIN}" -rf "${STAGE_DIR}"
  fi
  if [[ "${INSTALL_COMMITTED}" == "1" ]]; then
    for path in "${BACKUP_CONTAINERS[@]}"; do
      if [[ -n "${path}" && -e "${path}" ]]; then
        "${RM_BIN}" -rf "${path}"
      fi
    done
  fi
  for path in "${PENDING_PATHS[@]}"; do
    if [[ -e "${path}" ]]; then
      "${RM_BIN}" -rf "${path}"
    fi
  done
  return 0
}

remove_created_directories() {
  local index path
  for ((index=${#CREATED_DIRECTORIES[@]}-1; index>=0; index--)); do
    path="${CREATED_DIRECTORIES[index]}"
    if [[ -e "${path}" ]]; then
      "${RM_BIN}" -rf "${path}"
    fi
  done
  return 0
}

on_error() {
  local status=$?
  trap - ERR
  set +e
  if [[ "${INSTALL_COMMITTED}" != "1" ]]; then
    rollback_files
    if [[ "${DATABASE_CREATED}" == "1" ]]; then
      "${RM_BIN}" -f \
        "$(path_for /var/lib/gost-manager/metrics.sqlite3)" \
        "$(path_for /var/lib/gost-manager/metrics.sqlite3-wal)" \
        "$(path_for /var/lib/gost-manager/metrics.sqlite3-shm)"
    fi
    if [[ "${SERVICE_STATE_RECORDED}" == "1" ]]; then
      restore_service_state
    fi
    remove_created_directories
  fi
  cleanup
  exit "${status}"
}

main() {
  parse_arguments "$@"
  require_safe_root
  inject_failure preflight
  validate_dependencies
  inject_failure dependency_validation
  validate_source_manifest
  inject_failure manifest_validation
  stage_sources
  inject_failure staging
  validate_staged_bash
  inject_failure bash_validation
  validate_staged_python
  inject_failure python_validation
  validate_staged_config
  inject_failure config_validation
  validate_unit_content
  inject_failure unit_validation
  record_service_state
  install_files
  inject_failure file_replacement
  "${SYNC_BIN}"
  activate_collector
  INSTALL_COMMITTED=1
  cleanup
  info "GOST Manager and monitoring installed."
  info "Run: sudo gost-manager"
}

trap on_error ERR
main "$@"
