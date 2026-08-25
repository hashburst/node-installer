#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 --config /path/to/ha.json [--enable]" >&2
}

CONFIG_SRC=""
ENABLE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      CONFIG_SRC="$2"
      shift 2
      ;;
    --enable)
      ENABLE=1
      shift
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

[[ -n "$CONFIG_SRC" && -f "$CONFIG_SRC" ]] || { usage; exit 2; }
[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
install -d -m 0755 /opt/hashburst-ha
install -d -m 0755 /etc/hashburst
install -d -m 0700 /var/lib/hashburst/ha /run/hashburst-ha
install -m 0755 "$ROOT_DIR/ha/hashburst_ha_agent.py" /opt/hashburst-ha/hashburst_ha_agent.py
install -m 0600 "$CONFIG_SRC" /etc/hashburst/ha.json
install -m 0644 "$ROOT_DIR/ha/hashburst-ha-agent.service" /etc/systemd/system/hashburst-ha-agent.service
install -m 0644 "$ROOT_DIR/ha/hashburst-ha-watchdog.service" /etc/systemd/system/hashburst-ha-watchdog.service
systemctl daemon-reload

python3 -m py_compile /opt/hashburst-ha/hashburst_ha_agent.py

echo "Installed TEP-HA files."
echo "Configuration: /etc/hashburst/ha.json"
echo "Services are not started unless --enable was supplied."

if [[ "$ENABLE" -eq 1 ]]; then
  systemctl enable --now hashburst-ha-watchdog.service hashburst-ha-agent.service
  systemctl --no-pager --full status hashburst-ha-agent.service hashburst-ha-watchdog.service || true
fi
