#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-/etc/hashburst/ha.json}"
ACTIVE_CONFIG="/etc/hashburst/ha.json"
GUARD_FILE="/run/hashburst-ha/lease.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/hashburst-primary-lease.conf" ]]; then
  GUARD_DROPIN="$SCRIPT_DIR/hashburst-primary-lease.conf"
else
  ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
  GUARD_DROPIN="$ROOT_DIR/ha/hashburst-primary-lease.conf"
fi

[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
[[ -f "$CONFIG" ]] || { echo "Missing HA config: $CONFIG" >&2; exit 1; }
[[ -f "$GUARD_DROPIN" ]] || { echo "Missing primary lease guard: $GUARD_DROPIN" >&2; exit 1; }

python3 - "$CONFIG" <<'PY'
import json,sys
p=sys.argv[1]
d=json.load(open(p))
if d.get("armed") is not True:
    raise SystemExit("Refusing to arm: configuration must contain armed=true")
roles=set(d.get("roles") or [])
if "candidate" not in roles:
    raise SystemExit("Refusing to arm: local node is not an HA candidate")
services=[str(x).strip() for x in d.get("primary_services",[]) if str(x).strip()]
if not services:
    raise SystemExit("Refusing to arm: primary_services is empty")
print("\n".join(services))
PY

mapfile -t PRIMARY_SERVICES < <(python3 - "$CONFIG" <<'PY'
import json,sys
for x in json.load(open(sys.argv[1])).get("primary_services",[]):
    x=str(x).strip()
    if x: print(x)
PY
)

python3 - "$CONFIG" <<'PY'
import json,sys,urllib.request
cfg=json.load(open(sys.argv[1]))
tep=json.load(urllib.request.urlopen("http://127.0.0.1:47778/",timeout=2))
services=set(tep.get("services") or [])
assert tep.get("app_ready") is True, "TEP APP is not ready"
assert str(tep.get("node_id") or "") == str(cfg.get("node_id") or ""), "HA/TEP node_id mismatch"
assert "ha.lease" in services, "ha.lease is not advertised"
PY

python3 /opt/hashburst-ha/hashburst_ha_readiness.py \
  --config "$CONFIG" \
  --output /var/lib/hashburst/ha/replication.json \
  --once >/tmp/hashburst-ha-readiness.json
python3 - <<'PY'
import json
s=json.load(open('/tmp/hashburst-ha-readiness.json'))
if s.get('ready') is not True:
    raise SystemExit('Refusing to arm: DR readiness is false: '+','.join(s.get('errors') or []))
PY
rm -f /tmp/hashburst-ha-readiness.json

for svc in "${PRIMARY_SERVICES[@]}"; do
  systemctl cat "$svc" >/dev/null 2>&1 || {
    echo "Refusing to arm: primary service unit not found: $svc" >&2
    exit 1
  }
  install -d -m 0755 "/etc/systemd/system/${svc}.d"
  install -m 0644 "$GUARD_DROPIN" \
    "/etc/systemd/system/${svc}.d/20-hashburst-ha-lease.conf"
  systemctl disable "$svc" >/dev/null 2>&1 || true
done

systemctl stop hashburst-ha-agent.service hashburst-ha-watchdog.service 2>/dev/null || true

install -d -m 0755 /etc/hashburst /run/hashburst-ha
if [[ "$(readlink -f "$CONFIG")" != "$(readlink -m "$ACTIVE_CONFIG")" ]]; then
  install -m 0600 "$CONFIG" "$ACTIVE_CONFIG"
else
  chmod 0600 "$ACTIVE_CONFIG"
fi

rm -f "$GUARD_FILE"
for ((i=${#PRIMARY_SERVICES[@]}-1; i>=0; i--)); do
  systemctl stop "${PRIMARY_SERVICES[$i]}" 2>/dev/null || true
done

systemctl daemon-reload
systemctl enable hashburst-ha-readiness.service hashburst-ha-watchdog.service hashburst-ha-agent.service >/dev/null
systemctl restart hashburst-ha-readiness.service
systemctl restart hashburst-ha-watchdog.service
systemctl restart hashburst-ha-agent.service

sleep 2
curl -fsS http://127.0.0.1:47780/v1/status | python3 -m json.tool

echo "HA candidate armed. Primary-only services are disabled for autonomous boot."
echo "A fresh lease guard is required before a primary-only service can start."
