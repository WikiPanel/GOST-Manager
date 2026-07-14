#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=integration-test-lib.sh
source "${ROOT_DIR}/tests/integration-test-lib.sh"

TEST_HOME="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/gost-setup-tests.XXXXXX")" && pwd -P)"
cleanup_test_home() {
  local status=$?
  rm -rf "${TEST_HOME}"
  exit "${status}"
}
trap cleanup_test_home EXIT

STUB_BIN="${TEST_HOME}/bin"
mkdir -p "${STUB_BIN}"

cat > "${STUB_BIN}/curl" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
destination=""
url=""
printf 'curl' >> "${COMMAND_LOG}"
printf ' %q' "$@" >> "${COMMAND_LOG}"
printf '\n' >> "${COMMAND_LOG}"
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --output)
      destination="$2"
      shift 2
      ;;
    *)
      url="$1"
      shift
      ;;
  esac
done
[[ "${STUB_CURL_FAIL:-0}" != "1" ]] || exit 22
[[ "${url}" == https://* && -n "${destination}" ]]
cp "${FIXTURE_DOWNLOAD_DIR}/${url##*/}" "${destination}"
STUB

cat > "${STUB_BIN}/apt-get" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'apt-get %s\n' "$*" >> "${COMMAND_LOG}"
[[ "${STUB_APT_FAIL:-0}" != "1" ]]
STUB

cat > "${STUB_BIN}/manager-launcher" <<'STUB'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'launched\n' >> "${COMMAND_LOG}"
STUB
chmod 755 "${STUB_BIN}/curl" "${STUB_BIN}/apt-get" "${STUB_BIN}/manager-launcher"
make_command_stubs "${STUB_BIN}"

write_checksum() {
  local directory="$1"
  local digest
  digest="$(sha256sum "${directory}/gost-manager.tar.gz" | awk '{print $1}')"
  printf '%s  gost-manager.tar.gz\n' "${digest}" > "${directory}/gost-manager.tar.gz.sha256"
}

create_release_tree() {
  local directory="$1"
  local version="$2"
  local omit="${3:-}"
  local release_root="${directory}/GOST-Manager-${version}"
  mkdir -p "${release_root}/lib" "${release_root}/monitoring" "${release_root}/packaging"
  printf '%s\n' "${version}" > "${release_root}/VERSION"
  cp "${ROOT_DIR}/setup.sh" "${release_root}/setup.sh"
  cp "${ROOT_DIR}/gost-manager.sh" "${release_root}/gost-manager.sh"
  cp "${ROOT_DIR}/lib/gost-run-iran.sh" "${release_root}/lib/gost-run-iran.sh"
  cp "${ROOT_DIR}/lib/gost-run-kharej.sh" "${release_root}/lib/gost-run-kharej.sh"
  cp "${ROOT_DIR}/packaging/monitoring-runtime-manifest.txt" \
    "${release_root}/packaging/monitoring-runtime-manifest.txt"
  printf '# release fixture\n' > "${release_root}/monitoring/__init__.py"
  cat > "${release_root}/install.sh" <<'INSTALLER'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'installer %s\n' "$*" >> "${COMMAND_LOG}"
[[ "${STUB_INSTALL_FAIL:-0}" != "1" ]] || exit 42
root="${GOST_MANAGER_SETUP_ROOT%/}"
[[ "$(pwd -P)" == "${root}/opt/GOST-Manager" ]]
printf 'installer observed activated source\n' >> "${COMMAND_LOG}"
if [[ -n "${STUB_EXPECT_SOURCE_FILE:-}" ]]; then
  [[ -f "${root}/opt/GOST-Manager/${STUB_EXPECT_SOURCE_FILE}" ]]
fi
mkdir -p "${root}/usr/local/lib/gost-manager" "${root}/usr/local/sbin"
cp VERSION "${root}/usr/local/lib/gost-manager/VERSION"
cp gost-manager.sh "${root}/usr/local/sbin/gost-manager"
chmod 755 "${root}/usr/local/sbin/gost-manager"
INSTALLER
  chmod 755 "${release_root}/setup.sh" "${release_root}/install.sh" \
    "${release_root}/gost-manager.sh" "${release_root}/lib/gost-run-iran.sh" \
    "${release_root}/lib/gost-run-kharej.sh"
  if [[ -n "${omit}" ]]; then
    rm -f "${release_root}/${omit}"
  fi
}

create_release_assets() {
  local directory="$1"
  local version="${2:-2.0.0}"
  local omit="${3:-}"
  local build="${directory}/build"
  mkdir -p "${build}"
  create_release_tree "${build}" "${version}" "${omit}"
  tar -czf "${directory}/gost-manager.tar.gz" -C "${build}" "GOST-Manager-${version}"
  write_checksum "${directory}"
}

create_transactional_release_assets() {
  local directory="$1"
  local version="$2"
  local build="${directory}/build-transactional"
  local release_root="${build}/GOST-Manager-${version}"
  rm -rf "${build}"
  mkdir -p "${release_root}"
  cp "${ROOT_DIR}/VERSION" "${ROOT_DIR}/setup.sh" "${ROOT_DIR}/install.sh" \
    "${ROOT_DIR}/gost-manager.sh" "${release_root}/"
  cp -R "${ROOT_DIR}/lib" "${ROOT_DIR}/monitoring" "${ROOT_DIR}/packaging" \
    "${release_root}/"
  printf '%s\n' "${version}" > "${release_root}/VERSION"
  chmod 755 "${release_root}/setup.sh" "${release_root}/install.sh" \
    "${release_root}/gost-manager.sh" "${release_root}/lib/gost-run-iran.sh" \
    "${release_root}/lib/gost-run-kharej.sh"
  tar -czf "${directory}/gost-manager.tar.gz" -C "${build}" "GOST-Manager-${version}"
  write_checksum "${directory}"
}

create_malicious_assets() {
  local directory="$1"
  local kind="$2"
  local build="${directory}/build"
  mkdir -p "${build}"
  create_release_tree "${build}" "2.0.0"
  python3 - "${build}" "${directory}/gost-manager.tar.gz" "${kind}" <<'PY'
import io
import os
import tarfile
import sys

build, destination, kind = sys.argv[1:]
root = "GOST-Manager-2.0.0"
if kind == "required-symlink":
    os.unlink(os.path.join(build, root, "install.sh"))
with tarfile.open(destination, "w:gz") as archive:
    archive.add(os.path.join(build, root), arcname=root, recursive=True)
    if kind in {"absolute", "traversal"}:
        name = "/tmp/gost-manager-escape" if kind == "absolute" else root + "/../escape"
        payload = b"unsafe\n"
        member = tarfile.TarInfo(name)
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    elif kind == "symlink":
        member = tarfile.TarInfo(root + "/unsafe-link")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../outside"
        archive.addfile(member)
    elif kind == "hardlink":
        member = tarfile.TarInfo(root + "/unsafe-hardlink")
        member.type = tarfile.LNKTYPE
        member.linkname = "../../outside"
        archive.addfile(member)
    elif kind == "fifo":
        member = tarfile.TarInfo(root + "/unsafe-fifo")
        member.type = tarfile.FIFOTYPE
        archive.addfile(member)
    elif kind == "device":
        member = tarfile.TarInfo(root + "/unsafe-device")
        member.type = tarfile.CHRTYPE
        member.devmajor = 1
        member.devminor = 3
        archive.addfile(member)
    elif kind == "required-symlink":
        member = tarfile.TarInfo(root + "/install.sh")
        member.type = tarfile.SYMTYPE
        member.linkname = "gost-manager.sh"
        archive.addfile(member)
    elif kind == "second-root":
        payload = b"other\n"
        member = tarfile.TarInfo("another-root/file")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
PY
  write_checksum "${directory}"
}

new_case() {
  local name="$1"
  local case_dir="${TEST_HOME}/${name}"
  mkdir -p \
    "${case_dir}/root/opt" \
    "${case_dir}/root/etc/ssl/certs" \
    "${case_dir}/root/etc/gost" \
    "${case_dir}/root/etc/gost-manager" \
    "${case_dir}/root/etc/systemd/system" \
    "${case_dir}/root/etc/systemd/system/gost-iran-1.service.d" \
    "${case_dir}/root/etc/systemd/system/gost-kharej-1.service.d" \
    "${case_dir}/root/etc/sysctl.d" \
    "${case_dir}/root/var/lib/gost-manager" \
    "${case_dir}/assets" \
    "${case_dir}/state" \
    "${case_dir}/tmp"
  printf 'ID=ubuntu\nVERSION_ID="24.04"\n' > "${case_dir}/os-release"
  printf 'test-ca\n' > "${case_dir}/root/etc/ssl/certs/ca-certificates.crt"
  printf 'credential-canary\n' > "${case_dir}/root/etc/gost/iran-1.env"
  printf 'kharej-credential-canary\n' > "${case_dir}/root/etc/gost/kharej-1.env"
  printf 'monitor-config-canary\n' > "${case_dir}/root/etc/gost-manager/monitoring.env"
  printf 'service-canary\n' > "${case_dir}/root/etc/systemd/system/gost-iran-1.service"
  printf 'kharej-service-canary\n' > "${case_dir}/root/etc/systemd/system/gost-kharej-1.service"
  printf 'iran-dropin-canary\n' > "${case_dir}/root/etc/systemd/system/gost-iran-1.service.d/override.conf"
  printf 'kharej-dropin-canary\n' > "${case_dir}/root/etc/systemd/system/gost-kharej-1.service.d/override.conf"
  printf 'sysctl-canary\n' > "${case_dir}/root/etc/sysctl.d/99-gost-stability.conf"
  printf 'history-canary\n' > "${case_dir}/root/var/lib/gost-manager/history.canary"
  : > "${case_dir}/commands.log"
  printf '%s\n' "${case_dir}"
}

run_setup() {
  local case_dir="$1"
  local output="$2"
  shift 2
  COMMAND_LOG="${case_dir}/commands.log" \
  FIXTURE_DOWNLOAD_DIR="${case_dir}/assets" \
  GOST_MANAGER_SETUP_TESTING=1 \
  GOST_MANAGER_SETUP_ROOT="${case_dir}/root" \
  GOST_MANAGER_SETUP_EUID_TEST="${SETUP_EUID_TEST:-0}" \
  GOST_MANAGER_SETUP_ARCH_TEST="${SETUP_ARCH_TEST:-x86_64}" \
  GOST_MANAGER_SETUP_OS_RELEASE_TEST="${SETUP_OS_RELEASE_TEST:-${case_dir}/os-release}" \
  GOST_MANAGER_RELEASE_BASE_URL_TEST="https://fixtures.example/WikiPanel/GOST-Manager" \
  GOST_MANAGER_SETUP_MISSING_DEPS_TEST="${SETUP_MISSING_DEPS_TEST:-}" \
  GOST_MANAGER_SETUP_DEPENDENCIES_READY_TEST="${SETUP_DEPENDENCIES_READY_TEST:-}" \
  GOST_MANAGER_SETUP_INTERACTIVE_TEST="${SETUP_INTERACTIVE_TEST:-0}" \
  GOST_MANAGER_SETUP_LAUNCHER_TEST="${STUB_BIN}/manager-launcher" \
  GOST_MANAGER_SETUP_FAIL_PHASE_TEST="${SETUP_FAIL_PHASE_TEST:-}" \
  GOST_MANAGER_VERSION="${SETUP_VERSION_TEST:-latest}" \
  GOST_MANAGER_FAIL_PHASE="${INSTALL_FAIL_PHASE_TEST:-}" \
  GOST_MANAGER_INSTALLED_VERSION_OVERRIDE_TEST="${INSTALL_VERSION_OVERRIDE_TEST:-}" \
  STUB_CURL_FAIL="${STUB_CURL_FAIL:-0}" \
  STUB_APT_FAIL="${STUB_APT_FAIL:-0}" \
  STUB_INSTALL_FAIL="${STUB_INSTALL_FAIL:-0}" \
  STUB_EXPECT_SOURCE_FILE="${STUB_EXPECT_SOURCE_FILE:-}" \
  STUB_STATE_DIR="${case_dir}/state" \
  STUB_UNIT_PATH="${case_dir}/root/etc/systemd/system/gost-monitor-collector.service" \
  TMPDIR="${case_dir}/tmp" \
  PATH="${STUB_BIN}:${PATH}" \
    bash "${ROOT_DIR}/setup.sh" "$@" > "${output}" 2>&1
}

assert_setup_success() {
  local name="$1"
  local case_dir="$2"
  shift 2
  if run_setup "${case_dir}" "${case_dir}/setup.out" "$@"; then
    pass "${name}"
  else
    fail "${name}"
  fi
}

assert_setup_failure() {
  local name="$1"
  local case_dir="$2"
  shift 2
  if run_setup "${case_dir}" "${case_dir}/setup.out" "$@"; then
    fail "${name}"
  else
    pass "${name}"
  fi
}

assert_no_setup_temp() {
  local name="$1"
  local case_dir="$2"
  local count
  count="$(find "${case_dir}/tmp" -maxdepth 1 -name 'gost-manager-setup.*' -print | wc -l | tr -d ' ')"
  assert_eq "${name}" "0" "${count}"
}

managed_runtime_digest() {
  local case_dir="$1"
  local relative path
  {
    for relative in \
      usr/local/sbin \
      usr/local/lib/gost-manager \
      etc/gost-manager \
      etc/systemd/system/gost-monitor-collector.service; do
      path="${case_dir}/root/${relative}"
      printf 'PATH:%s\n' "${relative}"
      if [[ -e "${path}" || -L "${path}" ]]; then
        filesystem_manifest "${path}"
      else
        printf 'absent\n'
      fi
    done
  } | cksum | awk '{print $1":"$2}'
}

installed_manager_version() {
  local case_dir="$1"
  GOST_MANAGER_TESTING=1 \
  GOST_MANAGER_VERSION_FILE_TEST="${case_dir}/root/usr/local/lib/gost-manager/VERSION" \
    bash -c 'source "$1"; manager_banner' _ \
      "${case_dir}/root/usr/local/sbin/gost-manager"
}

assert_old_transaction_state() {
  local prefix="$1"
  local case_dir="$2"
  local source_digest="$3"
  local runtime_digest="$4"
  assert_eq "${prefix} keeps installed runtime VERSION" "1.9.0" \
    "$(< "${case_dir}/root/usr/local/lib/gost-manager/VERSION")"
  assert_eq "${prefix} keeps source VERSION" "1.9.0" \
    "$(< "${case_dir}/root/opt/GOST-Manager/VERSION")"
  assert_eq "${prefix} restores prior source digest" "${source_digest}" \
    "$(tree_digest "${case_dir}/root/opt/GOST-Manager")"
  assert_eq "${prefix} restores prior runtime digest" "${runtime_digest}" \
    "$(managed_runtime_digest "${case_dir}")"
}

assert_new_transaction_alignment() {
  local prefix="$1"
  local case_dir="$2"
  assert_eq "${prefix} installs runtime VERSION" "2.0.0" \
    "$(< "${case_dir}/root/usr/local/lib/gost-manager/VERSION")"
  assert_eq "${prefix} installs source VERSION" "2.0.0" \
    "$(< "${case_dir}/root/opt/GOST-Manager/VERSION")"
  assert_eq "${prefix} manager reports aligned VERSION" "GOST Manager v2.0.0" \
    "$(installed_manager_version "${case_dir}")"
}

latest_case="$(new_case latest)"
create_release_assets "${latest_case}/assets"
assert_setup_success "latest release setup succeeds" "${latest_case}"
assert_contains "latest archive URL selected" "/releases/latest/download/gost-manager.tar.gz" "${latest_case}/commands.log"
assert_contains "latest checksum URL selected" "/releases/latest/download/gost-manager.tar.gz.sha256" "${latest_case}/commands.log"
for curl_flag in "--fail" "--silent" "--show-error" "--location" "--proto" "=https" "--tlsv1.2" "--retry" "--retry-connrefused"; do
  assert_contains "curl uses ${curl_flag}" "${curl_flag}" "${latest_case}/commands.log"
done
assert_contains "local installer receives dependency option" "installer --install-dependencies" "${latest_case}/commands.log"
assert_contains "success prints exact version" "GOST Manager v2.0.0 installed successfully." "${latest_case}/setup.out"
assert_contains "noninteractive setup prints launch command" "Run: gost-manager" "${latest_case}/setup.out"
assert_not_contains "noninteractive setup does not launch manager" "launched" "${latest_case}/commands.log"
assert_file "source tree installed under opt" "${latest_case}/root/opt/GOST-Manager/setup.sh"
assert_eq "source tree VERSION is exact" "2.0.0" "$(< "${latest_case}/root/opt/GOST-Manager/VERSION")"
assert_eq "installed VERSION is exact" "2.0.0" "$(< "${latest_case}/root/usr/local/lib/gost-manager/VERSION")"
assert_file "installed manager exists" "${latest_case}/root/usr/local/sbin/gost-manager"
assert_eq "stored source reports its version" "GOST Manager v2.0.0" \
  "$(bash "${latest_case}/root/opt/GOST-Manager/gost-manager.sh" --version)"
assert_contains "stored source menu renders version" "GOST Manager v2.0.0" \
  <(printf '0\n' | bash "${latest_case}/root/opt/GOST-Manager/gost-manager.sh")
assert_eq "Direct credentials preserved" "credential-canary" "$(< "${latest_case}/root/etc/gost/iran-1.env")"
assert_eq "Kharej credentials preserved" "kharej-credential-canary" "$(< "${latest_case}/root/etc/gost/kharej-1.env")"
assert_eq "monitoring configuration preserved" "monitor-config-canary" "$(< "${latest_case}/root/etc/gost-manager/monitoring.env")"
assert_eq "Direct systemd unit preserved" "service-canary" "$(< "${latest_case}/root/etc/systemd/system/gost-iran-1.service")"
assert_eq "Kharej systemd unit preserved" "kharej-service-canary" "$(< "${latest_case}/root/etc/systemd/system/gost-kharej-1.service")"
assert_eq "Iran service drop-in preserved" "iran-dropin-canary" "$(< "${latest_case}/root/etc/systemd/system/gost-iran-1.service.d/override.conf")"
assert_eq "Kharej service drop-in preserved" "kharej-dropin-canary" "$(< "${latest_case}/root/etc/systemd/system/gost-kharej-1.service.d/override.conf")"
assert_eq "stability configuration preserved" "sysctl-canary" "$(< "${latest_case}/root/etc/sysctl.d/99-gost-stability.conf")"
assert_eq "monitoring history preserved" "history-canary" "$(< "${latest_case}/root/var/lib/gost-manager/history.canary")"
assert_not_contains "setup never invokes systemctl" "systemctl" "${latest_case}/commands.log"
assert_no_setup_temp "successful setup removes download workspace" "${latest_case}"

source_digest="$(tree_digest "${latest_case}/root/opt/GOST-Manager")"
assert_setup_success "same-version rerun succeeds" "${latest_case}"
assert_eq "same-version rerun is source-idempotent" "${source_digest}" "$(tree_digest "${latest_case}/root/opt/GOST-Manager")"
assert_eq "same-version rerun preserves credentials" "credential-canary" "$(< "${latest_case}/root/etc/gost/iran-1.env")"
assert_no_setup_temp "same-version rerun removes workspace" "${latest_case}"

pinned_case="$(new_case pinned)"
create_release_assets "${pinned_case}/assets"
SETUP_VERSION_TEST=2.0.0 assert_setup_success "pinned version without v succeeds" "${pinned_case}"
assert_contains "pinned URL uses normalized tag" "/releases/download/v2.0.0/gost-manager.tar.gz" "${pinned_case}/commands.log"
SETUP_VERSION_TEST=v2.0.0 assert_setup_success "pinned version with v succeeds" "${pinned_case}"
assert_contains "pinned v input remains normalized" "/releases/download/v2.0.0/gost-manager.tar.gz.sha256" "${pinned_case}/commands.log"

upgrade_case="$(new_case newer-upgrade)"
create_release_assets "${upgrade_case}/assets" 1.9.0
SETUP_VERSION_TEST=v1.9.0 assert_setup_success "older pinned release installs" "${upgrade_case}"
assert_eq "older release VERSION installed" "1.9.0" "$(< "${upgrade_case}/root/usr/local/lib/gost-manager/VERSION")"
create_release_assets "${upgrade_case}/assets" 2.0.0
assert_setup_success "newer release upgrade succeeds" "${upgrade_case}"
assert_eq "newer release replaces installed VERSION" "2.0.0" "$(< "${upgrade_case}/root/usr/local/lib/gost-manager/VERSION")"
assert_eq "newer release preserves monitoring config" "monitor-config-canary" "$(< "${upgrade_case}/root/etc/gost-manager/monitoring.env")"

interactive_case="$(new_case interactive)"
create_release_assets "${interactive_case}/assets"
SETUP_INTERACTIVE_TEST=1 assert_setup_success "interactive setup succeeds" "${interactive_case}"
assert_contains "interactive setup launches manager" "launched" "${interactive_case}/commands.log"

checksum_case="$(new_case checksum)"
create_release_assets "${checksum_case}/assets"
printf '%064d  gost-manager.tar.gz\n' 0 > "${checksum_case}/assets/gost-manager.tar.gz.sha256"
assert_setup_failure "checksum mismatch is rejected" "${checksum_case}"
assert_contains "checksum mismatch is reported" "checksum verification failed" "${checksum_case}/setup.out"
assert_absent "checksum failure does not replace source" "${checksum_case}/root/opt/GOST-Manager"
assert_no_setup_temp "checksum failure removes workspace" "${checksum_case}"

format_case="$(new_case checksum-format)"
create_release_assets "${format_case}/assets"
printf 'not-a-checksum gost-manager.tar.gz\n' > "${format_case}/assets/gost-manager.tar.gz.sha256"
assert_setup_failure "malformed checksum record is rejected" "${format_case}"
assert_contains "malformed checksum has useful error" "invalid format" "${format_case}/setup.out"

empty_case="$(new_case empty-download)"
create_release_assets "${empty_case}/assets"
: > "${empty_case}/assets/gost-manager.tar.gz"
assert_setup_failure "empty archive download is rejected" "${empty_case}"
assert_contains "empty archive is reported" "downloaded file is empty" "${empty_case}/setup.out"

missing_archive_case="$(new_case missing-archive)"
create_release_assets "${missing_archive_case}/assets"
rm -f "${missing_archive_case}/assets/gost-manager.tar.gz"
assert_setup_failure "missing archive download is rejected" "${missing_archive_case}"
assert_absent "missing archive leaves source absent" "${missing_archive_case}/root/opt/GOST-Manager"

missing_checksum_case="$(new_case missing-checksum)"
create_release_assets "${missing_checksum_case}/assets"
rm -f "${missing_checksum_case}/assets/gost-manager.tar.gz.sha256"
assert_setup_failure "missing checksum download is rejected" "${missing_checksum_case}"
assert_absent "missing checksum leaves source absent" "${missing_checksum_case}/root/opt/GOST-Manager"

empty_checksum_case="$(new_case empty-checksum)"
create_release_assets "${empty_checksum_case}/assets"
: > "${empty_checksum_case}/assets/gost-manager.tar.gz.sha256"
assert_setup_failure "empty checksum download is rejected" "${empty_checksum_case}"
assert_contains "empty checksum is reported" "downloaded file is empty" "${empty_checksum_case}/setup.out"

filename_case="$(new_case checksum-filename)"
create_release_assets "${filename_case}/assets"
digest="$(sha256sum "${filename_case}/assets/gost-manager.tar.gz" | awk '{print $1}')"
printf '%s  other.tar.gz\n' "${digest}" > "${filename_case}/assets/gost-manager.tar.gz.sha256"
assert_setup_failure "unexpected checksum filename is rejected" "${filename_case}"
assert_contains "unexpected checksum filename is reported" "invalid format" "${filename_case}/setup.out"

download_case="$(new_case download-failure)"
create_release_assets "${download_case}/assets"
STUB_CURL_FAIL=1 assert_setup_failure "download retry exhaustion fails setup" "${download_case}"
assert_contains "download uses three retries" "--retry 3" "${download_case}/commands.log"
assert_absent "download failure leaves source absent" "${download_case}/root/opt/GOST-Manager"
assert_no_setup_temp "download failure removes workspace" "${download_case}"

invalid_version_case="$(new_case invalid-version)"
create_release_assets "${invalid_version_case}/assets"
assert_setup_failure "malformed pinned version is rejected" "${invalid_version_case}" --version '2.0.0;id'
assert_contains "malformed version has useful error" "invalid version" "${invalid_version_case}/setup.out"
assert_eq "malformed version performs no download" "0" "$(wc -l < "${invalid_version_case}/commands.log" | tr -d ' ')"

prerelease_case="$(new_case prerelease-version)"
create_release_assets "${prerelease_case}/assets"
SETUP_VERSION_TEST=v2.0.0-beta assert_setup_failure "prerelease version is rejected" "${prerelease_case}"
assert_eq "prerelease rejection performs no download" "0" "$(wc -l < "${prerelease_case}/commands.log" | tr -d ' ')"

non_root_case="$(new_case non-root)"
create_release_assets "${non_root_case}/assets"
SETUP_EUID_TEST=1000 assert_setup_failure "non-root setup is rejected" "${non_root_case}"
assert_contains "non-root guidance mentions sudo" "sudo bash" "${non_root_case}/setup.out"

unsupported_os_case="$(new_case unsupported-os)"
create_release_assets "${unsupported_os_case}/assets"
printf 'ID=debian\nVERSION_ID="12"\n' > "${unsupported_os_case}/os-release"
assert_setup_failure "unsupported operating system is rejected" "${unsupported_os_case}"
assert_contains "unsupported OS is reported" "supports Ubuntu only" "${unsupported_os_case}/setup.out"

unsupported_release_case="$(new_case unsupported-release)"
create_release_assets "${unsupported_release_case}/assets"
printf 'ID=ubuntu\nVERSION_ID="20.04"\n' > "${unsupported_release_case}/os-release"
assert_setup_failure "unsupported Ubuntu release is rejected" "${unsupported_release_case}"
assert_contains "supported Ubuntu releases are named" "Ubuntu 22.04 or 24.04" "${unsupported_release_case}/setup.out"

unsupported_arch_case="$(new_case unsupported-arch)"
create_release_assets "${unsupported_arch_case}/assets"
SETUP_ARCH_TEST=riscv64 assert_setup_failure "unsupported architecture is rejected" "${unsupported_arch_case}"
assert_contains "unsupported architecture is reported" "riscv64" "${unsupported_arch_case}/setup.out"

dependency_case="$(new_case dependencies)"
create_release_assets "${dependency_case}/assets"
SETUP_MISSING_DEPS_TEST="curl ca-certificates" SETUP_DEPENDENCIES_READY_TEST=1 \
  assert_setup_success "missing dependencies are installed" "${dependency_case}"
assert_contains "dependency setup updates apt metadata" "apt-get update" "${dependency_case}/commands.log"
assert_contains "dependency setup uses no recommends" "--no-install-recommends curl ca-certificates" "${dependency_case}/commands.log"

dependency_fail_case="$(new_case dependency-failure)"
create_release_assets "${dependency_fail_case}/assets"
SETUP_MISSING_DEPS_TEST="curl" STUB_APT_FAIL=1 \
  assert_setup_failure "failed dependency installation aborts" "${dependency_fail_case}"
assert_absent "dependency failure leaves source absent" "${dependency_fail_case}/root/opt/GOST-Manager"

production_control_out="${TEST_HOME}/production-control.out"
if GOST_MANAGER_SETUP_ROOT="${TEST_HOME}/forbidden" bash "${ROOT_DIR}/setup.sh" > "${production_control_out}" 2>&1; then
  fail "production rejects test-only controls"
else
  pass "production rejects test-only controls"
fi
assert_contains "test-control rejection is explicit" "test-only setting" "${production_control_out}"

unsafe_root_out="${TEST_HOME}/unsafe-root.out"
if GOST_MANAGER_SETUP_TESTING=1 GOST_MANAGER_SETUP_ROOT=/ \
  bash "${ROOT_DIR}/setup.sh" > "${unsafe_root_out}" 2>&1; then
  fail "testing mode refuses slash root"
else
  pass "testing mode refuses slash root"
fi
assert_contains "unsafe test root is reported" "non-root absolute test directory" "${unsafe_root_out}"

target_link_case="$(new_case target-link)"
create_release_assets "${target_link_case}/assets"
mkdir -p "${target_link_case}/outside"
printf 'outside-canary\n' > "${target_link_case}/outside/canary"
ln -s "${target_link_case}/outside" "${target_link_case}/root/opt/GOST-Manager"
assert_setup_failure "symlink source target is rejected" "${target_link_case}"
assert_eq "symlink target remains untouched" "outside-canary" "$(< "${target_link_case}/outside/canary")"

target_file_case="$(new_case target-file)"
create_release_assets "${target_file_case}/assets"
printf 'not-a-directory\n' > "${target_file_case}/root/opt/GOST-Manager"
assert_setup_failure "non-directory source target is rejected" "${target_file_case}"
assert_eq "non-directory target remains untouched" "not-a-directory" "$(< "${target_file_case}/root/opt/GOST-Manager")"

parent_link_case="$(new_case parent-link)"
create_release_assets "${parent_link_case}/assets"
rm -rf "${parent_link_case}/root/opt"
mkdir -p "${parent_link_case}/outside-opt"
ln -s "${parent_link_case}/outside-opt" "${parent_link_case}/root/opt"
assert_setup_failure "symlink source parent is rejected" "${parent_link_case}"
assert_absent "symlink parent receives no source tree" "${parent_link_case}/outside-opt/GOST-Manager"

for malicious_kind in traversal absolute symlink hardlink fifo device required-symlink second-root; do
  malicious_case="$(new_case "archive-${malicious_kind}")"
  create_malicious_assets "${malicious_case}/assets" "${malicious_kind}"
  assert_setup_failure "${malicious_kind} archive is rejected" "${malicious_case}"
  assert_absent "${malicious_kind} archive does not replace source" "${malicious_case}/root/opt/GOST-Manager"
  assert_no_setup_temp "${malicious_kind} archive removes workspace" "${malicious_case}"
done

missing_case="$(new_case missing-required)"
create_release_assets "${missing_case}/assets" 2.0.0 install.sh
assert_setup_failure "archive missing required file is rejected" "${missing_case}"
assert_contains "missing required file is reported" "missing install.sh" "${missing_case}/setup.out"

mismatch_case="$(new_case version-mismatch)"
create_release_assets "${mismatch_case}/assets" 2.0.1
SETUP_VERSION_TEST=2.0.0 assert_setup_failure "pinned release mismatch is rejected" "${mismatch_case}"
assert_contains "pinned mismatch is reported" "does not match requested" "${mismatch_case}/setup.out"

existing_source_case="$(new_case existing-source)"
create_release_assets "${existing_source_case}/assets"
mkdir -p "${existing_source_case}/root/opt/GOST-Manager"
printf 'replace-me\n' > "${existing_source_case}/root/opt/GOST-Manager/old-source"
STUB_EXPECT_SOURCE_FILE=install.sh assert_setup_success "existing source replacement succeeds" "${existing_source_case}"
assert_contains "local installer runs from activated source" "installer observed activated source" "${existing_source_case}/commands.log"
assert_absent "successful replacement removes old managed source" "${existing_source_case}/root/opt/GOST-Manager/old-source"
assert_file "successful replacement preserves complete new source" "${existing_source_case}/root/opt/GOST-Manager/install.sh"

transaction_base="$(new_case transaction-base-1.9.0)"
cp "${ROOT_DIR}/packaging/monitoring.env" \
  "${transaction_base}/root/etc/gost-manager/monitoring.env"
create_transactional_release_assets "${transaction_base}/assets" 1.9.0
SETUP_VERSION_TEST=v1.9.0 assert_setup_success \
  "transaction baseline installs through real local installer" "${transaction_base}"
assert_eq "transaction baseline runtime VERSION is old" "1.9.0" \
  "$(< "${transaction_base}/root/usr/local/lib/gost-manager/VERSION")"
assert_eq "transaction baseline source VERSION is old" "1.9.0" \
  "$(< "${transaction_base}/root/opt/GOST-Manager/VERSION")"

new_transaction_case() {
  local name="$1"
  local case_dir
  case_dir="$(new_case "${name}")"
  rm -rf "${case_dir}/root" "${case_dir}/state"
  mkdir -p "${case_dir}/root" "${case_dir}/state"
  cp -a "${transaction_base}/root/." "${case_dir}/root/"
  touch "${case_dir}/state/enabled" "${case_dir}/state/active"
  : > "${case_dir}/commands.log"
  create_transactional_release_assets "${case_dir}/assets" 2.0.0
  printf '%s\n' "${case_dir}"
}

before_activation_case="$(new_transaction_case failure-before-source-activation)"
before_source_digest="$(tree_digest "${before_activation_case}/root/opt/GOST-Manager")"
before_runtime_digest="$(managed_runtime_digest "${before_activation_case}")"
SETUP_FAIL_PHASE_TEST=before_source_activation assert_setup_failure \
  "failure before source activation returns non-zero" "${before_activation_case}"
assert_old_transaction_state "failure before source activation" "${before_activation_case}" \
  "${before_source_digest}" "${before_runtime_digest}"
assert_no_setup_temp "failure before source activation removes workspace" "${before_activation_case}"

after_activation_case="$(new_transaction_case failure-after-source-activation)"
after_source_digest="$(tree_digest "${after_activation_case}/root/opt/GOST-Manager")"
after_runtime_digest="$(managed_runtime_digest "${after_activation_case}")"
SETUP_FAIL_PHASE_TEST=after_source_activation assert_setup_failure \
  "failure after source activation returns non-zero" "${after_activation_case}"
assert_old_transaction_state "failure after source activation" "${after_activation_case}" \
  "${after_source_digest}" "${after_runtime_digest}"

installer_pre_mutation_case="$(new_transaction_case installer-failure-before-mutation)"
pre_mutation_source_digest="$(tree_digest "${installer_pre_mutation_case}/root/opt/GOST-Manager")"
pre_mutation_runtime_digest="$(managed_runtime_digest "${installer_pre_mutation_case}")"
INSTALL_FAIL_PHASE_TEST=unit_validation assert_setup_failure \
  "installer failure before mutation returns non-zero" "${installer_pre_mutation_case}"
assert_old_transaction_state "installer failure before mutation" "${installer_pre_mutation_case}" \
  "${pre_mutation_source_digest}" "${pre_mutation_runtime_digest}"

installer_partial_case="$(new_transaction_case installer-partial-file-replacement)"
partial_source_digest="$(tree_digest "${installer_partial_case}/root/opt/GOST-Manager")"
partial_runtime_digest="$(managed_runtime_digest "${installer_partial_case}")"
INSTALL_FAIL_PHASE_TEST=partial_file_replacement assert_setup_failure \
  "installer failure after partial replacement returns non-zero" "${installer_partial_case}"
assert_old_transaction_state "installer partial replacement rollback" "${installer_partial_case}" \
  "${partial_source_digest}" "${partial_runtime_digest}"
assert_contains "partial replacement invokes installer rollback" \
  "injected installer failure at phase: partial_file_replacement" \
  "${installer_partial_case}/setup.out"

installed_mismatch_case="$(new_transaction_case installed-version-mismatch)"
mismatch_source_digest="$(tree_digest "${installed_mismatch_case}/root/opt/GOST-Manager")"
mismatch_runtime_digest="$(managed_runtime_digest "${installed_mismatch_case}")"
INSTALL_VERSION_OVERRIDE_TEST=9.9.9 assert_setup_failure \
  "installed VERSION mismatch returns non-zero" "${installed_mismatch_case}"
assert_old_transaction_state "installed VERSION mismatch rollback" "${installed_mismatch_case}" \
  "${mismatch_source_digest}" "${mismatch_runtime_digest}"
assert_contains "installed VERSION mismatch is caught inside installer" \
  "does not match source VERSION" "${installed_mismatch_case}/setup.out"

unverified_installer_case="$(new_transaction_case installer-rollback-unverified)"
STUB_FAIL_SYSTEMCTL_ACTION=stop INSTALL_FAIL_PHASE_TEST=collector_start \
  assert_setup_failure "unverified installer rollback returns non-zero" \
    "${unverified_installer_case}"
unverified_source_backup="$(find "${unverified_installer_case}/root/opt" -maxdepth 1 \
  -type d -name '.GOST-Manager.backup.*' -print -quit)"
unverified_runtime_backup="$(find "${unverified_installer_case}/root" \
  -type d -name '*.gost-manager-backup.*' -print -quit)"
assert_new_transaction_alignment "unverified installer rollback" "${unverified_installer_case}"
assert_dir "unverified installer rollback retains prior source backup" \
  "${unverified_source_backup}"
assert_dir "unverified installer rollback retains managed runtime backup" \
  "${unverified_runtime_backup}"
assert_eq "unverified installer rollback retains old source VERSION" "1.9.0" \
  "$(< "${unverified_source_backup}/VERSION")"
assert_contains "unverified installer rollback reports installer recovery" \
  "Installer rollback could not be verified" "${unverified_installer_case}/setup.out"
assert_contains "unverified installer rollback skips unsafe source rollback" \
  "source rollback was skipped" "${unverified_installer_case}/setup.out"
assert_contains "unverified installer rollback prints exact source backup" \
  "${unverified_source_backup}" "${unverified_installer_case}/setup.out"

rename_failure_case="$(new_transaction_case candidate-rename-failure)"
rename_source_digest="$(tree_digest "${rename_failure_case}/root/opt/GOST-Manager")"
rename_runtime_digest="$(managed_runtime_digest "${rename_failure_case}")"
SETUP_FAIL_PHASE_TEST=candidate_rename assert_setup_failure \
  "candidate rename failure returns non-zero" "${rename_failure_case}"
assert_old_transaction_state "candidate rename failure" "${rename_failure_case}" \
  "${rename_source_digest}" "${rename_runtime_digest}"

restore_failure_case="$(new_transaction_case source-restore-failure)"
restore_source_digest="$(tree_digest "${restore_failure_case}/root/opt/GOST-Manager")"
restore_runtime_digest="$(managed_runtime_digest "${restore_failure_case}")"
SETUP_FAIL_PHASE_TEST=after_source_activation,source_restore assert_setup_failure \
  "source restore failure returns non-zero" "${restore_failure_case}"
restore_backup="$(find "${restore_failure_case}/root/opt" -maxdepth 1 \
  -type d -name '.GOST-Manager.backup.*' -print -quit)"
assert_dir "source restore failure retains prior source backup" "${restore_backup}"
assert_eq "source restore failure retains old backup VERSION" "1.9.0" \
  "$(< "${restore_backup}/VERSION")"
assert_eq "source restore failure retains exact prior source digest" \
  "${restore_source_digest}" "$(tree_digest "${restore_backup}")"
assert_eq "source restore failure leaves runtime unchanged" \
  "${restore_runtime_digest}" "$(managed_runtime_digest "${restore_failure_case}")"
assert_eq "source restore failure reports current activated source" "2.0.0" \
  "$(< "${restore_failure_case}/root/opt/GOST-Manager/VERSION")"
assert_contains "source restore failure reports unverified rollback" \
  "source rollback could not be verified" "${restore_failure_case}/setup.out"
assert_contains "source restore failure prints exact backup path" \
  "${restore_backup}" "${restore_failure_case}/setup.out"
assert_not_contains "source restore failure never claims installation success" \
  "installed successfully" "${restore_failure_case}/setup.out"

cleanup_failure_case="$(new_transaction_case source-backup-cleanup-failure)"
SETUP_FAIL_PHASE_TEST=backup_cleanup assert_setup_failure \
  "backup cleanup failure returns non-zero after commit" "${cleanup_failure_case}"
cleanup_backup="$(find "${cleanup_failure_case}/root/opt" -maxdepth 1 \
  -type d -name '.GOST-Manager.backup.*' -print -quit)"
assert_new_transaction_alignment "backup cleanup failure" "${cleanup_failure_case}"
assert_dir "backup cleanup failure retains private backup" "${cleanup_backup}"
assert_eq "backup cleanup failure retains old source VERSION" "1.9.0" \
  "$(< "${cleanup_backup}/VERSION")"
assert_contains "backup cleanup failure prints exact retained path" \
  "${cleanup_backup}" "${cleanup_failure_case}/setup.out"
assert_not_contains "backup cleanup failure does not report source rollback" \
  "source rollback could not be verified" "${cleanup_failure_case}/setup.out"

successful_transaction_case="$(new_transaction_case successful-newer-upgrade)"
traffic_before="$(tree_digest "${successful_transaction_case}/root/etc/gost")"
history_before="$(cksum "${successful_transaction_case}/root/var/lib/gost-manager/history.canary")"
db_path="${successful_transaction_case}/root/var/lib/gost-manager/metrics.sqlite3"
db_inode_before="$(stat -c '%i' "${db_path}" 2>/dev/null || stat -f '%i' "${db_path}")"
assert_setup_success "newer transactional upgrade succeeds" "${successful_transaction_case}"
assert_new_transaction_alignment "newer transactional upgrade" "${successful_transaction_case}"
assert_eq "newer upgrade preserves tunnel env digest" "${traffic_before}" \
  "$(tree_digest "${successful_transaction_case}/root/etc/gost")"
assert_eq "newer upgrade preserves monitoring history canary" "${history_before}" \
  "$(cksum "${successful_transaction_case}/root/var/lib/gost-manager/history.canary")"
assert_eq "newer upgrade preserves monitoring database inode" "${db_inode_before}" \
  "$(stat -c '%i' "${db_path}" 2>/dev/null || stat -f '%i' "${db_path}")"
assert_eq "newer upgrade preserves monitoring schema version" "4" \
  "$(python3 -c 'import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); print(db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0])' "${db_path}")"
assert_eq "successful commit removes prior source backup" "0" \
  "$(find "${successful_transaction_case}/root/opt" -maxdepth 1 \
    -type d -name '.GOST-Manager.backup.*' -print | wc -l | tr -d ' ')"

same_source_digest="$(tree_digest "${successful_transaction_case}/root/opt/GOST-Manager")"
same_runtime_digest="$(managed_runtime_digest "${successful_transaction_case}")"
: > "${successful_transaction_case}/commands.log"
assert_setup_success "same-version transactional reinstall succeeds" "${successful_transaction_case}"
assert_new_transaction_alignment "same-version transactional reinstall" "${successful_transaction_case}"
assert_eq "same-version reinstall keeps source digest" "${same_source_digest}" \
  "$(tree_digest "${successful_transaction_case}/root/opt/GOST-Manager")"
assert_eq "same-version reinstall keeps runtime digest" "${same_runtime_digest}" \
  "$(managed_runtime_digest "${successful_transaction_case}")"
assert_not_contains "transaction tests never target Iran traffic lifecycle" \
  "gost-iran-" "${successful_transaction_case}/commands.log"
assert_not_contains "transaction tests never target Kharej traffic lifecycle" \
  "gost-kharej-" "${successful_transaction_case}/commands.log"

direct_install_root="${TEST_HOME}/direct-local/root"
direct_stub_state="${TEST_HOME}/direct-local/state"
direct_command_log="${TEST_HOME}/direct-local/commands.log"
mkdir -p "${direct_install_root}" "${direct_stub_state}"
: > "${direct_command_log}"
if COMMAND_LOG="${direct_command_log}" STUB_STATE_DIR="${direct_stub_state}" \
  STUB_UNIT_PATH="${direct_install_root}/etc/systemd/system/gost-monitor-collector.service" \
  GOST_MANAGER_TESTING=1 GOST_MANAGER_ROOT="${direct_install_root}" \
  PYTHONPYCACHEPREFIX="${TEST_HOME}/direct-local/pycache" PATH="${STUB_BIN}:${PATH}" \
  bash "${ROOT_DIR}/install.sh" > "${TEST_HOME}/direct-local/install.out" 2>&1; then
  pass "local install.sh path still succeeds"
else
  fail "local install.sh path still succeeds"
fi
assert_file "local install.sh installs VERSION" "${direct_install_root}/usr/local/lib/gost-manager/VERSION"
assert_file "local install.sh installs manager" "${direct_install_root}/usr/local/sbin/gost-manager"

version_fallback_dir="${TEST_HOME}/version-fallback"
mkdir -p "${version_fallback_dir}"
cp "${ROOT_DIR}/gost-manager.sh" "${version_fallback_dir}/gost-manager"
assert_eq "version fallback is safe" "GOST Manager version unknown" \
  "$(GOST_MANAGER_TESTING=0 bash "${version_fallback_dir}/gost-manager" --version)"

assert_not_contains "setup contains no traffic service command" "gost-iran-" "${ROOT_DIR}/setup.sh"
assert_not_contains "setup contains no Kharej service command" "gost-kharej-" "${ROOT_DIR}/setup.sh"
assert_not_contains "setup contains no firewall command" "iptables" "${ROOT_DIR}/setup.sh"
assert_not_contains "setup contains no nftables command" "nft" "${ROOT_DIR}/setup.sh"
assert_not_contains "setup contains no kernel module command" "modprobe" "${ROOT_DIR}/setup.sh"
assert_not_contains "setup never runs Server Stability" "run_stability" "${ROOT_DIR}/setup.sh"
assert_absent "bootstrap entrypoint is not present" "${ROOT_DIR}/bootstrap.sh"

finish_suite
