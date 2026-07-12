#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="${GOST_MANAGER_SOURCE_ROOT:-${SCRIPT_DIR}}"
GOST_MANAGER_ROOT="${GOST_MANAGER_ROOT:-}"
GOST_MANAGER_TESTING="${GOST_MANAGER_TESTING:-0}"
GOST_MANAGER_INSTALL_DEPS="${GOST_MANAGER_INSTALL_DEPS:-0}"
GOST_MANAGER_ALLOW_TEST_PACKAGE_STUB="${GOST_MANAGER_ALLOW_TEST_PACKAGE_STUB:-0}"
GOST_MANAGER_TEST_MISSING_COMMANDS="${GOST_MANAGER_TEST_MISSING_COMMANDS:-}"
GOST_MANAGER_TEST_DEP_BIN="${GOST_MANAGER_TEST_DEP_BIN:-}"
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
ACTIVE_CONFIG_PATH=""
CONFIGURED_DB_PRODUCTION=""
CONFIGURED_DB_ACTUAL=""
CONFIGURED_ENV_PRODUCTION=""
CONFIGURED_ENV_ACTUAL=""
INSTALL_COMMITTED=0
ROLLBACK_VERIFIED=0
SERVICE_STATE_RECORDED=0
UNIT_CHANGED=0
PREVIOUS_UNIT_EXISTED=0
PREVIOUS_ENABLED=0
PREVIOUS_ACTIVE=0
FILES_CHANGED=0
DATABASE_CREATED=0
SERVICE_ENABLE_ATTEMPTED=0
SERVICE_START_ATTEMPTED=0
declare -a RUNTIME_MANIFEST_ENTRIES=()
declare -a STAGED_MODULES=()
declare -a CHANGED_DESTINATIONS=()
declare -a BACKUP_PATHS=()
declare -a BACKUP_CONTAINERS=()
declare -a CREATED_DIRECTORIES=()
declare -a PENDING_PATHS=()
declare -a METADATA_PATHS=()
declare -a METADATA_MODES=()
declare -a METADATA_UIDS=()
declare -a METADATA_GIDS=()

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
  else
    [[ -z "${GOST_MANAGER_ROOT}" ]] || die "GOST_MANAGER_ROOT is allowed only in testing mode."
    [[ "${SOURCE_ROOT}" == "${SCRIPT_DIR}" ]] || die "GOST_MANAGER_SOURCE_ROOT is allowed only in testing mode."
    [[ "${EUID}" -eq 0 ]] || die "install.sh must be run as root. Try: sudo bash install.sh"
  fi
  [[ "${SOURCE_ROOT}" == /* && -d "${SOURCE_ROOT}" && ! -L "${SOURCE_ROOT}" ]] || die "installer source root must be a real absolute directory."
  SOURCE_ROOT="$(cd "${SOURCE_ROOT}" && pwd -P)"
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
    bash) printf 'bash\n' ;;
    python3) printf 'python3\n' ;;
    systemctl|systemd-analyze) printf 'systemd\n' ;;
    ss) printf 'iproute2\n' ;;
    cmp) printf 'diffutils\n' ;;
    grep) printf 'grep\n' ;;
    install|cp|mv|rm|chmod|chown|sync|mktemp|stat) printf 'coreutils\n' ;;
    *) die "no package mapping for required command: $1" ;;
  esac
}

command_available() {
  local command_name="$1"
  local forced=",${GOST_MANAGER_TEST_MISSING_COMMANDS// /,},"
  if [[ "${GOST_MANAGER_TESTING}" == "1" && "${forced}" == *",${command_name},"* ]]; then
    [[ -n "${GOST_MANAGER_TEST_DEP_BIN}" && -x "${GOST_MANAGER_TEST_DEP_BIN}/${command_name}" ]]
    return
  fi
  command -v "${command_name}" >/dev/null 2>&1
}

validate_dependencies() {
  local command_name package existing
  local -a required=(bash python3 systemctl systemd-analyze ss grep install cp mv rm chmod chown sync cmp mktemp stat)
  local -a missing=()
  local -a packages=()
  for command_name in "${required[@]}"; do
    if ! command_available "${command_name}"; then
      missing+=("${command_name}")
      package="$(package_for_command "${command_name}")"
      existing=0
      for candidate in "${packages[@]+"${packages[@]}"}"; do
        if [[ "${candidate}" == "${package}" ]]; then
          existing=1
          break
        fi
      done
      [[ "${existing}" == "1" ]] || packages+=("${package}")
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
    command_available "${command_name}" || die "dependency installation did not provide: ${command_name}"
  done
}

manifest_contains() {
  local expected="$1"
  local entry
  for entry in "${RUNTIME_MANIFEST_ENTRIES[@]+"${RUNTIME_MANIFEST_ENTRIES[@]}"}"; do
    [[ "${entry}" == "${expected}" ]] && return 0
  done
  return 1
}

validate_source_manifest() {
  local path entry existing
  local manifest="${SOURCE_ROOT}/packaging/monitoring-runtime-manifest.txt"
  local -a fixed=(
    "gost-manager.sh"
    "lib/gost-run-iran.sh"
    "lib/gost-run-kharej.sh"
    "packaging/gost-monitor"
    "packaging/gost-monitor-admin"
    "packaging/gost-monitor-collector"
    "packaging/gost-monitor-collector.service"
    "packaging/monitoring.env"
    "packaging/monitoring-runtime-manifest.txt"
  )
  for path in "${fixed[@]}"; do
    [[ -f "${SOURCE_ROOT}/${path}" && ! -L "${SOURCE_ROOT}/${path}" ]] || die "required source file is missing or unsafe: ${path}"
  done
  [[ -f "${manifest}" && ! -L "${manifest}" ]] || die "runtime manifest must be a regular non-symlink file."
  RUNTIME_MANIFEST_ENTRIES=()
  while IFS= read -r entry || [[ -n "${entry}" ]]; do
    [[ -n "${entry}" ]] || die "runtime manifest contains a blank record."
    [[ "${entry}" != /* && "${entry}" != *".."* ]] || die "runtime manifest contains an unsafe path: ${entry}"
    [[ "${entry}" =~ ^monitoring/[A-Za-z_][A-Za-z0-9_]*\.py$ ]] || die "runtime manifest contains an invalid module: ${entry}"
    existing=0
    for path in "${RUNTIME_MANIFEST_ENTRIES[@]+"${RUNTIME_MANIFEST_ENTRIES[@]}"}"; do
      if [[ "${path}" == "${entry}" ]]; then
        existing=1
        break
      fi
    done
    [[ "${existing}" == "0" ]] || die "runtime manifest contains a duplicate module: ${entry}"
    [[ -f "${SOURCE_ROOT}/${entry}" && ! -L "${SOURCE_ROOT}/${entry}" ]] || die "runtime module is missing, non-regular, or symlinked: ${entry}"
    RUNTIME_MANIFEST_ENTRIES+=("${entry}")
  done < "${manifest}"
  [[ "${#RUNTIME_MANIFEST_ENTRIES[@]}" -gt 1 ]] || die "runtime manifest is incomplete."
  for path in monitoring/__init__.py monitoring/admin_cli.py monitoring/gost_monitoring.py monitoring/query_cli.py monitoring/runtime_lock.py; do
    manifest_contains "${path}" || die "runtime manifest omits required module: ${path}"
  done
}

stage_sources() {
  local entry destination
  STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gost-manager-install.XXXXXX")"
  "${CHMOD_BIN}" 700 "${STAGE_DIR}"
  "${INSTALL_BIN}" -d -m 755 "${STAGE_DIR}/lib" "${STAGE_DIR}/monitoring" "${STAGE_DIR}/sbin"
  "${CP_BIN}" "${SOURCE_ROOT}/gost-manager.sh" "${STAGE_DIR}/gost-manager"
  "${CP_BIN}" "${SOURCE_ROOT}/lib/gost-run-iran.sh" "${STAGE_DIR}/lib/gost-run-iran.sh"
  "${CP_BIN}" "${SOURCE_ROOT}/lib/gost-run-kharej.sh" "${STAGE_DIR}/lib/gost-run-kharej.sh"
  STAGED_MODULES=()
  for entry in "${RUNTIME_MANIFEST_ENTRIES[@]}"; do
    destination="${STAGE_DIR}/monitoring/${entry##*/}"
    "${CP_BIN}" "${SOURCE_ROOT}/${entry}" "${destination}"
    STAGED_MODULES+=("${destination}")
  done
  "${CP_BIN}" "${SOURCE_ROOT}/packaging/gost-monitor" "${STAGE_DIR}/sbin/gost-monitor"
  "${CP_BIN}" "${SOURCE_ROOT}/packaging/gost-monitor-admin" "${STAGE_DIR}/sbin/gost-monitor-admin"
  "${CP_BIN}" "${SOURCE_ROOT}/packaging/gost-monitor-collector" "${STAGE_DIR}/sbin/gost-monitor-collector"
  "${CP_BIN}" "${SOURCE_ROOT}/packaging/gost-monitor-collector.service" "${STAGE_DIR}/gost-monitor-collector.service"
  "${CP_BIN}" "${SOURCE_ROOT}/packaging/monitoring.env" "${STAGE_DIR}/monitoring.env"
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
  PYTHONPYCACHEPREFIX="${STAGE_DIR}/pycache" "${PYTHON_BIN}" -m py_compile "${STAGED_MODULES[@]}"
}

