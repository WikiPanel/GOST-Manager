#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "$(uname -s)" != "Linux" ]]; then
  printf 'SKIP: Watchdog hardened runtime test requires Linux\n'
  exit 0
fi
if ! command -v systemd-run >/dev/null 2>&1 || \
   ! command -v systemctl >/dev/null 2>&1 || \
   [[ "$(ps -p 1 -o comm= 2>/dev/null)" != "systemd" ]]; then
  printf 'SKIP: Watchdog hardened runtime test requires a running systemd\n'
  exit 0
fi

SUDO=()
if [[ "${EUID}" -ne 0 ]]; then
  if ! sudo -n true >/dev/null 2>&1; then
    printf 'SKIP: Watchdog hardened runtime test requires passwordless sudo\n'
    exit 0
  fi
  SUDO=(sudo -n)
fi

TEST_ID="gost-watchdog-runtime-$$"
BASE="/var/lib/gost-manager/${TEST_ID}"
ROOT="${BASE}/root"
RUNTIME="${BASE}/runtime"
STATE="${ROOT}/var/lib/gost-manager/watchdog"
cleanup() {
  "${SUDO[@]}" systemctl reset-failed "${TEST_ID}.service" >/dev/null 2>&1 || true
  "${SUDO[@]}" rm -rf "${BASE}"
}
trap cleanup EXIT

"${SUDO[@]}" install -d -m 0700 \
  "${ROOT}/etc/gost-manager/watchdog.d" \
  "${ROOT}/etc/gost" \
  "${ROOT}/etc/systemd/system" \
  "${STATE}" \
  "${RUNTIME}"
"${SUDO[@]}" cp -a "${ROOT_DIR}/gost_watchdog" "${RUNTIME}/gost_watchdog"
"${SUDO[@]}" cp "${ROOT_DIR}/tests/watchdog_runtime_probe.py" "${RUNTIME}/watchdog_runtime_probe.py"
"${SUDO[@]}" cp "${ROOT_DIR}/tests/watchdog_soak.py" "${RUNTIME}/watchdog_soak.py"
"${SUDO[@]}" cp "${ROOT_DIR}/packaging/watchdog.conf" "${ROOT}/etc/gost-manager/watchdog.conf"
printf 'KHAREJ_IP=127.0.0.1\n' | "${SUDO[@]}" tee "${ROOT}/etc/gost/iran-1.env" >/dev/null
printf '[Service]\nExecStart=/bin/true\n' | "${SUDO[@]}" tee \
  "${ROOT}/etc/systemd/system/gost-iran-1.service" >/dev/null
printf 'MODE=disabled\n' | "${SUDO[@]}" tee \
  "${ROOT}/etc/gost-manager/watchdog.d/iran-1.conf" >/dev/null
"${SUDO[@]}" chmod 0600 \
  "${ROOT}/etc/gost-manager/watchdog.conf" \
  "${ROOT}/etc/gost-manager/watchdog.d/iran-1.conf" \
  "${ROOT}/etc/gost/iran-1.env"

"${SUDO[@]}" systemd-run \
  --unit="${TEST_ID}" \
  --wait \
  --collect \
  --pipe \
  --property=Type=oneshot \
  --property=User=root \
  --property=Group=root \
  --property="WorkingDirectory=${STATE}" \
  --property=UMask=0077 \
  --property=LimitNOFILE=4096 \
  --property=TasksMax=128 \
  --property=NoNewPrivileges=true \
  --property=PrivateTmp=true \
  --property=PrivateDevices=true \
  --property=ProtectHome=true \
  --property=ProtectSystem=strict \
  --property=ProtectKernelTunables=true \
  --property=ProtectKernelModules=true \
  --property=ProtectKernelLogs=true \
  --property=ProtectControlGroups=true \
  --property=ProtectClock=true \
  --property=RestrictRealtime=true \
  --property=RestrictSUIDSGID=true \
  --property=LockPersonality=true \
  --property=MemoryDenyWriteExecute=true \
  --property="RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" \
  --property="ReadOnlyPaths=${ROOT}/etc/gost ${ROOT}/etc/gost-manager/watchdog.conf ${ROOT}/etc/gost-manager/watchdog.d ${ROOT}/etc/systemd/system ${RUNTIME}" \
  --property="ReadWritePaths=${STATE}" \
  /usr/bin/env \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="${RUNTIME}" \
  /usr/bin/python3 "${RUNTIME}/watchdog_runtime_probe.py" "${ROOT}"

printf 'PASS: hardened Watchdog runtime and ten-profile soak benchmark\n'
