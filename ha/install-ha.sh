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
install -d -m 0755 /etc/hashburst /etc/systemd/system/hashburst-tep.service.d /etc/systemd/system/hashburst-node.service.d
install -d -m 0700 /var/lib/hashburst/ha /run/hashburst-ha

install -m 0755 "$ROOT_DIR/ha/hashburst_ha_agent.py" /opt/hashburst-ha/hashburst_ha_agent.py
install -m 0755 "$ROOT_DIR/ha/hashburst_ha_agent_v220.py" /opt/hashburst-ha/hashburst_ha_agent_v220.py
install -m 0755 "$ROOT_DIR/ha/hashburst_ha_readiness.py" /opt/hashburst-ha/hashburst_ha_readiness.py
install -m 0755 "$ROOT_DIR/ha/hashburst_ha_ingress.py" /opt/hashburst-ha/hashburst_ha_ingress.py
install -m 0755 "$ROOT_DIR/ha/hashburst-rpc-guard.sh" /opt/hashburst-ha/hashburst-rpc-guard.sh
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
install -m 0644 "$ROOT_DIR/ha/hashburst-rpc-guard.service" /etc/systemd/system/hashburst-rpc-guard.service
install -m 0644 "$ROOT_DIR/ha/hashburst-node-rpc-guard.conf" /etc/systemd/system/hashburst-node.service.d/rpc-guard.conf
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
systemctl enable --now hashburst-rpc-guard.service

echo "Installed HashBurst v2.2 HA candidate files."
echo "Configuration: /etc/hashburst/ha.json"
echo "Blockchain RPC ingress guard: TCP/8009 loopback-only."
echo "No running TEP or primary-only service was restarted or disabled."
echo "Restart hashburst-tep.service in a controlled step, then validate role-appropriate TEP services."

if [[ "$ENABLE_OBSERVATION" -eq 1 ]]; then
  python3 - "$CONFIG_SRC" <<'PY'
import json,sys
cfg=json.load(open(sys.argv[1]))
if cfg.get('armed') is True:
    raise SystemExit('Observation enable refuses armed=true; use armed=false first')
PY
  if ! python3 - /etc/hashburst/ha.json <<'PY'
import json,sys,urllib.request
cfg=json.load(open(sys.argv[1]))
d=json.load(urllib.request.urlopen("http://127.0.0.1:47778/",timeout=2))
if d.get("app_ready") is not True:
    raise SystemExit(1)
services=set(d.get("services") or [])
required={"ha.lease"}
node_id=str(cfg.get("node_id") or "")
candidates={str(item.get("node_id") or "") for item in (cfg.get("candidates") or []) if isinstance(item,dict)}
is_candidate=node_id in candidates
if is_candidate:
    required.add("k325t.exchange")
if not required.issubset(services):
    raise SystemExit(1)
if not is_candidate and "k325t.exchange" in services:
    raise SystemExit(1)
PY
  then
    echo "TEP v2.2 runtime is not active with the services expected for this HA role. Restart hashburst-tep.service first and validate it." >&2
    exit 1
  fi
  systemctl enable --now hashburst-ha-readiness.service hashburst-ha-watchdog.service hashburst-ha-agent.service
  systemctl --no-pager --full status hashburst-ha-readiness.service hashburst-ha-agent.service hashburst-ha-watchdog.service || true
fi
