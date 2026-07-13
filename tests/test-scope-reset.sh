#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=integration-test-lib.sh
source "${ROOT_DIR}/tests/integration-test-lib.sh"

sha256_file() {
  local path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "${path}" | awk '{print $1}'
  else
    shasum -a 256 "${path}" | awk '{print $1}'
  fi
}

assert_absent "Gateway Python package removed" "${ROOT_DIR}/gateway"
assert_absent "Gateway state launcher removed" "${ROOT_DIR}/packaging/gost-gateway"
assert_absent "Gateway runtime launcher removed" "${ROOT_DIR}/packaging/gost-gateway-runtime"
assert_absent "Gateway runner removed" "${ROOT_DIR}/lib/gost-run-gateway-exit.sh"
assert_absent "Gateway runtime manifest removed" "${ROOT_DIR}/packaging/gateway-runtime-manifest.txt"
assert_absent "Gateway state documentation removed" "${ROOT_DIR}/docs/GATEWAY-STATE-V0.2.md"
assert_absent "Gateway runtime documentation removed" "${ROOT_DIR}/docs/GATEWAY-RUNTIME-V0.2.md"

production_gateway_imports="$(
  grep -R -E '(^|[[:space:]])(from|import)[[:space:]]+gateway([[:space:].]|$)' \
    "${ROOT_DIR}/monitoring" "${ROOT_DIR}/packaging" 2>/dev/null || true
)"
assert_eq "production code imports no Gateway package" "" "${production_gateway_imports}"

vendored_gost_source="$(
  find "${ROOT_DIR}" -path "${ROOT_DIR}/.git" -prune -o \
    \( -name '*.go' -o -name 'go.mod' -o -name 'go.sum' -o -name '*.patch' -o -name '*.diff' \) \
    -print -quit
)"
assert_eq "no vendored GOST source or patch tree" "" "${vendored_gost_source}"
assert_contains "official upstream releases API retained" \
  "https://api.github.com/repos/go-gost/gost/releases?per_page=100" \
  "${ROOT_DIR}/gost-manager.sh"
assert_contains "official upstream release URL allow-list retained" \
  "https://github.com/go-gost/gost/releases/download/" \
  "${ROOT_DIR}/gost-manager.sh"

assert_eq "Iran Direct runner byte checksum unchanged" \
  "618b167f3057b67a6f89bb46b1971e5acd6865f57d991566482981965bb53549" \
  "$(sha256_file "${ROOT_DIR}/lib/gost-run-iran.sh")"
assert_eq "Kharej Direct runner byte checksum unchanged" \
  "e3e6a358c812613f3473be6937f1d384a80925cb9e072f7a8794e2f8586294cd" \
  "$(sha256_file "${ROOT_DIR}/lib/gost-run-kharej.sh")"
assert_eq "Iran example env byte checksum unchanged" \
  "fc817cb943133c5949ffa432a6acc58281f2106e4072c481a98e7635d62a6d3e" \
  "$(sha256_file "${ROOT_DIR}/examples/iran-1.env.example")"
assert_eq "Kharej example env byte checksum unchanged" \
  "47fb03fdcdead68e7822080f54c546baba0749e3af8d0a2e0590a184996e7c43" \
  "$(sha256_file "${ROOT_DIR}/examples/kharej-1.env.example")"

finish_suite
