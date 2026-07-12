#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "$(uname -s)" != "Linux" ]]; then
  printf 'SKIP: real systemd verification requires Linux\n'
  exit 0
fi

SYSTEMD_ANALYZE_REAL="$(type -P systemd-analyze || true)"
if [[ -z "${SYSTEMD_ANALYZE_REAL}" ]]; then
  printf 'SKIP: real systemd-analyze is unavailable on Linux\n'
  exit 0
fi

version_output="$("${SYSTEMD_ANALYZE_REAL}" --version)"
if [[ "${version_output}" != systemd* ]]; then
  printf 'FAIL: %s is not a real systemd-analyze binary\n' "${SYSTEMD_ANALYZE_REAL}" >&2
  exit 1
fi

TEST_HOME="$(mktemp -d "${TMPDIR:-/tmp}/gost-systemd-linux.XXXXXX")"
cleanup_test_home() {
  local status=$?
  rm -rf "${TEST_HOME}"
  exit "${status}"
}
trap cleanup_test_home EXIT

VERIFY_DIR="${TEST_HOME}/verify"
mkdir -p "${VERIFY_DIR}"
VERIFY_UNIT="${VERIFY_DIR}/gost-monitor-collector.service"
cp "${ROOT_DIR}/packaging/gost-monitor-collector.service" "${VERIFY_UNIT}"
cp "${ROOT_DIR}/packaging/monitoring.env" "${VERIFY_DIR}/monitoring.env"
printf '#!/usr/bin/env bash\nexit 0\n' > "${VERIFY_DIR}/gost-monitor-admin"
printf '#!/usr/bin/env bash\nexit 0\n' > "${VERIFY_DIR}/gost-monitor-collector"
chmod 755 "${VERIFY_DIR}/gost-monitor-admin" "${VERIFY_DIR}/gost-monitor-collector"
python3 -c \
  'import sys; from pathlib import Path; p=Path(sys.argv[1]); s=p.read_text(); s=s.replace("/usr/local/sbin/gost-monitor-admin", sys.argv[2]).replace("/usr/local/sbin/gost-monitor-collector", sys.argv[3]).replace("/etc/gost-manager/monitoring.env", sys.argv[4]); p.write_text(s)' \
  "${VERIFY_UNIT}" \
  "${VERIFY_DIR}/gost-monitor-admin" \
  "${VERIFY_DIR}/gost-monitor-collector" \
  "${VERIFY_DIR}/monitoring.env"

python3 - "${VERIFY_UNIT}" <<'PY'
import shlex
import sys
from pathlib import Path

unit = Path(sys.argv[1])
for raw in unit.read_text(encoding="utf-8").splitlines():
    if raw.startswith("EnvironmentFile="):
        path = Path(raw.split("=", 1)[1].lstrip("-"))
        if not path.is_file():
            raise SystemExit(f"missing EnvironmentFile fixture: {path}")
    if raw.startswith(("ExecStart=", "ExecStartPre=")):
        command = shlex.split(raw.split("=", 1)[1])[0]
        path = Path(command)
        if not path.is_file() or not path.stat().st_mode & 0o111:
            raise SystemExit(f"missing executable fixture: {path}")
PY

for forbidden in \
  'Requires=' 'PartOf=' 'BindsTo=' \
  'ExecStartPost=' 'ExecStop=' 'ExecStopPost=' 'ExecReload=' \
  'nginx.service' 'gost-iran-' 'gost-kharej-'; do
  if grep -Fq "${forbidden}" "${ROOT_DIR}/packaging/gost-monitor-collector.service"; then
    printf 'FAIL: production unit contains traffic dependency/lifecycle token: %s\n' "${forbidden}" >&2
    exit 1
  fi
done

VERIFY_OUTPUT="${TEST_HOME}/verify.out"
if ! "${SYSTEMD_ANALYZE_REAL}" verify "${VERIFY_UNIT}" > "${VERIFY_OUTPUT}" 2>&1; then
  cat "${VERIFY_OUTPUT}" >&2
  exit 1
fi
if [[ -s "${VERIFY_OUTPUT}" ]]; then
  cat "${VERIFY_OUTPUT}" >&2
  printf 'FAIL: systemd-analyze emitted a warning or error\n' >&2
  exit 1
fi

# shellcheck source=integration-test-lib.sh
source "${ROOT_DIR}/tests/integration-test-lib.sh"
COMMAND_LOG="${TEST_HOME}/commands.log"
STUB_STATE_DIR="${TEST_HOME}/state"
STUB_BIN="${TEST_HOME}/bin"
INSTALL_ROOT="${TEST_HOME}/root"
mkdir -p "${STUB_STATE_DIR}" "${INSTALL_ROOT}"
: > "${COMMAND_LOG}"
make_command_stubs "${STUB_BIN}"
rm -f "${STUB_BIN}/systemd-analyze"
export COMMAND_LOG STUB_STATE_DIR
PATH="${STUB_BIN}:${PATH}" \
GOST_MANAGER_TESTING=1 \
GOST_MANAGER_ROOT="${INSTALL_ROOT}" \
STUB_UNIT_PATH="${INSTALL_ROOT}/etc/systemd/system/gost-monitor-collector.service" \
SYSTEMD_ANALYZE_BIN="${SYSTEMD_ANALYZE_REAL}" \
PYTHONPYCACHEPREFIX="${TEST_HOME}/pycache" \
bash "${ROOT_DIR}/install.sh" >/dev/null

[[ -x "${INSTALL_ROOT}/usr/local/sbin/gost-monitor-collector" ]]
[[ -x "${INSTALL_ROOT}/usr/local/sbin/gost-monitor-admin" ]]
[[ -f "${INSTALL_ROOT}/etc/gost-manager/monitoring.env" ]]
[[ -f "${INSTALL_ROOT}/etc/systemd/system/gost-monitor-collector.service" ]]

distribution="unknown"
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  distribution="$(. /etc/os-release && printf '%s %s' "${NAME:-Linux}" "${VERSION_ID:-unknown}")"
fi
printf 'PASS: real systemd-analyze host verification\n'
printf 'PASS: temporary-root install with real systemd-analyze\n'
printf 'LINUX_DISTRIBUTION=%s\n' "${distribution}"
printf 'SYSTEMD_VERSION=%s\n' "$(printf '%s\n' "${version_output}" | sed -n '1p')"
