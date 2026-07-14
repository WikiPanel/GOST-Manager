#!/usr/bin/env bash
set -Eeuo pipefail

readonly RELEASE_ORIGIN="https://github.com/WikiPanel/GOST-Manager"
readonly ARCHIVE_NAME="gost-manager.tar.gz"
readonly CHECKSUM_NAME="gost-manager.tar.gz.sha256"

WORK_DIR=""
CANDIDATE_DIR=""
BACKUP_DIR=""
TARGET_DIR=""
TARGET_PARENT=""
SOURCE_REPLACED=0
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
    GOST_MANAGER_RELEASE_BASE_URL_TEST \
    GOST_MANAGER_SETUP_MISSING_DEPS_TEST \
    GOST_MANAGER_SETUP_DEPENDENCIES_READY_TEST \
    GOST_MANAGER_SETUP_INTERACTIVE_TEST \
    GOST_MANAGER_SETUP_LAUNCHER_TEST \
    GOST_MANAGER_SETUP_FAIL_AFTER_SOURCE_REPLACE_TEST; do
    [[ -z "${!name:-}" ]] || die "${name} is a test-only setting."
  done
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

remove_private_tree() {
  local path="$1"
  [[ -n "${path}" && "${path}" != "/" ]] || return 0
  case "${path}" in
    "${TARGET_PARENT}"/.GOST-Manager.new.*|"${TARGET_PARENT}"/.GOST-Manager.backup.*|"${WORK_DIR}")
      rm -rf -- "${path}"
      ;;
    *)
      die "refusing to remove an unexpected setup path: ${path}"
      ;;
  esac
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM

  if [[ "${status}" -ne 0 && "${SOURCE_REPLACED}" == "1" && "${SETUP_COMMITTED}" == "0" ]]; then
    if [[ -e "${TARGET_DIR}" || -L "${TARGET_DIR}" ]]; then
      rm -rf -- "${TARGET_DIR}"
    fi
    if [[ -n "${BACKUP_DIR}" && -d "${BACKUP_DIR}" && ! -L "${BACKUP_DIR}" ]]; then
      if mv -- "${BACKUP_DIR}" "${TARGET_DIR}"; then
        BACKUP_DIR=""
      else
        printf 'ERROR: source rollback failed; recovery source remains at %s\n' "${BACKUP_DIR}" >&2
      fi
    fi
  fi

  if [[ -n "${CANDIDATE_DIR}" && -d "${CANDIDATE_DIR}" && ! -L "${CANDIDATE_DIR}" ]]; then
    remove_private_tree "${CANDIDATE_DIR}"
  fi
  if [[ -n "${WORK_DIR}" && -d "${WORK_DIR}" && ! -L "${WORK_DIR}" ]]; then
    remove_private_tree "${WORK_DIR}"
  fi
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

read_os_release() {
  local os_release
  os_release="$(path_for /etc/os-release)"
  if testing_enabled && [[ -n "${GOST_MANAGER_SETUP_OS_RELEASE_TEST:-}" ]]; then
    os_release="${GOST_MANAGER_SETUP_OS_RELEASE_TEST}"
  fi
  [[ -f "${os_release}" && ! -L "${os_release}" ]] || die "cannot read a safe /etc/os-release file."

  local id="" version_id="" key value
  while IFS='=' read -r key value || [[ -n "${key}" ]]; do
    value="${value%\"}"
    value="${value#\"}"
    case "${key}" in
      ID) id="${value}" ;;
      VERSION_ID) version_id="${value}" ;;
    esac
  done < "${os_release}"

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
    packaging/monitoring-runtime-manifest.txt monitoring/__init__.py; do
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
  TARGET_DIR="$(path_for /opt/GOST-Manager)"
  TARGET_PARENT="$(dirname "${TARGET_DIR}")"
  [[ -d "${TARGET_PARENT}" && ! -L "${TARGET_PARENT}" ]] || die "source parent is missing or unsafe: ${TARGET_PARENT}"
  if [[ -e "${TARGET_DIR}" || -L "${TARGET_DIR}" ]]; then
    [[ -d "${TARGET_DIR}" && ! -L "${TARGET_DIR}" ]] || die "existing source path is not a safe directory: ${TARGET_DIR}"
  fi
}

prepare_source_tree() {
  local release_root="$1"
  CANDIDATE_DIR="$(mktemp -d "${TARGET_PARENT}/.GOST-Manager.new.XXXXXX")"
  chmod 700 "${CANDIDATE_DIR}"
  cp -a "${release_root}/." "${CANDIDATE_DIR}/"
}

replace_source_tree() {
  if [[ -d "${TARGET_DIR}" ]]; then
    BACKUP_DIR="$(mktemp -d "${TARGET_PARENT}/.GOST-Manager.backup.XXXXXX")"
    rmdir "${BACKUP_DIR}"
    mv -- "${TARGET_DIR}" "${BACKUP_DIR}"
  fi
  SOURCE_REPLACED=1
  mv -- "${CANDIDATE_DIR}" "${TARGET_DIR}"
  CANDIDATE_DIR=""
  if testing_enabled && [[ "${GOST_MANAGER_SETUP_FAIL_AFTER_SOURCE_REPLACE_TEST:-0}" == "1" ]]; then
    die "injected failure after source replacement."
  fi
}

run_local_installer() {
  local release_root="$1"
  (
    cd "${release_root}"
    bash install.sh --install-dependencies
  )
}

verify_installed_version() {
  local expected_version="$1"
  local installed_file installed_version
  installed_file="$(path_for /usr/local/lib/gost-manager/VERSION)"
  [[ -f "${installed_file}" && ! -L "${installed_file}" ]] || die "installed VERSION file is missing or unsafe."
  installed_version="$(< "${installed_file}")"
  [[ "${installed_version}" == "${expected_version}" ]] || die "installed version does not match release v${expected_version}."
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
  run_local_installer "${CANDIDATE_DIR}"
  verify_installed_version "${version}"
  replace_source_tree

  SETUP_COMMITTED=1
  if [[ -n "${BACKUP_DIR}" ]]; then
    remove_private_tree "${BACKUP_DIR}"
    BACKUP_DIR=""
  fi
  info "Source installed at: /opt/GOST-Manager"
  info "GOST Manager v${version} installed successfully."
  launch_or_explain
}

main "$@"
