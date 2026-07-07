#!/usr/bin/env bash
set -Eeuo pipefail

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "uninstall.sh must be run as root. Try: sudo bash uninstall.sh"
  fi
}

confirm() {
  local prompt="$1"
  local answer
  read -r -p "${prompt} [y/N]: " answer
  case "${answer}" in
    y|Y|yes|YES|Yes) return 0 ;;
    *) return 1 ;;
  esac
}

require_root

confirm "Uninstall GOST Manager and stop managed tunnels?" || die "uninstall aborted."

for service_file in /etc/systemd/system/gost-iran-*.service /etc/systemd/system/gost-kharej-*.service; do
  [[ -e "${service_file}" ]] || continue
  service_name="$(basename "${service_file}")"
  systemctl disable --now "${service_name}" || true
  rm -f "${service_file}"
done

rm -f /usr/local/sbin/gost-manager
rm -rf /usr/local/lib/gost-manager

if confirm "Delete /etc/gost/ including managed env files and backups?"; then
  rm -rf /etc/gost
fi

if confirm "Delete /usr/local/bin/gost binary?"; then
  rm -f /usr/local/bin/gost
fi

systemctl daemon-reload
systemctl reset-failed

printf 'GOST Manager uninstalled.\n'
