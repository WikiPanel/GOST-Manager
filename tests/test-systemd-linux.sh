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

GATEWAY_VERIFY_DIR="${TEST_HOME}/gateway-verify"
mkdir -p "${GATEWAY_VERIFY_DIR}/secrets" "${GATEWAY_VERIFY_DIR}/generated/exits" \
  "${GATEWAY_VERIFY_DIR}/systemd"
gateway_test_user="user-$(printf '%s' "${RANDOM}${RANDOM}" | shasum | cut -c1-12)"
gateway_test_pass="pass-$(printf '%s' "${RANDOM}${RANDOM}${RANDOM}" | shasum | cut -c1-16)"
printf 'GOST_USER=%s\nGOST_PASS=%s\n' "${gateway_test_user}" "${gateway_test_pass}" \
  > "${GATEWAY_VERIFY_DIR}/secrets/secret-ee-primary.env"
printf 'GATEWAY_EXIT_ID=ee-primary\nGATEWAY_LISTEN_ADDRESS=127.0.0.1\nGATEWAY_LISTEN_PORT=18081\nGATEWAY_EXIT_HOST=exit.example.org\nGATEWAY_SOCKS_PORT=28420\nGATEWAY_TARGET_ADDRESS=127.0.0.1\nGATEWAY_TARGET_PORT=18081\n' \
  > "${GATEWAY_VERIFY_DIR}/generated/exits/ee-primary.env"
printf '#!/usr/bin/env bash\nexit 0\n' > "${GATEWAY_VERIFY_DIR}/gost-run-gateway-exit.sh"
chmod 755 "${GATEWAY_VERIFY_DIR}/gost-run-gateway-exit.sh"
GATEWAY_UNIT="${GATEWAY_VERIFY_DIR}/systemd/gost-gateway-exit-ee-primary.service"
PYTHONPATH="${ROOT_DIR}" python3 - "${GATEWAY_VERIFY_DIR}" "${GATEWAY_UNIT}" <<'PY'
import sys
from pathlib import Path
from gateway.runtime_models import DesiredExitRuntime
from gateway.runtime_paths import RuntimePaths, service_name
from gateway.runtime_render import render_unit

root = Path(sys.argv[1]).resolve()
paths = RuntimePaths.from_values(
    root / "secrets", root / "generated", root / "backups", root / "runtime.lock",
    root / "systemd", root / "gost-run-gateway-exit.sh", root / "gost",
)
desired = DesiredExitRuntime(
    "ee-primary", service_name("ee-primary"), "127.0.0.1", 18081,
    "exit.example.org", 28420, "127.0.0.1", 18081,
    "secret-ee-primary", 1,
)
Path(sys.argv[2]).write_bytes(render_unit(desired, paths))
PY

for required in 'LimitNOFILE=200000' 'Gateway Exit ee-primary'; do
  grep -Fq "${required}" "${GATEWAY_UNIT}" || {
    printf 'FAIL: generated gateway unit lacks %s\n' "${required}" >&2
    exit 1
  }
done
for forbidden in 'nginx.service' 'gost-iran-' 'gost-kharej-' \
  'gost-monitor-collector.service' 'PrivateNetwork=true'; do
  if grep -Fq "${forbidden}" "${GATEWAY_UNIT}"; then
    printf 'FAIL: generated gateway unit contains forbidden token: %s\n' "${forbidden}" >&2
    exit 1
  fi
done
GATEWAY_VERIFY_OUTPUT="${TEST_HOME}/gateway-verify.out"
if ! "${SYSTEMD_ANALYZE_REAL}" verify "${GATEWAY_UNIT}" > "${GATEWAY_VERIFY_OUTPUT}" 2>&1; then
  cat "${GATEWAY_VERIFY_OUTPUT}" >&2
  exit 1
fi
if grep -Fq "${GATEWAY_UNIT##*/}" "${GATEWAY_VERIFY_OUTPUT}"; then
  cat "${GATEWAY_VERIFY_OUTPUT}" >&2
  printf 'FAIL: systemd-analyze emitted a gateway candidate warning\n' >&2
  exit 1
fi

NGINX_VERIFY_DIR="${TEST_HOME}/nginx-verify"
mkdir -p "${NGINX_VERIFY_DIR}"
NGINX_UNIT="${NGINX_VERIFY_DIR}/gost-nginx-gateway.service"
NGINX_RUNNER="${NGINX_VERIFY_DIR}/gost-run-nginx-gateway.sh"
cp "${ROOT_DIR}/packaging/gost-nginx-gateway.service" "${NGINX_UNIT}"
printf '#!/usr/bin/env bash\nexit 0\n' > "${NGINX_RUNNER}"
chmod 755 "${NGINX_RUNNER}"
python3 -c \
  'import sys; from pathlib import Path; p=Path(sys.argv[1]); s=p.read_text(); s=s.replace("/usr/local/lib/gost-manager/gost-run-nginx-gateway.sh", sys.argv[2]); p.write_text(s)' \
  "${NGINX_UNIT}" "${NGINX_RUNNER}"
