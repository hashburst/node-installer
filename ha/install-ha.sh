#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 --config /path/to/ha.json [--enable-observation]" >&2
}

CONFIG_SRC=""
ENABLE_OBSERVATION=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      CONFIG_SRC="$2"
      shift 2
      ;;
    --enable-observation)
      ENABLE_OBSERVATION=1
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
  echo "Existing HashBurst TEP v2.1.6-compatible runtime not found under /opt/hashburst-tep/tep." >&2
  exit 1
}

install -d -m 0755 /opt/hashburst-ha /opt/hashburst-tep/tep
install -d -m 0755 /etc/hashburst /etc/systemd/system/hashburst-tep.service.d
install -d -m 0700 /var/lib/hashburst/ha /run/hashburst-ha

install -m 0755 "$ROOT_DIR/ha/hashburst_ha_agent.py" /opt/hashburst-ha/hashburst_ha_agent.py
install -m 0755 "$ROOT_DIR/ha/hashburst_ha_agent_v220.py" /opt/hashburst-ha/hashburst_ha_agent_v220.py
install -m 0755 "$ROOT_DIR/ha/hashburst_ha_readiness.py" /opt/hashburst-ha/hashburst_ha_readiness.py
install -m 0755 "$ROOT_DIR/ha/hashburst_ha_ingress.py" /opt/hashburst-ha/hashburst_ha_ingress.py
install -m 0644 "$ROOT_DIR/tep/hb_tep_ha_service.py" /opt/hashburst-tep/tep/hb_tep_ha_service.py
install -m 0644 "$ROOT_DIR/tep/hb_tep_runtime_ha.py" /opt/hashburst-tep/tep/hb_tep_runtime_ha.py
install -m 0644 "$ROOT_DIR/tep/hb_tep_k325t_service.py" /opt/hashburst-tep/tep/hb_tep_k325t_service.py
install -m 0644 "$ROOT_DIR/tep/hb_tep_runtime_v220.py" /opt/hashburst-tep/tep/hb_tep_runtime_v220.py
install -m 0644 "$ROOT_DIR/ha/hashburst-tep-ha.conf" /etc/systemd/system/hashburst-tep.service.d/ha.conf
install -m 0600 "$CONFIG_SRC" /etc/hashburst/ha.json
install -m 0644 "$ROOT_DIR/ha/hashburst-ha-agent.service" /etc/systemd/system/hashburst-ha-agent.service
install -m 0644 "$ROOT_DIR/ha/hashburst-ha-watchdog.service" /etc/systemd/system/hashburst-ha-watchdog.service
install -m 0644 "$ROOT_DIR/ha/hashburst-ha-readiness.service" /etc/systemd/system/hashburst-ha-readiness.service
install -m 0644 "$ROOT_DIR/ha/hashburst-ha-ingress.service" /etc/systemd/system/hashburst-ha-ingress.service
install -m 0755 "$ROOT_DIR/ha/arm-ha.sh" /opt/hashburst-ha/arm-ha.sh
install -m 0644 "$ROOT_DIR/ha/hashburst-primary-lease.conf" /opt/hashburst-ha/hashburst-primary-lease.conf

python3 -m py_compile \
  /opt/hashburst-ha/hashburst_ha_agent.py \
  /opt/hashburst-ha/hashburst_ha_agent_v220.py \
  /opt/hashburst-ha/hashburst_ha_readiness.py \
  /opt/hashburst-ha/hashburst_ha_ingress.py \
  /opt/hashburst-tep/tep/hb_tep_ha_service.py \
  /opt/hashburst-tep/tep/hb_tep_runtime_ha.py \
  /opt/hashburst-tep/tep/hb_tep_k325t_service.py \
  /opt/hashburst-tep/tep/hb_tep_runtime_v220.py

systemctl daemon-reload

echo "Installed HashBurst v2.2 HA candidate files."
echo "Configuration: /etc/hashburst/ha.json"
echo "No running TEP or primary-only service was restarted or disabled."
echo "Restart hashburst-tep.service in a controlled step, then validate services ha.lease and k325t.exchange."

if [[ "$ENABLE_OBSERVATION" -eq 1 ]]; then
  python3 - "$CONFIG_SRC" <<'PY'
import json,sys
cfg=json.load(open(sys.argv[1]))
if cfg.get('armed') is True:
    raise SystemExit('Observation enable refuses armed=true; use armed=false first')
PY
  if ! python3 -c 'import json,urllib.request; d=json.load(urllib.request.urlopen("http://127.0.0.1:47778/",timeout=2)); s=set(d.get("services") or []); assert d.get("app_ready") is True and {"ha.lease","k325t.exchange"}.issubset(s)'; then
    echo "TEP v2.2 runtime is not active. Restart hashburst-tep.service first and validate it." >&2
    exit 1
  fi
  systemctl enable --now hashburst-ha-readiness.service hashburst-ha-watchdog.service hashburst-ha-agent.service
  systemctl --no-pager --full status hashburst-ha-readiness.service hashburst-ha-agent.service hashburst-ha-watchdog.service || true
fi
