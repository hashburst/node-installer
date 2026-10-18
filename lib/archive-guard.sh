#!/usr/bin/env bash
# Refuse generic installation over a terminal archive before writing any files.
hashburst_require_writable_install_target() {
  local details
  if ! details="$(systemctl show hashburst-node.service -p LoadState -p ExecStart -p Description 2>/dev/null)"; then
    echo "ERROR: cannot inspect existing node service; no installation performed." >&2
    return 1
  fi
  if [[ -z "$details" ]]; then
    echo "ERROR: empty service inspection; no installation performed." >&2
    return 1
  fi
  if printf '%s\n' "$details" | grep -Eiq 'hvm-legacy-archive|immutable terminal archive|legacy-terminal-archive'; then
    echo "ERROR: legacy terminal archive detected. Generic installer and HA replacement are prohibited on this host. Use a reviewed archive-preserving update." >&2
    return 1
  fi
}