for required in \
  'LimitNOFILE=200000' 'RuntimeDirectory=gost-manager-nginx' \
  'RuntimeDirectoryMode=0700' 'TasksMax=4096' 'KillMode=mixed'; do
  grep -Fq "${required}" "${NGINX_UNIT}" || {
    printf 'FAIL: dedicated NGINX unit lacks %s\n' "${required}" >&2
    exit 1
  }
done
for forbidden in \
  'nginx.service' 'gost-gateway-exit-' 'gost-monitor-collector.service' \
  'PrivateNetwork=true' 'iptables' 'nft'; do
  if grep -Fq "${forbidden}" "${NGINX_UNIT}"; then
    printf 'FAIL: dedicated NGINX unit contains forbidden token: %s\n' "${forbidden}" >&2
    exit 1
  fi
done
NGINX_VERIFY_OUTPUT="${TEST_HOME}/nginx-verify.out"
if ! "${SYSTEMD_ANALYZE_REAL}" verify "${NGINX_UNIT}" > "${NGINX_VERIFY_OUTPUT}" 2>&1; then
  cat "${NGINX_VERIFY_OUTPUT}" >&2
  exit 1
fi
if grep -Fq "${NGINX_UNIT##*/}" "${NGINX_VERIFY_OUTPUT}"; then
  cat "${NGINX_VERIFY_OUTPUT}" >&2
  printf 'FAIL: systemd-analyze emitted a dedicated NGINX candidate warning\n' >&2
  exit 1
fi
NGINX_INVALID_UNIT="${NGINX_VERIFY_DIR}/gost-nginx-gateway-invalid.service"
cp "${NGINX_UNIT}" "${NGINX_INVALID_UNIT}"
printf '\nDefinitelyInvalidNginxGatewayDirective=true\n' >> "${NGINX_INVALID_UNIT}"
NGINX_INVALID_OUTPUT="${TEST_HOME}/nginx-invalid.out"
if "${SYSTEMD_ANALYZE_REAL}" verify "${NGINX_INVALID_UNIT}" > "${NGINX_INVALID_OUTPUT}" 2>&1 && \
   [[ ! -s "${NGINX_INVALID_OUTPUT}" ]]; then
  printf 'FAIL: malformed dedicated NGINX unit produced no diagnostic\n' >&2
  exit 1
fi
grep -Fq "${NGINX_INVALID_UNIT##*/}" "${NGINX_INVALID_OUTPUT}" || {
  cat "${NGINX_INVALID_OUTPUT}" >&2
  printf 'FAIL: malformed dedicated NGINX diagnostic was not attributable\n' >&2
  exit 1
}

GATEWAY_INVALID_UNIT="${GATEWAY_VERIFY_DIR}/systemd/gost-gateway-exit-invalid.service"
cp "${GATEWAY_UNIT}" "${GATEWAY_INVALID_UNIT}"
printf '\nDefinitelyInvalidGatewayDirective=true\n' >> "${GATEWAY_INVALID_UNIT}"
GATEWAY_INVALID_OUTPUT="${TEST_HOME}/gateway-invalid.out"
if "${SYSTEMD_ANALYZE_REAL}" verify "${GATEWAY_INVALID_UNIT}" > "${GATEWAY_INVALID_OUTPUT}" 2>&1 && \
   [[ ! -s "${GATEWAY_INVALID_OUTPUT}" ]]; then
  printf 'FAIL: malformed gateway unit produced no diagnostic\n' >&2
  exit 1
fi
if ! grep -Fq "${GATEWAY_INVALID_UNIT##*/}" "${GATEWAY_INVALID_OUTPUT}"; then
  cat "${GATEWAY_INVALID_OUTPUT}" >&2
  printf 'FAIL: malformed gateway unit diagnostic was not attributable\n' >&2
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
[[ -x "${INSTALL_ROOT}/usr/local/sbin/gost-gateway-nginx" ]]
[[ -x "${INSTALL_ROOT}/usr/local/lib/gost-manager/gost-run-nginx-gateway.sh" ]]
[[ -f "${INSTALL_ROOT}/etc/systemd/system/gost-nginx-gateway.service" ]]

distribution="unknown"
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  distribution="$(. /etc/os-release && printf '%s %s' "${NAME:-Linux}" "${VERSION_ID:-unknown}")"
fi
printf 'PASS: real systemd-analyze host verification\n'
printf 'PASS: malformed staged unit rejected by systemd-analyze\n'
printf 'PASS: temporary-root install with real systemd-analyze\n'
printf 'PASS: generated gateway Exit unit verified by real systemd-analyze\n'
printf 'PASS: malformed gateway Exit unit rejected by real systemd-analyze\n'
printf 'PASS: dedicated NGINX Gateway unit verified by real systemd-analyze\n'
printf 'PASS: malformed dedicated NGINX Gateway unit rejected by real systemd-analyze\n'
printf 'LINUX_DISTRIBUTION=%s\n' "${distribution}"
printf 'SYSTEMD_VERSION=%s\n' "$(printf '%s\n' "${version_output}" | sed -n '1p')"
