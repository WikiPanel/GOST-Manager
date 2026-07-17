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
  'gost-iran-' 'gost-kharej-'; do
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
  if grep -Fq "${VERIFY_UNIT}" "${VERIFY_OUTPUT}" || \
     grep -Fq "${VERIFY_UNIT##*/}" "${VERIFY_OUTPUT}" || \
     grep -Fq "${VERIFY_DIR}" "${VERIFY_OUTPUT}"; then
    cat "${VERIFY_OUTPUT}" >&2
    printf 'FAIL: systemd-analyze emitted a candidate warning or error\n' >&2
    exit 1
  fi
  printf 'INFO: systemd-analyze emitted only unrelated host-unit diagnostics (%s line(s))\n' \
    "$(wc -l < "${VERIFY_OUTPUT}" | tr -d ' ')"
fi

WATCHDOG_VERIFY_UNIT="${VERIFY_DIR}/gost-upstream-watchdog.service"
cp "${ROOT_DIR}/packaging/gost-upstream-watchdog.service" "${WATCHDOG_VERIFY_UNIT}"
cp "${ROOT_DIR}/packaging/watchdog.conf" "${VERIFY_DIR}/watchdog.conf"
mkdir -p "${VERIFY_DIR}/watchdog-state" "${VERIFY_DIR}/watchdog.d" \
  "${VERIFY_DIR}/gost" "${VERIFY_DIR}/systemd"
printf '#!/usr/bin/env bash\nexit 0\n' > "${VERIFY_DIR}/gost-watchdog-admin"
printf '#!/usr/bin/env bash\nexit 0\n' > "${VERIFY_DIR}/gost-upstream-watchdog"
chmod 755 "${VERIFY_DIR}/gost-watchdog-admin" "${VERIFY_DIR}/gost-upstream-watchdog"
python3 -c \
  'import functools, sys; from pathlib import Path; p=Path(sys.argv[1]); s=p.read_text(); pairs=zip(sys.argv[2::2],sys.argv[3::2]); p.write_text(functools.reduce(lambda value, pair: value.replace(*pair), pairs, s))' \
  "${WATCHDOG_VERIFY_UNIT}" \
  /usr/local/sbin/gost-watchdog-admin "${VERIFY_DIR}/gost-watchdog-admin" \
  /usr/local/sbin/gost-upstream-watchdog "${VERIFY_DIR}/gost-upstream-watchdog" \
  /var/lib/gost-manager/watchdog "${VERIFY_DIR}/watchdog-state" \
  /etc/gost-manager/watchdog.conf "${VERIFY_DIR}/watchdog.conf" \
  /etc/gost-manager/watchdog.d "${VERIFY_DIR}/watchdog.d" \
  /etc/gost "${VERIFY_DIR}/gost" \
  /etc/systemd/system "${VERIFY_DIR}/systemd"

for forbidden in \
  'Requires=' 'PartOf=' 'BindsTo=' \
  'ExecStartPost=' 'ExecStop=' 'ExecStopPost=' 'ExecReload=' \
  'gost-iran-' 'gost-kharej-'; do
  if grep -Fq "${forbidden}" "${ROOT_DIR}/packaging/gost-upstream-watchdog.service"; then
    printf 'FAIL: Watchdog unit contains traffic dependency/lifecycle token: %s\n' "${forbidden}" >&2
    exit 1
  fi
done

WATCHDOG_VERIFY_OUTPUT="${TEST_HOME}/watchdog-verify.out"
if ! "${SYSTEMD_ANALYZE_REAL}" verify "${WATCHDOG_VERIFY_UNIT}" > "${WATCHDOG_VERIFY_OUTPUT}" 2>&1; then
  cat "${WATCHDOG_VERIFY_OUTPUT}" >&2
  exit 1
fi
if [[ -s "${WATCHDOG_VERIFY_OUTPUT}" ]] && \
   { grep -Fq "${WATCHDOG_VERIFY_UNIT}" "${WATCHDOG_VERIFY_OUTPUT}" || \
     grep -Fq "${WATCHDOG_VERIFY_UNIT##*/}" "${WATCHDOG_VERIFY_OUTPUT}" || \
     grep -Fq "${VERIFY_DIR}" "${WATCHDOG_VERIFY_OUTPUT}"; }; then
  cat "${WATCHDOG_VERIFY_OUTPUT}" >&2
  printf 'FAIL: Watchdog unit emitted a systemd diagnostic\n' >&2
  exit 1
