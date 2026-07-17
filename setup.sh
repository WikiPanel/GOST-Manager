#!/usr/bin/env bash
set -Eeuo pipefail

readonly RELEASE_ORIGIN="https://github.com/WikiPanel/GOST-Manager"
readonly ARCHIVE_NAME="gost-manager.tar.gz"
readonly CHECKSUM_NAME="gost-manager.tar.gz.sha256"
readonly OS_RELEASE_MAX_BYTES=65536

WORK_DIR=""
CANDIDATE_DIR=""
BACKUP_DIR=""
TARGET_DIR=""
TARGET_PARENT=""
SOURCE_CANDIDATE_READY=0
PRIOR_SOURCE_BACKED_UP=0
NEW_SOURCE_ACTIVATED=0
LOCAL_INSTALL_SUCCEEDED=0
LOCAL_INSTALL_ROLLBACK_UNVERIFIED=0
SETUP_COMMITTED=0
MISSING_DEPENDENCIES=()

info() {
  printf '%s\n' "$*"
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

testing_enabled() {
  [[ "${GOST_MANAGER_SETUP_TESTING:-0}" == "1" ]]
}

reject_production_test_controls() {
  local name
  testing_enabled && return 0
  for name in \
    GOST_MANAGER_SETUP_ROOT \
    GOST_MANAGER_SETUP_EUID_TEST \
    GOST_MANAGER_SETUP_ARCH_TEST \
    GOST_MANAGER_SETUP_OS_RELEASE_TEST \
    GOST_MANAGER_SETUP_OS_RELEASE_UID_TEST \
    GOST_MANAGER_RELEASE_BASE_URL_TEST \
    GOST_MANAGER_SETUP_MISSING_DEPS_TEST \
    GOST_MANAGER_SETUP_DEPENDENCIES_READY_TEST \
    GOST_MANAGER_SETUP_INTERACTIVE_TEST \
    GOST_MANAGER_SETUP_LAUNCHER_TEST \
    GOST_MANAGER_SETUP_FAIL_PHASE_TEST; do
    [[ -z "${!name:-}" ]] || die "${name} is a test-only setting."
  done
}

validate_test_failure_phases() {
  local phase
  local -a phases=()
  testing_enabled || return 0
  [[ -n "${GOST_MANAGER_SETUP_FAIL_PHASE_TEST:-}" ]] || return 0
  IFS=',' read -r -a phases <<< "${GOST_MANAGER_SETUP_FAIL_PHASE_TEST}"
  for phase in "${phases[@]}"; do
    case "${phase}" in
      before_source_activation|candidate_rename|after_source_activation|source_restore|backup_cleanup) ;;
      *) die "unknown setup test failure phase: ${phase:-empty}" ;;
    esac
  done
}

setup_failure_enabled() {
  local expected="$1"
  local phase
  local -a phases=()
  testing_enabled || return 1
  IFS=',' read -r -a phases <<< "${GOST_MANAGER_SETUP_FAIL_PHASE_TEST:-}"
  for phase in "${phases[@]+"${phases[@]}"}"; do
    [[ "${phase}" == "${expected}" ]] && return 0
  done
  return 1
}

inject_setup_failure() {
  local phase="$1"
  setup_failure_enabled "${phase}" || return 0
  die "injected setup failure at phase: ${phase}"
}

