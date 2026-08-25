#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-/etc/hashburst/ha.json}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
[[ -f "$CONFIG" ]] || { echo "Missing HA config: $CONFIG" >&2; exit 1; }

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

python3 - <<'PY'
import json,urllib.request
tep=json.load(urllib.request.urlopen("http://127.0.0.1:47778/",timeout=2))
services=set(tep.get("services") or [])
assert tep.get("app_ready") is True, "TEP APP is not ready"
assert "ha.lease" in services, "ha.lease is not advertised"
assert "k325t.exchange" in services, "k325t.exchange is not advertised"
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

install -d -m 0755 /etc/systemd/system
for svc in "${PRIMARY_SERVICES[@]}"; do
  systemctl cat "$svc" >/dev/null 2>&1 || {
    echo "Refusing to arm: primary service unit not found: $svc" >&2
    exit 1
  }
  install -d -m 0755 "/etc/systemd/system/${svc}.d"
  install -m 0644 "$ROOT_DIR/ha/hashburst-primary-lease.conf" \
    "/etc/systemd/system/${svc}.d/20-hashburst-ha-lease.conf"
  systemctl disable "$svc" >/dev/null 2>&1 || true
done

systemctl daemon-reload
systemctl enable --now hashburst-ha-readiness.service
systemctl enable --now hashburst-ha-watchdog.service hashburst-ha-agent.service

sleep 2
curl -fsS http://127.0.0.1:47780/v1/status | python3 -m json.tool

echo "HA candidate armed. Primary-only services are disabled for autonomous boot."
echo "They may start only while /run/hashburst-ha/lease.json exists and HA owns the lease."