fi

STABILITY_VERIFY_UNIT="${VERIFY_DIR}/gost-iran-1.service"
cat > "${STABILITY_VERIFY_UNIT}" <<'UNIT'
[Unit]
Description=GOST Server Stability verification

[Service]
Type=simple
ExecStart=/bin/true

[Install]
WantedBy=multi-user.target
UNIT
(
  export GOST_MANAGER_TESTING=1
  # shellcheck source=../gost-manager.sh
  source "${ROOT_DIR}/gost-manager.sh"
  render_stability_systemd_override
) >> "${STABILITY_VERIFY_UNIT}"
STABILITY_VERIFY_OUTPUT="${TEST_HOME}/stability-verify.out"
if ! "${SYSTEMD_ANALYZE_REAL}" verify "${STABILITY_VERIFY_UNIT}" > "${STABILITY_VERIFY_OUTPUT}" 2>&1; then
  cat "${STABILITY_VERIFY_OUTPUT}" >&2
  exit 1
fi
if [[ -s "${STABILITY_VERIFY_OUTPUT}" ]] &&
   { grep -Fq "${STABILITY_VERIFY_UNIT}" "${STABILITY_VERIFY_OUTPUT}" ||
     grep -Fq "${STABILITY_VERIFY_UNIT##*/}" "${STABILITY_VERIFY_OUTPUT}"; }; then
  cat "${STABILITY_VERIFY_OUTPUT}" >&2
  printf 'FAIL: Server Stability override emitted a systemd diagnostic\n' >&2
  exit 1
fi

INVALID_DIR="${TEST_HOME}/invalid"
mkdir -p "${INVALID_DIR}"
INVALID_UNIT="${INVALID_DIR}/gost-monitor-collector-invalid.service"
cp "${VERIFY_UNIT}" "${INVALID_UNIT}"
printf '\nDefinitelyInvalidMonitoringDirective=true\n' >> "${INVALID_UNIT}"
INVALID_OUTPUT="${TEST_HOME}/invalid.out"
if "${SYSTEMD_ANALYZE_REAL}" verify "${INVALID_UNIT}" > "${INVALID_OUTPUT}" 2>&1 && \
   [[ ! -s "${INVALID_OUTPUT}" ]]; then
  printf 'FAIL: malformed staged unit produced no systemd-analyze diagnostic\n' >&2
  exit 1
fi
if ! grep -Fq "${INVALID_UNIT}" "${INVALID_OUTPUT}" && \
   ! grep -Fq "${INVALID_UNIT##*/}" "${INVALID_OUTPUT}"; then
  cat "${INVALID_OUTPUT}" >&2
  printf 'FAIL: malformed staged unit diagnostic was not attributable\n' >&2
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
[[ -x "${INSTALL_ROOT}/usr/local/sbin/gost-upstream-watchdog" ]]
[[ -x "${INSTALL_ROOT}/usr/local/sbin/gost-watchdog-admin" ]]
[[ -f "${INSTALL_ROOT}/etc/gost-manager/watchdog.conf" ]]
[[ -f "${INSTALL_ROOT}/etc/systemd/system/gost-upstream-watchdog.service" ]]
[[ -f "${INSTALL_ROOT}/var/lib/gost-manager/watchdog/watchdog.sqlite3" ]]

distribution="unknown"
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  distribution="$(. /etc/os-release && printf '%s %s' "${NAME:-Linux}" "${VERSION_ID:-unknown}")"
fi
printf 'PASS: real systemd-analyze host verification\n'
printf 'PASS: Watchdog unit verified by real systemd-analyze\n'
printf 'PASS: Server Stability override verified by real systemd-analyze\n'
printf 'PASS: malformed staged unit rejected by systemd-analyze\n'
printf 'PASS: temporary-root install with real systemd-analyze\n'
printf 'LINUX_DISTRIBUTION=%s\n' "${distribution}"
printf 'SYSTEMD_VERSION=%s\n' "$(printf '%s\n' "${version_output}" | sed -n '1p')"