admin_config_command() {
  local config_path="$1"
  shift
  local -a root_args=()
  if [[ -n "${GOST_MANAGER_ROOT}" ]]; then
    root_args=(--path-root "${GOST_MANAGER_ROOT}")
  fi
  if [[ "${#root_args[@]}" -gt 0 ]]; then
    PYTHONPATH="${STAGE_DIR}" "${PYTHON_BIN}" -m monitoring.admin_cli \
      --policy installed "${root_args[@]}" "$@" --config "${config_path}"
  else
    PYTHONPATH="${STAGE_DIR}" "${PYTHON_BIN}" -m monitoring.admin_cli \
      --policy installed "$@" --config "${config_path}"
  fi
}

validate_config_file() {
  local config_path="$1"
  [[ ! -L "${config_path}" ]] || die "monitoring config may not be a symlink."
  admin_config_command "${config_path}" validate-config >/dev/null
}

config_value() {
  local config_path="$1"
  local field="$2"
  admin_config_command "${config_path}" config --format value --field "${field}"
}

validate_staged_config() {
  local installed_config
  validate_config_file "${STAGE_DIR}/monitoring.env"
  installed_config="$(path_for /etc/gost-manager/monitoring.env)"
  ACTIVE_CONFIG_PATH="${STAGE_DIR}/monitoring.env"
  if [[ -e "${installed_config}" || -L "${installed_config}" ]]; then
    reject_symlink_path "${installed_config}"
    if ! validate_config_file "${installed_config}"; then
      die "existing monitoring config is incompatible with the installed policy; move its database below /var/lib/gost-manager and env directory below /etc/gost before retrying."
    fi
    ACTIVE_CONFIG_PATH="${installed_config}"
  fi
  CONFIGURED_DB_PRODUCTION="$(config_value "${ACTIVE_CONFIG_PATH}" database_path)"
  CONFIGURED_ENV_PRODUCTION="$(config_value "${ACTIVE_CONFIG_PATH}" env_directory)"
  CONFIGURED_DB_ACTUAL="$(path_for "${CONFIGURED_DB_PRODUCTION}")"
  CONFIGURED_ENV_ACTUAL="$(path_for "${CONFIGURED_ENV_PRODUCTION}")"
  reject_symlink_path "${CONFIGURED_DB_ACTUAL}"
  reject_symlink_path "${CONFIGURED_ENV_ACTUAL}"
}

