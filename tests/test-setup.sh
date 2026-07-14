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
if [[ -n "${STUB_EXPECT_SOURCE_FILE:-}" ]]; then
  [[ -f "${root}/opt/GOST-Manager/${STUB_EXPECT_SOURCE_FILE}" ]]
  printf 'installer observed old source\n' >> "${COMMAND_LOG}"
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
  GOST_MANAGER_SETUP_FAIL_AFTER_SOURCE_REPLACE_TEST="${SETUP_FAIL_AFTER_REPLACE_TEST:-0}" \
  GOST_MANAGER_VERSION="${SETUP_VERSION_TEST:-latest}" \
  STUB_CURL_FAIL="${STUB_CURL_FAIL:-0}" \
  STUB_APT_FAIL="${STUB_APT_FAIL:-0}" \
  STUB_INSTALL_FAIL="${STUB_INSTALL_FAIL:-0}" \
  STUB_EXPECT_SOURCE_FILE="${STUB_EXPECT_SOURCE_FILE:-}" \
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

replace_failure_case="$(new_case replacement-failure)"
create_release_assets "${replace_failure_case}/assets"
mkdir -p "${replace_failure_case}/root/opt/GOST-Manager"
printf 'old-source\n' > "${replace_failure_case}/root/opt/GOST-Manager/canary"
source_before="$(tree_digest "${replace_failure_case}/root/opt/GOST-Manager")"
SETUP_FAIL_AFTER_REPLACE_TEST=1 assert_setup_failure "source replacement failure is reported" "${replace_failure_case}"
assert_eq "failed source replacement restores prior tree" "${source_before}" "$(tree_digest "${replace_failure_case}/root/opt/GOST-Manager")"
assert_no_setup_temp "replacement failure removes workspace" "${replace_failure_case}"

installer_failure_case="$(new_case installer-failure)"
create_release_assets "${installer_failure_case}/assets"
mkdir -p "${installer_failure_case}/root/opt/GOST-Manager"
printf 'old-source\n' > "${installer_failure_case}/root/opt/GOST-Manager/canary"
installer_source_before="$(tree_digest "${installer_failure_case}/root/opt/GOST-Manager")"
STUB_INSTALL_FAIL=1 assert_setup_failure "local installer failure aborts setup" "${installer_failure_case}"
assert_eq "installer failure restores prior source" "${installer_source_before}" "$(tree_digest "${installer_failure_case}/root/opt/GOST-Manager")"
assert_eq "installer failure preserves credentials" "credential-canary" "$(< "${installer_failure_case}/root/etc/gost/iran-1.env")"
assert_no_setup_temp "installer failure removes workspace" "${installer_failure_case}"

existing_source_case="$(new_case existing-source)"
create_release_assets "${existing_source_case}/assets"
mkdir -p "${existing_source_case}/root/opt/GOST-Manager"
printf 'replace-me\n' > "${existing_source_case}/root/opt/GOST-Manager/old-source"
STUB_EXPECT_SOURCE_FILE=old-source assert_setup_success "existing source replacement succeeds" "${existing_source_case}"
assert_contains "local installer runs before source replacement" "installer observed old source" "${existing_source_case}/commands.log"
assert_absent "successful replacement removes old managed source" "${existing_source_case}/root/opt/GOST-Manager/old-source"
assert_file "successful replacement preserves complete new source" "${existing_source_case}/root/opt/GOST-Manager/install.sh"

direct_install_root="${TEST_HOME}/direct-local/root"
direct_stub_state="${TEST_HOME}/direct-local/state"
direct_command_log="${TEST_HOME}/direct-local/commands.log"
mkdir -p "${direct_install_root}" "${direct_stub_state}"
: > "${direct_command_log}"
make_command_stubs "${STUB_BIN}"
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
