#!/usr/bin/env bash
set -euo pipefail

printf '%s\n' '===== HASHBURST TEP HOST PREFLIGHT ====='
echo "hostname=$(hostname)"
echo "utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ip -br addr || true

echo
echo '===== INSTALLED UNITS ====='
systemctl list-unit-files 2>/dev/null | grep -E 'hashburst-(node|tep|ha)' || true

echo
echo '===== ACTIVE STATE ====='
for svc in hashburst-node.service hashburst-tep.service hashburst-ha-agent.service hashburst-ha-watchdog.service; do
  printf '%-36s ' "$svc"
  systemctl is-active "$svc" 2>/dev/null || true
done

echo
echo '===== EXISTING IDENTITY FILES ====='
for path in \
  /var/lib/hashburst/node_p2p.key \
  /var/lib/hashburst/tep/node_x25519.key \
  /etc/hashburst/hashburst-tep.env \
  /etc/hashburst/env \
  /etc/hashburst/install-state.json
do
  if [[ -e "$path" ]]; then
    stat -c '%A %U:%G %s %n' "$path"
  else
    echo "MISSING $path"
  fi
done

echo
echo '===== SAFE IDENTITY VALUES ====='
grep -hE '^(NODE_ID|HB_TEP_NODE_ID|HB_TEP_PEER_ID|TEP_PUBKEY)=' \
  /etc/hashburst/env /etc/hashburst/hashburst-tep.env 2>/dev/null || true

echo
echo '===== LISTENERS ====='
ss -lntup 2>/dev/null | grep -E ':(30307|8009|47777|47778|47780|47781|47782)\b' || true

echo
echo '===== LOCAL TEP STATUS ====='
if curl -fsS --max-time 2 http://127.0.0.1:47778/ >/tmp/hashburst-tep-preflight.json 2>/dev/null; then
  python3 -m json.tool /tmp/hashburst-tep-preflight.json
  rm -f /tmp/hashburst-tep-preflight.json
else
  echo 'NO_LOCAL_TEP_STATUS'
fi

echo
echo '===== LOCAL BLOCKCHAIN HEALTH ====='
if curl -fsS --max-time 2 http://127.0.0.1:8009/api/health >/tmp/hashburst-node-preflight.json 2>/dev/null; then
  python3 -m json.tool /tmp/hashburst-node-preflight.json
  rm -f /tmp/hashburst-node-preflight.json
else
  echo 'NO_LOCAL_BLOCKCHAIN_HEALTH'
fi

echo
echo '===== DECISION ====='
if [[ -e /var/lib/hashburst/tep/node_x25519.key || -e /etc/hashburst/hashburst-tep.env ]] || systemctl cat hashburst-tep.service >/dev/null 2>&1; then
  echo 'ADOPT_EXISTING_TEP_IDENTITY=1'
  echo 'Do not generate, replace, delete or clone TEP identity files.'
else
  echo 'ADOPT_EXISTING_TEP_IDENTITY=0'
  echo 'Host has no detected TEP identity; fresh onboarding may proceed.'
fi