validate_unit_content() {
  local unit="${STAGE_DIR}/gost-monitor-collector.service"
  local forbidden required verification_dir verification_unit output
  for forbidden in 'Requires=' 'PartOf=' 'BindsTo=' 'PrivateNetwork=' 'ProtectProc=' 'ProcSubset=' 'InaccessiblePaths=/proc' 'nginx.service' 'gost-iran-' 'gost-kharej-'; do
    if grep -Fq "${forbidden}" "${unit}"; then
      die "monitoring unit contains forbidden setting: ${forbidden}"
    fi
  done
  for required in \
    'EnvironmentFile=/etc/gost-manager/monitoring.env' \
    'ExecStartPre=/usr/local/sbin/gost-monitor-admin validate-config --config /etc/gost-manager/monitoring.env' \
    'ExecStart=/usr/local/sbin/gost-monitor-collector --daemon' \
    'Restart=on-failure' 'UMask=0077' 'StateDirectoryMode=0700' \
    'RuntimeDirectory=gost-manager' 'RuntimeDirectoryMode=0700' \
    'ReadWritePaths=/var/lib/gost-manager'; do
    grep -Fq "${required}" "${unit}" || die "monitoring unit lacks required production setting: ${required}"
  done
  if ! command -v "${SYSTEMD_ANALYZE_BIN}" >/dev/null 2>&1; then
    info "systemd-analyze unavailable; deterministic unit validation passed."
    return 0
  fi
  verification_dir="$(mktemp -d "${STAGE_DIR}/systemd-verify.XXXXXX")"
  verification_unit="${verification_dir}/${MONITOR_SERVICE}"
  "${CP_BIN}" "${unit}" "${verification_unit}"
  "${CP_BIN}" "${STAGE_DIR}/monitoring.env" "${verification_dir}/monitoring.env"
  printf '#!/usr/bin/env bash\nexit 0\n' > "${verification_dir}/gost-monitor-admin"
  printf '#!/usr/bin/env bash\nexit 0\n' > "${verification_dir}/gost-monitor-collector"
  "${CHMOD_BIN}" 755 "${verification_dir}/gost-monitor-admin" "${verification_dir}/gost-monitor-collector"
  "${PYTHON_BIN}" -c \
    'import sys; from pathlib import Path; p=Path(sys.argv[1]); s=p.read_text(); s=s.replace("/usr/local/sbin/gost-monitor-admin", sys.argv[2]).replace("/usr/local/sbin/gost-monitor-collector", sys.argv[3]).replace("/etc/gost-manager/monitoring.env", sys.argv[4]); p.write_text(s)' \
    "${verification_unit}" \
    "${verification_dir}/gost-monitor-admin" \
    "${verification_dir}/gost-monitor-collector" \
    "${verification_dir}/monitoring.env"
  if ! output="$("${SYSTEMD_ANALYZE_BIN}" verify "${verification_unit}" 2>&1)"; then
    [[ -z "${output}" ]] || printf '%s\n' "${output}" >&2
    die "real host systemd unit verification failed."
  fi
  if [[ -n "${output}" ]]; then
    printf '%s\n' "${output}" >&2
    die "real host systemd unit verification emitted warnings."
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

record_metadata() {
  local path="$1"
  local existing metadata mode uid gid
  [[ -e "${path}" && ! -L "${path}" ]] || return 0
  for existing in "${METADATA_PATHS[@]+"${METADATA_PATHS[@]}"}"; do
    [[ "${existing}" == "${path}" ]] && return 0
  done
  metadata="$(stat -c '%a %u %g' "${path}" 2>/dev/null || stat -f '%Lp %u %g' "${path}")"
  read -r mode uid gid <<< "${metadata}"
  METADATA_PATHS+=("${path}")
  METADATA_MODES+=("${mode}")
  METADATA_UIDS+=("${uid}")
  METADATA_GIDS+=("${gid}")
}

ensure_shared_directory() {
  local path="$1"
  local mode="$2"
  reject_symlink_path "${path}"
  if [[ -e "${path}" && ! -d "${path}" ]]; then
    die "shared path is not a directory: ${path}"
  fi
  if [[ ! -d "${path}" ]]; then
    "${INSTALL_BIN}" -d -m "${mode}" "${path}"
    CREATED_DIRECTORIES+=("${path}")
    "${CHOWN_BIN}" root:root "${path}"
  fi
}

ensure_legacy_directory() {
  local path="$1"
  reject_symlink_path "${path}"
  if [[ -e "${path}" && ! -d "${path}" ]]; then
    die "legacy path is not a directory: ${path}"
  fi
  if [[ ! -d "${path}" ]]; then
    "${INSTALL_BIN}" -d -m 700 "${path}"
    CREATED_DIRECTORIES+=("${path}")
    "${CHOWN_BIN}" root:root "${path}"
  fi
}

ensure_private_directory() {
  local path="$1"
  local mode="$2"
  reject_symlink_path "${path}"
  if [[ -e "${path}" && ! -d "${path}" ]]; then
    die "private managed path is not a directory: ${path}"
  fi
  if [[ ! -d "${path}" ]]; then
    "${INSTALL_BIN}" -d -m "${mode}" "${path}"
    CREATED_DIRECTORIES+=("${path}")
    "${CHOWN_BIN}" root:root "${path}"
  else
    record_metadata "${path}"
    "${CHMOD_BIN}" "${mode}" "${path}"
    "${CHOWN_BIN}" root:root "${path}"
  fi
}

ensure_configured_db_parent() {
  local state_root parent relative part current
  local -a parts=()
  state_root="$(path_for /var/lib/gost-manager)"
  parent="${CONFIGURED_DB_ACTUAL%/*}"
  [[ "${parent}" == "${state_root}" || "${parent}" == "${state_root}"/* ]] || die "configured database parent escaped the managed state tree."
  relative="${parent#"${state_root}"}"
  current="${state_root}"
  IFS='/' read -r -a parts <<< "${relative#/}"
  for part in "${parts[@]+"${parts[@]}"}"; do
    [[ -n "${part}" ]] || continue
    current="${current}/${part}"
    ensure_private_directory "${current}" 700
  done
}

install_managed_file() {
  local source="$1"
  local destination="$2"
  local mode="$3"
  local candidate backup backup_container
  reject_symlink_path "${destination}"
  if [[ -e "${destination}" && ! -f "${destination}" ]]; then
    die "managed file destination is not a regular file: ${destination}"
  fi
  if [[ -f "${destination}" ]]; then
    record_metadata "${destination}"
    if "${CMP_BIN}" -s "${source}" "${destination}"; then
      "${CHMOD_BIN}" "${mode}" "${destination}"
      "${CHOWN_BIN}" root:root "${destination}"
      return 0
    fi
  fi
  candidate="$(mktemp "${destination}.gost-manager-new.XXXXXX")"
  PENDING_PATHS+=("${candidate}")
  "${INSTALL_BIN}" -m "${mode}" "${source}" "${candidate}"
  "${CHOWN_BIN}" root:root "${candidate}"
  if [[ -e "${destination}" ]]; then
    backup_container="$(mktemp -d "${destination}.gost-manager-backup.XXXXXX")"
    BACKUP_CONTAINERS+=("${backup_container}")
    "${CHMOD_BIN}" 700 "${backup_container}"
    "${CHOWN_BIN}" root:root "${backup_container}"
    backup="${backup_container}/original"
    "${CP_BIN}" -p "${destination}" "${backup}"
  else
    backup=""
    backup_container=""
  fi
  CHANGED_DESTINATIONS+=("${destination}")
  BACKUP_PATHS+=("${backup}")
  inject_failure backup
  "${MV_BIN}" "${candidate}" "${destination}"
  FILES_CHANGED=1
}

install_monitoring_package() {
  local destination candidate backup backup_container entry
  destination="$(path_for /usr/local/lib/gost-manager/monitoring)"
  reject_symlink_path "${destination}"
  if [[ -e "${destination}" && ! -d "${destination}" ]]; then
    die "monitoring package destination is not a directory."
  fi
  [[ ! -d "${destination}" ]] || record_metadata "${destination}"
  candidate="$(mktemp -d "${destination}.gost-manager-new.XXXXXX")"
  PENDING_PATHS+=("${candidate}")
  "${CHMOD_BIN}" 755 "${candidate}"
  for entry in "${RUNTIME_MANIFEST_ENTRIES[@]}"; do
    "${INSTALL_BIN}" -m 644 "${STAGE_DIR}/monitoring/${entry##*/}" "${candidate}/${entry##*/}"
  done
  "${CHOWN_BIN}" -R root:root "${candidate}"
  if [[ -d "${destination}" ]]; then
    backup_container="$(mktemp -d "${destination}.gost-manager-backup.XXXXXX")"
    BACKUP_CONTAINERS+=("${backup_container}")
    "${CHMOD_BIN}" 700 "${backup_container}"
    "${CHOWN_BIN}" root:root "${backup_container}"
    backup="${backup_container}/original"
    "${MV_BIN}" "${destination}" "${backup}"
  else
    backup=""
    backup_container=""
  fi
  CHANGED_DESTINATIONS+=("${destination}")
  BACKUP_PATHS+=("${backup}")
  "${MV_BIN}" "${candidate}" "${destination}"
  FILES_CHANGED=1
}

install_files() {
  local config_path unit_path before_count
  ensure_shared_directory "$(path_for /usr/local/sbin)" 755
  ensure_private_directory "$(path_for /usr/local/lib/gost-manager)" 755
  ensure_legacy_directory "$(path_for /etc/gost)"
  ensure_private_directory "$(path_for /etc/gost-manager)" 700
  ensure_shared_directory "$(path_for /etc/systemd/system)" 755
  ensure_private_directory "$(path_for /var/lib/gost-manager)" 700
  ensure_configured_db_parent

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
  else
    record_metadata "${config_path}"
    "${CHMOD_BIN}" 600 "${config_path}"
    "${CHOWN_BIN}" root:root "${config_path}"
  fi
  ACTIVE_CONFIG_PATH="${config_path}"

  unit_path="$(path_for /etc/systemd/system/${MONITOR_SERVICE})"
  before_count="${#CHANGED_DESTINATIONS[@]}"
  install_managed_file "${STAGE_DIR}/gost-monitor-collector.service" "${unit_path}" 644
  if [[ "${#CHANGED_DESTINATIONS[@]}" -gt "${before_count}" ]]; then
    UNIT_CHANGED=1
  fi
}

migrate_configured_database() {
  local library
  local -a root_args=()
  library="$(path_for /usr/local/lib/gost-manager)"
  [[ -e "${CONFIGURED_DB_ACTUAL}" ]] || DATABASE_CREATED=1
  [[ ! -e "${CONFIGURED_DB_ACTUAL}" ]] || record_metadata "${CONFIGURED_DB_ACTUAL}"
  if [[ -n "${GOST_MANAGER_ROOT}" ]]; then
    root_args=(--path-root "${GOST_MANAGER_ROOT}")
  fi
  if [[ "${#root_args[@]}" -gt 0 ]]; then
    PYTHONPATH="${library}" "${PYTHON_BIN}" -m monitoring.admin_cli \
      --policy installed "${root_args[@]}" migrate --config "${ACTIVE_CONFIG_PATH}" >/dev/null
  else
    PYTHONPATH="${library}" "${PYTHON_BIN}" -m monitoring.admin_cli \
      --policy installed migrate --config "${ACTIVE_CONFIG_PATH}" >/dev/null
  fi
  reject_symlink_path "${CONFIGURED_DB_ACTUAL}"
  "${CHMOD_BIN}" 600 "${CONFIGURED_DB_ACTUAL}"
  "${CHOWN_BIN}" root:root "${CONFIGURED_DB_ACTUAL}"
}

activate_collector() {
  if [[ "${UNIT_CHANGED}" == "1" ]]; then
    "${SYSTEMCTL_BIN}" daemon-reload
  fi
  inject_failure daemon_reload
  migrate_configured_database
  inject_failure migration
  if [[ "${PREVIOUS_UNIT_EXISTED}" == "0" ]]; then
    SERVICE_ENABLE_ATTEMPTED=1
    "${SYSTEMCTL_BIN}" enable "${MONITOR_SERVICE}"
    SERVICE_START_ATTEMPTED=1
    "${SYSTEMCTL_BIN}" start "${MONITOR_SERVICE}"
  elif [[ "${PREVIOUS_ACTIVE}" == "1" && "${FILES_CHANGED}" == "1" ]]; then
    "${SYSTEMCTL_BIN}" restart "${MONITOR_SERVICE}"
  fi
  inject_failure collector_start
}

rollback_files() {
  local index destination backup failed=0
  for ((index=${#CHANGED_DESTINATIONS[@]}-1; index>=0; index--)); do
    destination="${CHANGED_DESTINATIONS[index]}"
    backup="${BACKUP_PATHS[index]}"
    "${RM_BIN}" -rf "${destination}" || failed=1
    if [[ -n "${backup}" && -e "${backup}" ]]; then
      "${CP_BIN}" -Rp "${backup}" "${destination}" || failed=1
    fi
  done
  return "${failed}"
}

restore_metadata() {
  local index path failed=0
  for ((index=${#METADATA_PATHS[@]}-1; index>=0; index--)); do
    path="${METADATA_PATHS[index]}"
    if [[ -e "${path}" && ! -L "${path}" ]]; then
      "${CHMOD_BIN}" "${METADATA_MODES[index]}" "${path}" || failed=1
      "${CHOWN_BIN}" "${METADATA_UIDS[index]}:${METADATA_GIDS[index]}" "${path}" || failed=1
    fi
  done
  return "${failed}"
}

collector_is_active() {
  "${SYSTEMCTL_BIN}" is-active --quiet "${MONITOR_SERVICE}" >/dev/null 2>&1
}

collector_is_enabled() {
  "${SYSTEMCTL_BIN}" is-enabled --quiet "${MONITOR_SERVICE}" >/dev/null 2>&1
}

verify_collector_loaded() {
  local state
  state="$("${SYSTEMCTL_BIN}" show "${MONITOR_SERVICE}" --property=LoadState --value 2>/dev/null)" || return 1
  [[ -n "${state}" && "${state}" != "not-found" ]]
}

verify_collector_not_loaded() {
  local state
  state="$("${SYSTEMCTL_BIN}" show "${MONITOR_SERVICE}" --property=LoadState --value 2>/dev/null)" || return 1
  [[ "${state}" == "not-found" ]]
}

prepare_service_rollback() {
  if [[ "${PREVIOUS_UNIT_EXISTED}" == "0" ]]; then
    if [[ "${SERVICE_START_ATTEMPTED}" == "1" ]] || collector_is_active; then
      "${SYSTEMCTL_BIN}" stop "${MONITOR_SERVICE}" >/dev/null 2>&1 || true
    fi
    if [[ "${SERVICE_ENABLE_ATTEMPTED}" == "1" ]] || collector_is_enabled; then
      "${SYSTEMCTL_BIN}" disable "${MONITOR_SERVICE}" >/dev/null 2>&1 || true
    fi
    if collector_is_active || collector_is_enabled; then
      info "Collector cleanup could not be verified before fresh-install rollback."
      return 1
    fi
    return 0
  fi
  if [[ "${FILES_CHANGED}" == "1" && "${PREVIOUS_ACTIVE}" == "1" ]]; then
    "${SYSTEMCTL_BIN}" stop "${MONITOR_SERVICE}" >/dev/null 2>&1 || true
    if collector_is_active; then
      info "Existing collector could not be stopped for file restoration."
      return 1
    fi
  fi
}

restore_recorded_service_state() {
  if [[ "${PREVIOUS_ENABLED}" == "1" ]]; then
    "${SYSTEMCTL_BIN}" enable "${MONITOR_SERVICE}" >/dev/null 2>&1 || true
  else
    "${SYSTEMCTL_BIN}" disable "${MONITOR_SERVICE}" >/dev/null 2>&1 || true
  fi
  if [[ "${PREVIOUS_ACTIVE}" == "1" ]]; then
    "${SYSTEMCTL_BIN}" start "${MONITOR_SERVICE}" >/dev/null 2>&1 || true
  else
    "${SYSTEMCTL_BIN}" stop "${MONITOR_SERVICE}" >/dev/null 2>&1 || true
  fi
  if [[ "${PREVIOUS_ENABLED}" == "1" ]]; then
    collector_is_enabled || return 1
  else
    ! collector_is_enabled || return 1
  fi
  if [[ "${PREVIOUS_ACTIVE}" == "1" ]]; then
    collector_is_active || return 1
  else
    ! collector_is_active || return 1
  fi
  return 0
}

remove_created_database() {
  if [[ "${DATABASE_CREATED}" == "1" && -n "${CONFIGURED_DB_ACTUAL}" ]]; then
    "${RM_BIN}" -f \
      "${CONFIGURED_DB_ACTUAL}" \
      "${CONFIGURED_DB_ACTUAL}-wal" \
      "${CONFIGURED_DB_ACTUAL}-shm" || return 1
  fi
  return 0
}

remove_created_directories() {
  local index path failed=0
  for ((index=${#CREATED_DIRECTORIES[@]}-1; index>=0; index--)); do
    path="${CREATED_DIRECTORIES[index]}"
    if [[ -e "${path}" ]]; then
      "${RM_BIN}" -rf "${path}" || failed=1
    fi
  done
  return "${failed}"
}

rollback_transaction() {
  [[ "${SERVICE_STATE_RECORDED}" == "1" ]] || {
    rollback_files || return 1
    restore_metadata || return 1
    remove_created_database || return 1
    remove_created_directories || return 1
    return 0
  }
  prepare_service_rollback || return 1
  rollback_files || return 1
  remove_created_database || return 1
  restore_metadata || return 1
  remove_created_directories || return 1
  "${SYSTEMCTL_BIN}" daemon-reload >/dev/null 2>&1 || {
    info "systemd daemon-reload failed during installer rollback."
    return 1
  }
  if [[ "${PREVIOUS_UNIT_EXISTED}" == "0" ]]; then
    if collector_is_active || collector_is_enabled || ! verify_collector_not_loaded; then
      info "Fresh collector state remains after rollback."
      return 1
    fi
    return 0
  fi
  restore_recorded_service_state || {
    info "Previous collector enabled/active state could not be restored."
    return 1
  }
  verify_collector_loaded || {
    info "Previous collector unit is not loaded after rollback."
    return 1
  }
  return 0
}

cleanup() {
  local path
  if [[ -n "${STAGE_DIR}" && -d "${STAGE_DIR}" ]]; then
    "${RM_BIN}" -rf "${STAGE_DIR}"
  fi
  if [[ "${INSTALL_COMMITTED}" == "1" || "${ROLLBACK_VERIFIED}" == "1" ]]; then
    for path in "${BACKUP_CONTAINERS[@]+"${BACKUP_CONTAINERS[@]}"}"; do
      if [[ -n "${path}" && -e "${path}" ]]; then
        "${RM_BIN}" -rf "${path}"
      fi
    done
  fi
  for path in "${PENDING_PATHS[@]+"${PENDING_PATHS[@]}"}"; do
    if [[ -e "${path}" ]]; then
      "${RM_BIN}" -rf "${path}"
    fi
  done
  return 0
}

print_recovery_guidance() {
  local index destination backup path
  info "Installer rollback could not be verified; recovery backups were retained."
  info "Run these recovery commands after inspecting the retained files:"
  printf 'systemctl stop %q\n' "${MONITOR_SERVICE}"
  for ((index=${#CHANGED_DESTINATIONS[@]}-1; index>=0; index--)); do
    destination="${CHANGED_DESTINATIONS[index]}"
    backup="${BACKUP_PATHS[index]}"
    printf 'rm -rf %q\n' "${destination}"
    if [[ -n "${backup}" && -e "${backup}" ]]; then
      printf 'cp -a %q %q\n' "${backup}" "${destination}"
    fi
  done
  printf 'systemctl daemon-reload\n'
  if [[ "${PREVIOUS_ENABLED}" == "1" ]]; then
    printf 'systemctl enable %q\n' "${MONITOR_SERVICE}"
  else
    printf 'systemctl disable %q\n' "${MONITOR_SERVICE}"
  fi
  if [[ "${PREVIOUS_ACTIVE}" == "1" ]]; then
    printf 'systemctl start %q\n' "${MONITOR_SERVICE}"
  else
    printf 'systemctl stop %q\n' "${MONITOR_SERVICE}"
  fi
  printf 'systemctl status %q --no-pager\n' "${MONITOR_SERVICE}"
  for path in "${BACKUP_CONTAINERS[@]+"${BACKUP_CONTAINERS[@]}"}"; do
    [[ -z "${path}" ]] || info "Retained backup: ${path}"
  done
}

on_error() {
  local status=$?
  trap - ERR
  set +e
  if [[ "${INSTALL_COMMITTED}" != "1" ]]; then
    if rollback_transaction; then
      ROLLBACK_VERIFIED=1
    else
      print_recovery_guidance
      status=1
    fi
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
  info "Monitoring database: ${CONFIGURED_DB_PRODUCTION}"
  info "Run: sudo gost-manager"
}

trap on_error ERR
main "$@"
