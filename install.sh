#!/usr/bin/env bash
set -Eeuo pipefail

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "install.sh must be run as root. Try: sudo bash install.sh"
  fi
}

require_root

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install -d -m 755 /usr/local/lib/gost-manager
install -d -m 700 /etc/gost

install -m 755 "${SCRIPT_DIR}/gost-manager.sh" /usr/local/sbin/gost-manager
install -m 755 "${SCRIPT_DIR}/lib/gost-run-iran.sh" /usr/local/lib/gost-manager/gost-run-iran.sh
install -m 755 "${SCRIPT_DIR}/lib/gost-run-kharej.sh" /usr/local/lib/gost-manager/gost-run-kharej.sh

chmod 755 /usr/local/sbin/gost-manager
chmod 755 /usr/local/lib/gost-manager/gost-run-iran.sh
chmod 755 /usr/local/lib/gost-manager/gost-run-kharej.sh
chmod 700 /etc/gost

cat <<'EOF'
GOST Manager installed.
Run:
  sudo gost-manager
EOF