validate_testing_root() {
  local root resolved
  testing_enabled || return 0
  root="${GOST_MANAGER_SETUP_ROOT:-}"
  [[ -n "${root}" && "${root}" == /* && "${root}" != "/" ]] || die "GOST_MANAGER_SETUP_ROOT must be a non-root absolute test directory."
  [[ -d "${root}" && ! -L "${root}" ]] || die "GOST_MANAGER_SETUP_ROOT must be a safe directory."
  resolved="$(cd "${root}" && pwd -P)"
  [[ "${resolved}" == "${root%/}" ]] || die "GOST_MANAGER_SETUP_ROOT must not contain symlink components."
}

path_for() {
  local path="$1"
  if testing_enabled && [[ -n "${GOST_MANAGER_SETUP_ROOT:-}" ]]; then
    printf '%s%s\n' "${GOST_MANAGER_SETUP_ROOT%/}" "${path}"
  else
    printf '%s\n' "${path}"
  fi
}

validate_private_source_path() {
  local path="$1"
  local kind="$2"
  local parent base
  [[ -n "${path}" && "${path}" != "/" ]] || return 1
  parent="$(dirname -- "${path}")"
  base="$(basename -- "${path}")"
  [[ "${parent}" == "${TARGET_PARENT}" ]] || return 1
  case "${kind}" in
    candidate) [[ "${base}" =~ ^\.GOST-Manager\.new\.[A-Za-z0-9]+$ ]] ;;
    backup) [[ "${base}" =~ ^\.GOST-Manager\.backup\.[A-Za-z0-9]+$ ]] ;;
    *) return 1 ;;
  esac
}

remove_private_tree() {
  local path="$1"
  local kind="$2"
  [[ ! -e "${path}" && ! -L "${path}" ]] && return 0
  [[ -d "${path}" && ! -L "${path}" ]] || {
    printf 'ERROR: refusing to remove unsafe %s path: %s\n' "${kind}" "${path}" >&2
    return 1
  }
  case "${kind}" in
    candidate|backup)
      validate_private_source_path "${path}" "${kind}" || {
        printf 'ERROR: refusing to remove unexpected %s path: %s\n' "${kind}" "${path}" >&2
        return 1
      }
      ;;
    workspace)
      [[ "${path}" == "${WORK_DIR}" && "$(basename -- "${path}")" =~ ^gost-manager-setup\.[A-Za-z0-9]+$ ]] || {
        printf 'ERROR: refusing to remove unexpected setup workspace: %s\n' "${path}" >&2
        return 1
      }
      ;;
    active-source)
      [[ "${NEW_SOURCE_ACTIVATED}" == "1" && "${path}" == "${TARGET_DIR}" ]] || {
        printf 'ERROR: refusing to remove an unproven active source: %s\n' "${path}" >&2
        return 1
      }
      ;;
    *)
      printf 'ERROR: refusing to remove unknown setup path kind: %s\n' "${kind}" >&2
      return 1
      ;;
  esac
  rm -rf -- "${path}"
}

print_source_recovery() {
  printf 'ERROR: source rollback could not be verified.\n' >&2
  if [[ -e "${TARGET_DIR}" || -L "${TARGET_DIR}" ]]; then
    printf 'Current source path: %s\n' "${TARGET_DIR}" >&2
  fi
  if [[ -n "${BACKUP_DIR}" ]]; then
    printf 'Retained source backup: %s\n' "${BACKUP_DIR}" >&2
  fi
  if [[ -n "${CANDIDATE_DIR}" && ( -e "${CANDIDATE_DIR}" || -L "${CANDIDATE_DIR}" ) ]]; then
    printf 'Retained source candidate: %s\n' "${CANDIDATE_DIR}" >&2
  fi
}

rollback_source_tree() {
  if [[ "${PRIOR_SOURCE_BACKED_UP}" == "0" && "${NEW_SOURCE_ACTIVATED}" == "0" ]]; then
    return 0
  fi
  if setup_failure_enabled source_restore; then
    printf 'ERROR: injected setup failure at phase: source_restore\n' >&2
    return 1
  fi
  if [[ "${PRIOR_SOURCE_BACKED_UP}" == "1" ]]; then
    validate_private_source_path "${BACKUP_DIR}" backup || return 1
    [[ -d "${BACKUP_DIR}" && ! -L "${BACKUP_DIR}" ]] || return 1
  fi
  if [[ "${NEW_SOURCE_ACTIVATED}" == "1" ]]; then
    remove_private_tree "${TARGET_DIR}" active-source || return 1
    NEW_SOURCE_ACTIVATED=0
  fi
  if [[ "${PRIOR_SOURCE_BACKED_UP}" == "1" ]]; then
    [[ ! -e "${TARGET_DIR}" && ! -L "${TARGET_DIR}" ]] || return 1
    mv -- "${BACKUP_DIR}" "${TARGET_DIR}" || return 1
    [[ -d "${TARGET_DIR}" && ! -L "${TARGET_DIR}" && ! -e "${BACKUP_DIR}" ]] || return 1
    BACKUP_DIR=""
    PRIOR_SOURCE_BACKED_UP=0
  fi
  return 0
}

cleanup() {
  local status=$?
  local cleanup_failed=0
  trap - EXIT INT TERM
  set +e

  if [[ "${status}" -ne 0 && "${SETUP_COMMITTED}" == "0" ]]; then
    if [[ "${LOCAL_INSTALL_ROLLBACK_UNVERIFIED}" == "1" ]]; then
      printf 'ERROR: installer rollback was not verified; source rollback was skipped to retain matching recovery material.\n' >&2
      print_source_recovery
      status=1
    elif [[ "${LOCAL_INSTALL_SUCCEEDED}" == "0" ]]; then
      if ! rollback_source_tree; then
        print_source_recovery
        status=1
      fi
    else
      printf 'ERROR: local installation succeeded; source rollback was intentionally skipped to preserve runtime/source alignment.\n' >&2
      print_source_recovery
      status=1
    fi
  fi

  if [[ -n "${CANDIDATE_DIR}" && ( -e "${CANDIDATE_DIR}" || -L "${CANDIDATE_DIR}" ) ]]; then
    if ! remove_private_tree "${CANDIDATE_DIR}" candidate; then
      cleanup_failed=1
    else
      CANDIDATE_DIR=""
      SOURCE_CANDIDATE_READY=0
    fi
  fi
  if [[ -n "${BACKUP_DIR}" && "${PRIOR_SOURCE_BACKED_UP}" == "0" && ( -e "${BACKUP_DIR}" || -L "${BACKUP_DIR}" ) ]]; then
    if ! remove_private_tree "${BACKUP_DIR}" backup; then
      cleanup_failed=1
    else
      BACKUP_DIR=""
    fi
  fi
  if [[ -n "${WORK_DIR}" && ( -e "${WORK_DIR}" || -L "${WORK_DIR}" ) ]]; then
    remove_private_tree "${WORK_DIR}" workspace || cleanup_failed=1
  fi
  [[ "${cleanup_failed}" == "0" ]] || status=1
  exit "${status}"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

require_root() {
  local current_euid="${EUID}"
  if testing_enabled && [[ -n "${GOST_MANAGER_SETUP_EUID_TEST:-}" ]]; then
    current_euid="${GOST_MANAGER_SETUP_EUID_TEST}"
  fi
  [[ "${current_euid}" == "0" ]] || die "setup must run as root. Try piping the command to sudo bash."
}

os_release_stat_mode() {
  local path="$1"
  stat -c '%a' -- "${path}" 2>/dev/null || stat -f '%Lp' "${path}"
}

os_release_stat_uid() {
  local path="$1"
  if testing_enabled && [[ -n "${GOST_MANAGER_SETUP_OS_RELEASE_UID_TEST:-}" ]]; then
    printf '%s\n' "${GOST_MANAGER_SETUP_OS_RELEASE_UID_TEST}"
    return 0
  fi
  stat -c '%u' -- "${path}" 2>/dev/null || stat -f '%u' "${path}"
}

os_release_stat_size() {
  local path="$1"
  stat -c '%s' -- "${path}" 2>/dev/null || stat -f '%z' "${path}"
}

validate_os_release_mode() {
  local path="$1"
  local description="$2"
  local mode permissions
  mode="$(os_release_stat_mode "${path}")" || die "cannot inspect ${description} permissions."
  [[ "${mode}" =~ ^[0-7]{3,4}$ ]] || die "cannot validate ${description} permissions."
  permissions=$((8#${mode}))
  (( (permissions & 8#022) == 0 )) || die "${description} must not be writable by group or others."
}

validate_os_release_parents() {
  local logical physical canonical uid
  for logical in /etc /usr /usr/lib; do
    physical="$(path_for "${logical}")"
    [[ -d "${physical}" && ! -L "${physical}" ]] || die "unsafe OS metadata directory: ${logical}."
    canonical="$(cd "${physical}" 2>/dev/null && pwd -P)" || die "cannot resolve OS metadata directory: ${logical}."
    [[ "${canonical}" == "${physical}" ]] || die "OS metadata directory resolves outside its trusted path: ${logical}."
    validate_os_release_mode "${physical}" "OS metadata directory ${logical}"
    if ! testing_enabled; then
      uid="$(stat -c '%u' -- "${physical}" 2>/dev/null || stat -f '%u' "${physical}")" || \
        die "cannot inspect OS metadata directory ownership: ${logical}."
      [[ "${uid}" == "0" ]] || die "OS metadata directory must be owned by root: ${logical}."
    fi
  done
}

resolve_os_release_candidate() {
  local current="$1"
  local target parent base canonical_parent
  local depth=0

  while [[ -L "${current}" ]]; do
    depth=$((depth + 1))
    (( depth <= 16 )) || return 1
    target="$(readlink "${current}")" || return 1
    [[ -n "${target}" ]] || return 1
    if [[ "${target}" == /* ]]; then
      if testing_enabled; then
        current="$(path_for "${target}")"
      else
        current="${target}"
      fi
    else
      current="$(dirname -- "${current}")/${target}"
    fi
  done

  [[ -e "${current}" && ! -L "${current}" ]] || return 1
  parent="$(dirname -- "${current}")"
  base="$(basename -- "${current}")"
  canonical_parent="$(cd "${parent}" 2>/dev/null && pwd -P)" || return 1
  printf '%s/%s\n' "${canonical_parent%/}" "${base}"
}

resolve_os_release() {
  local etc_candidate usr_candidate selected resolved
  local trusted_etc trusted_usr
  validate_os_release_parents
  etc_candidate="$(path_for /etc/os-release)"
  usr_candidate="$(path_for /usr/lib/os-release)"
  trusted_etc="${etc_candidate}"
  trusted_usr="${usr_candidate}"

  if [[ -e "${etc_candidate}" || -L "${etc_candidate}" ]]; then
    selected="${etc_candidate}"
  elif [[ -e "${usr_candidate}" || -L "${usr_candidate}" ]]; then
    selected="${usr_candidate}"
  else
    die "cannot find OS metadata at /etc/os-release or /usr/lib/os-release."
  fi

  resolved="$(resolve_os_release_candidate "${selected}")" || die "cannot safely resolve the OS metadata file."
  case "${resolved}" in
    "${trusted_etc}"|"${trusted_usr}") ;;
    *) die "OS metadata resolves outside trusted paths." ;;
  esac
  printf '%s\n' "${resolved}"
}

validate_os_release_file() {
  local path="$1"
  local uid size
  [[ -f "${path}" && ! -L "${path}" && -r "${path}" ]] || die "resolved OS metadata is not a safe regular file."
  uid="$(os_release_stat_uid "${path}")" || die "cannot inspect OS metadata ownership."
  [[ "${uid}" =~ ^[0-9]+$ && "${uid}" == "0" ]] || die "OS metadata must be owned by root."
  validate_os_release_mode "${path}" "OS metadata file"
  size="$(os_release_stat_size "${path}")" || die "cannot inspect OS metadata size."
  [[ "${size}" =~ ^[0-9]+$ ]] || die "cannot validate OS metadata size."
  (( size <= OS_RELEASE_MAX_BYTES )) || die "OS metadata exceeds the ${OS_RELEASE_MAX_BYTES}-byte safety limit."
  if LC_ALL=C grep -q '[[:cntrl:]]' "${path}"; then
    die "OS metadata contains control characters."
  fi
}

parse_os_release_scalar() {
  local raw="$1"
  local value
  if [[ "${raw}" == \"* ]]; then
    [[ "${#raw}" -ge 2 && "${raw: -1}" == '"' ]] || return 1
    value="${raw:1:${#raw}-2}"
  elif [[ "${raw}" == \'* ]]; then
    [[ "${#raw}" -ge 2 && "${raw: -1}" == "'" ]] || return 1
    value="${raw:1:${#raw}-2}"
  else
    value="${raw}"
  fi
  [[ "${value}" =~ ^[A-Za-z0-9._-]+$ ]] || return 1
  printf '%s\n' "${value}"
}

read_os_release() {
  local os_release
  os_release="$(resolve_os_release)"
  validate_os_release_file "${os_release}"

  local id="" version_id="" line key raw value
  local id_seen=0 version_seen=0
  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ -z "${line}" || "${line}" == \#* ]] && continue
    [[ "${line}" =~ ^[A-Z_][A-Z0-9_]*=.*$ ]] || die "OS metadata contains a malformed key line."
    key="${line%%=*}"
    raw="${line#*=}"
    case "${key}" in
      ID)
        (( id_seen == 0 )) || die "OS metadata contains duplicate ID fields."
        value="$(parse_os_release_scalar "${raw}")" || die "OS metadata has a malformed ID field."
        id="${value}"
        id_seen=1
        ;;
      VERSION_ID)
        (( version_seen == 0 )) || die "OS metadata contains duplicate VERSION_ID fields."
        value="$(parse_os_release_scalar "${raw}")" || die "OS metadata has a malformed VERSION_ID field."
        version_id="${value}"
        version_seen=1
        ;;
    esac
  done < "${os_release}"

  (( id_seen == 1 )) || die "OS metadata is missing ID."
  (( version_seen == 1 )) || die "OS metadata is missing VERSION_ID."
  [[ "${id}" == "ubuntu" ]] || die "unsupported operating system: GOST Manager supports Ubuntu only."
  case "${version_id}" in
    22.04|24.04) ;;
    *) die "unsupported Ubuntu release: ${version_id:-unknown}; use Ubuntu 22.04 or 24.04." ;;
  esac
}

validate_architecture() {
  local architecture
  architecture="$(uname -m)"
  if testing_enabled && [[ -n "${GOST_MANAGER_SETUP_ARCH_TEST:-}" ]]; then
    architecture="${GOST_MANAGER_SETUP_ARCH_TEST}"
  fi
  case "${architecture}" in
    x86_64|amd64|aarch64|arm64) ;;
    *) die "unsupported architecture: ${architecture}" ;;
  esac
}

missing_dependencies() {
  MISSING_DEPENDENCIES=()
  if testing_enabled && [[ -n "${GOST_MANAGER_SETUP_MISSING_DEPS_TEST:-}" ]]; then
    read -r -a MISSING_DEPENDENCIES <<< "${GOST_MANAGER_SETUP_MISSING_DEPS_TEST}"
    return 0
  fi
  command -v curl >/dev/null 2>&1 || MISSING_DEPENDENCIES+=(curl)
  command -v tar >/dev/null 2>&1 || MISSING_DEPENDENCIES+=(tar)
  command -v gzip >/dev/null 2>&1 || MISSING_DEPENDENCIES+=(gzip)
  command -v sha256sum >/dev/null 2>&1 || MISSING_DEPENDENCIES+=(coreutils)
  [[ -r "$(path_for /etc/ssl/certs/ca-certificates.crt)" ]] || MISSING_DEPENDENCIES+=(ca-certificates)
}

ensure_dependencies() {
  missing_dependencies
  [[ "${#MISSING_DEPENDENCIES[@]}" -gt 0 ]] || return 0
  command -v apt-get >/dev/null 2>&1 || die "apt-get is required to install: ${MISSING_DEPENDENCIES[*]}"
  info "Installing setup dependencies: ${MISSING_DEPENDENCIES[*]}"
  DEBIAN_FRONTEND=noninteractive apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${MISSING_DEPENDENCIES[@]}"

  if testing_enabled && [[ -n "${GOST_MANAGER_SETUP_MISSING_DEPS_TEST:-}" ]]; then
    [[ "${GOST_MANAGER_SETUP_DEPENDENCIES_READY_TEST:-0}" == "1" ]] || die "required setup dependencies remain unavailable after installation."
    return 0
  fi
  missing_dependencies
  [[ "${#MISSING_DEPENDENCIES[@]}" -eq 0 ]] || die "required setup dependencies remain unavailable: ${MISSING_DEPENDENCIES[*]}"
}

normalize_version() {
  local requested="$1"
  requested="${requested#v}"
  [[ "${requested}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "invalid version: use a value such as 2.0.0 or v2.0.0."
  printf '%s\n' "${requested}"
}

download_file() {
  local url="$1"
  local destination="$2"
  curl \
    --fail \
    --silent \
    --show-error \
    --location \
    --proto '=https' \
    --tlsv1.2 \
    --retry 3 \
    --retry-delay 2 \
    --retry-connrefused \
    --output "${destination}" \
    "${url}"
  [[ -s "${destination}" ]] || die "downloaded file is empty: ${url}"
}

verify_checksum() {
  local checksum_file="$1"
  local archive_file="$2"
  local line="" has_extra=0
  {
    IFS= read -r line || true
    if IFS= read -r _; then
      has_extra=1
    fi
  } < "${checksum_file}"
  [[ -n "${line}" ]] || die "checksum file is empty."
  [[ "${has_extra}" == "0" ]] || die "checksum file must contain exactly one record."
  [[ "${line}" =~ ^[0-9a-f]{64}\ \ ${ARCHIVE_NAME}$ ]] || die "checksum file has an invalid format."
  if ! (
    cd "$(dirname "${archive_file}")"
    sha256sum -c "${CHECKSUM_NAME}"
  ) >/dev/null; then
    die "release checksum verification failed."
  fi
}

validate_member_path() {
  local member="$1"
  local normalized component
  local -a components=()
  [[ -n "${member}" && "${member}" != /* ]] || return 1
  [[ "${member}" =~ ^[A-Za-z0-9_@+.,=/:-]+$ ]] || return 1
  normalized="${member%/}"
  [[ -n "${normalized}" ]] || return 1
  IFS='/' read -r -a components <<< "${normalized}"
  for component in "${components[@]}"; do
    [[ -n "${component}" && "${component}" != "." && "${component}" != ".." ]] || return 1
  done
}

validate_archive() {
  local archive="$1"
  local listing="$2"
  local verbose="$3"
  local member normalized top="" member_top line type
  local -a required=(
    VERSION
    setup.sh
    install.sh
    gost-manager.sh
    lib/gost-run-iran.sh
    lib/gost-run-kharej.sh
    packaging/monitoring-runtime-manifest.txt
    monitoring/__init__.py
    packaging/watchdog-runtime-manifest.txt
    gost_watchdog/__init__.py
  )

  tar -tzf "${archive}" > "${listing}" || die "release archive cannot be listed."
  [[ -s "${listing}" ]] || die "release archive is empty."
  while IFS= read -r member || [[ -n "${member}" ]]; do
    validate_member_path "${member}" || die "release archive contains an unsafe path: ${member}"
    normalized="${member%/}"
    member_top="${normalized%%/*}"
    if [[ -z "${top}" ]]; then
      top="${member_top}"
    elif [[ "${member_top}" != "${top}" ]]; then
      die "release archive must contain exactly one top-level directory."
    fi
  done < "${listing}"
  [[ -z "$(LC_ALL=C sort "${listing}" | uniq -d)" ]] || die "release archive contains duplicate member names."

  tar -tvzf "${archive}" > "${verbose}" || die "release archive metadata cannot be read."
  while IFS= read -r line || [[ -n "${line}" ]]; do
    type="${line:0:1}"
    [[ "${type}" == "-" || "${type}" == "d" ]] || die "release archive contains a link or special file."
    [[ "${line}" != *" -> "* && "${line}" != *" link to "* ]] || die "release archive contains a link."
  done < "${verbose}"

  local required_path
  for required_path in "${required[@]}"; do
    grep -Fxq "${top}/${required_path}" "${listing}" || die "release archive is missing ${required_path}."
  done
  printf '%s\n' "${top}"
}

extract_and_verify_release() {
  local archive="$1"
  local expected_version="$2"
  local top release_root version
  top="$(validate_archive "${archive}" "${WORK_DIR}/archive.list" "${WORK_DIR}/archive.verbose")"
  mkdir -p "${WORK_DIR}/extract"
  tar -xzf "${archive}" --no-same-owner --no-same-permissions -C "${WORK_DIR}/extract"
  release_root="$(cd "${WORK_DIR}/extract/${top}" && pwd -P)"
  [[ "${release_root}" == "${WORK_DIR}/extract/"* ]] || die "release archive escaped the extraction directory."

  local required_path
  for required_path in VERSION setup.sh install.sh gost-manager.sh \
    lib/gost-run-iran.sh lib/gost-run-kharej.sh \
    packaging/monitoring-runtime-manifest.txt monitoring/__init__.py \
    packaging/watchdog-runtime-manifest.txt gost_watchdog/__init__.py; do
    [[ -f "${release_root}/${required_path}" && ! -L "${release_root}/${required_path}" ]] || die "extracted release file is missing or unsafe: ${required_path}"
  done
  version="$(< "${release_root}/VERSION")"
  [[ "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "release VERSION is invalid."
  [[ "${top}" == "GOST-Manager-${version}" ]] || die "release directory and VERSION do not match."
  if [[ -n "${expected_version}" && "${version}" != "${expected_version}" ]]; then
    die "downloaded release v${version} does not match requested v${expected_version}."
  fi
  printf '%s\n' "${release_root}"
}

validate_target() {
  local resolved_parent
  TARGET_DIR="$(path_for /opt/GOST-Manager)"
  TARGET_PARENT="$(dirname "${TARGET_DIR}")"
  [[ -d "${TARGET_PARENT}" && ! -L "${TARGET_PARENT}" ]] || die "source parent is missing or unsafe: ${TARGET_PARENT}"
  resolved_parent="$(cd "${TARGET_PARENT}" && pwd -P)"
  [[ "${resolved_parent}" == "${TARGET_PARENT}" ]] || die "source parent contains a symlink component: ${TARGET_PARENT}"
  if [[ -e "${TARGET_DIR}" || -L "${TARGET_DIR}" ]]; then
    [[ -d "${TARGET_DIR}" && ! -L "${TARGET_DIR}" ]] || die "existing source path is not a safe directory: ${TARGET_DIR}"
  fi
}

prepare_source_tree() {
  local release_root="$1"
  CANDIDATE_DIR="$(mktemp -d "${TARGET_PARENT}/.GOST-Manager.new.XXXXXX")"
  chmod 700 "${CANDIDATE_DIR}"
  cp -a "${release_root}/." "${CANDIDATE_DIR}/"
  validate_private_source_path "${CANDIDATE_DIR}" candidate || die "candidate source path is outside the managed source parent."
  [[ -d "${CANDIDATE_DIR}" && ! -L "${CANDIDATE_DIR}" ]] || die "candidate source path is unsafe."
  SOURCE_CANDIDATE_READY=1
}

activate_source_tree() {
  [[ "${SOURCE_CANDIDATE_READY}" == "1" ]] || die "source candidate is not ready."
  inject_setup_failure before_source_activation
  if [[ -d "${TARGET_DIR}" ]]; then
    BACKUP_DIR="$(mktemp -d "${TARGET_PARENT}/.GOST-Manager.backup.XXXXXX")"
    validate_private_source_path "${BACKUP_DIR}" backup || die "source backup path is outside the managed source parent."
    rmdir "${BACKUP_DIR}"
    mv -- "${TARGET_DIR}" "${BACKUP_DIR}"
    PRIOR_SOURCE_BACKED_UP=1
    [[ -d "${BACKUP_DIR}" && ! -L "${BACKUP_DIR}" ]] || die "prior source backup could not be verified."
  fi
  inject_setup_failure candidate_rename
  mv -- "${CANDIDATE_DIR}" "${TARGET_DIR}"
  NEW_SOURCE_ACTIVATED=1
  SOURCE_CANDIDATE_READY=0
  CANDIDATE_DIR=""
  [[ -d "${TARGET_DIR}" && ! -L "${TARGET_DIR}" ]] || die "activated source could not be verified."
  inject_setup_failure after_source_activation
}

run_local_installer() {
  local status
  [[ "${NEW_SOURCE_ACTIVATED}" == "1" ]] || die "local installer requires an activated source tree."
  if testing_enabled; then
    if (
      cd "${TARGET_DIR}"
      GOST_MANAGER_TESTING=1 \
      GOST_MANAGER_ROOT="${GOST_MANAGER_SETUP_ROOT}" \
      PYTHONPYCACHEPREFIX="${WORK_DIR}/pycache" \
        bash install.sh --install-dependencies
    ); then
      LOCAL_INSTALL_SUCCEEDED=1
      return 0
    else
      status=$?
    fi
  else
    if (
      cd "${TARGET_DIR}"
      bash install.sh --install-dependencies
    ); then
      LOCAL_INSTALL_SUCCEEDED=1
      return 0
    else
      status=$?
    fi
  fi
  [[ "${status}" != "70" ]] || LOCAL_INSTALL_ROLLBACK_UNVERIFIED=1
  return "${status}"
}

verify_runtime_source_alignment() {
  local expected_version="$1"
  local source_file installed_file manager_file source_version installed_version
  source_file="${TARGET_DIR}/VERSION"
  installed_file="$(path_for /usr/local/lib/gost-manager/VERSION)"
  manager_file="$(path_for /usr/local/sbin/gost-manager)"
  [[ -f "${source_file}" && ! -L "${source_file}" ]] || die "source VERSION file is missing or unsafe."
  [[ -f "${installed_file}" && ! -L "${installed_file}" ]] || die "installed VERSION file is missing or unsafe."
  [[ -f "${manager_file}" && ! -L "${manager_file}" && -x "${manager_file}" ]] || die "installed manager executable is missing or unsafe."
  source_version="$(< "${source_file}")"
  installed_version="$(< "${installed_file}")"
  [[ "${source_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "source VERSION is invalid after activation."
  [[ "${installed_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "installed VERSION is invalid after installation."
  [[ "${source_version}" == "${expected_version}" ]] || die "activated source version does not match release v${expected_version}."
  [[ "${installed_version}" == "${source_version}" ]] || die "installed version does not match activated source v${source_version}."
}

cleanup_prior_source_backup() {
  [[ "${PRIOR_SOURCE_BACKED_UP}" == "1" ]] || return 0
  if setup_failure_enabled backup_cleanup; then
    printf 'ERROR: injected setup failure at phase: backup_cleanup\n' >&2
    printf 'Retained source backup: %s\n' "${BACKUP_DIR}" >&2
    return 1
  fi
  remove_private_tree "${BACKUP_DIR}" backup || {
    printf 'Retained source backup: %s\n' "${BACKUP_DIR}" >&2
    return 1
  }
  BACKUP_DIR=""
  PRIOR_SOURCE_BACKED_UP=0
}

launch_or_explain() {
  local launcher
  launcher="$(path_for /usr/local/sbin/gost-manager)"
  if testing_enabled && [[ -n "${GOST_MANAGER_SETUP_LAUNCHER_TEST:-}" ]]; then
    launcher="${GOST_MANAGER_SETUP_LAUNCHER_TEST}"
  fi
  if { [[ -t 0 && -t 1 ]] || { testing_enabled && [[ "${GOST_MANAGER_SETUP_INTERACTIVE_TEST:-0}" == "1" ]]; }; }; then
    "${launcher}"
  else
    info "Run: gost-manager"
  fi
}

usage() {
  cat <<'USAGE'
Usage: setup.sh [--version X.Y.Z]

Without --version, installs the latest published release. A pinned install accepts
either MAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH.
USAGE
}

main() {
  local selected_version="${GOST_MANAGER_VERSION:-latest}"
  local requested_version="" release_path base archive_url checksum_url release_root version
  reject_production_test_controls
  validate_testing_root
  validate_test_failure_phases
  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      --version)
        [[ "$#" -ge 2 ]] || die "--version requires a value."
        selected_version="$2"
        shift 2
        ;;
      --help|-h)
        usage
        return 0
        ;;
      *) die "unknown setup option: $1" ;;
    esac
  done
  if [[ "${selected_version}" != "latest" ]]; then
    requested_version="$(normalize_version "${selected_version}")"
  fi

  require_root
  read_os_release
  validate_architecture
  ensure_dependencies
  validate_target

  base="${RELEASE_ORIGIN}"
  if testing_enabled && [[ -n "${GOST_MANAGER_RELEASE_BASE_URL_TEST:-}" ]]; then
    base="${GOST_MANAGER_RELEASE_BASE_URL_TEST%/}"
  fi
  if [[ -n "${requested_version}" ]]; then
    release_path="releases/download/v${requested_version}"
  else
    release_path="releases/latest/download"
  fi
  archive_url="${base}/${release_path}/${ARCHIVE_NAME}"
  checksum_url="${base}/${release_path}/${CHECKSUM_NAME}"

  info "GOST Manager setup"
  info "Selected version: ${selected_version}"
  WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gost-manager-setup.XXXXXX")"
  chmod 700 "${WORK_DIR}"
  info "Downloading checksum..."
  download_file "${checksum_url}" "${WORK_DIR}/${CHECKSUM_NAME}"
  info "Downloading release archive..."
  download_file "${archive_url}" "${WORK_DIR}/${ARCHIVE_NAME}"
  verify_checksum "${WORK_DIR}/${CHECKSUM_NAME}" "${WORK_DIR}/${ARCHIVE_NAME}"
  info "Checksum verified."
  release_root="$(extract_and_verify_release "${WORK_DIR}/${ARCHIVE_NAME}" "${requested_version}")"
  version="$(< "${release_root}/VERSION")"
  info "Archive safety verified."

  prepare_source_tree "${release_root}"
  info "Installing GOST Manager v${version}..."
  activate_source_tree
  run_local_installer
  verify_runtime_source_alignment "${version}"

  SETUP_COMMITTED=1
  cleanup_prior_source_backup || die "installation committed, but prior source backup cleanup failed."
  info "Source installed at: /opt/GOST-Manager"
  info "GOST Manager v${version} installed successfully."
  launch_or_explain
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
