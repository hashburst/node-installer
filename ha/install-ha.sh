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
[[ -f /opt/hashburst-tep/tep/hb_tep_runtime.py ]] || {
  echo "Existing HashBurst TEP v2.1.6 runtime not found under /opt/hashburst-tep/tep." >&2
  exit 1
}

install -d -m 0755 /opt/hashburst-ha /opt/hashburst-tep/tep
install -d -m 0755 /etc/hashburst /etc/systemd/system/hashburst-tep.service.d
install -d -m 0700 /var/lib/hashburst/ha /run/hashburst-ha

install -m 0755 "$ROOT_DIR/ha/hashburst_ha_agent.py" /opt/hashburst-ha/hashburst_ha_agent.py
install -m 0644 "$ROOT_DIR/tep/hb_tep_ha_service.py" /opt/hashburst-tep/tep/hb_tep_ha_service.py
install -m 0644 "$ROOT_DIR/tep/hb_tep_runtime_ha.py" /opt/hashburst-tep/tep/hb_tep_runtime_ha.py
install -m 0644 "$ROOT_DIR/ha/hashburst-tep-ha.conf" /etc/systemd/system/hashburst-tep.service.d/ha.conf
install -m 0600 "$CONFIG_SRC" /etc/hashburst/ha.json
install -m 0644 "$ROOT_DIR/ha/hashburst-ha-agent.service" /etc/systemd/system/hashburst-ha-agent.service
install -m 0644 "$ROOT_DIR/ha/hashburst-ha-watchdog.service" /etc/systemd/system/hashburst-ha-watchdog.service

python3 -m py_compile \
  /opt/hashburst-ha/hashburst_ha_agent.py \
  /opt/hashburst-tep/tep/hb_tep_ha_service.py \
  /opt/hashburst-tep/tep/hb_tep_runtime_ha.py

systemctl daemon-reload

echo "Installed TEP-HA files and the hashburst-tep.service HA drop-in."
echo "Configuration: /etc/hashburst/ha.json"
echo "No running service was restarted by this installer."
echo "hashburst-tep.service must be restarted in a controlled step before the HA agent can be enabled."

if [[ "$ENABLE" -eq 1 ]]; then
  if ! python3 -c 'import json,urllib.request; d=json.load(urllib.request.urlopen("http://127.0.0.1:47778/",timeout=2)); assert d.get("app_ready") is True and "ha.lease" in set(d.get("services") or [])'; then
    echo "TEP HA runtime is not active. Restart hashburst-tep.service first, validate it, then rerun with --enable." >&2
    exit 1
  fi
  systemctl enable --now hashburst-ha-watchdog.service hashburst-ha-agent.service
  systemctl --no-pager --full status hashburst-ha-agent.service hashburst-ha-watchdog.service || true
fi
