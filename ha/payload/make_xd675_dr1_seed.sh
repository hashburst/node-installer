#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run as root (sudo).' >&2; exit 1; }

OUT="${1:-/home/synapta/hashburst-dr1-seed-v220.tgz}"
MONERO_ROOT="/opt/monero/0.18.5.1"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ST="$TMP/seed"
mkdir -p "$ST/etc/hashburst" "$ST/etc/monero-k325t" "$ST/systemd" "$ST/opt/monero"

for f in \
  /etc/hashburst/config.json \
  /etc/hashburst/k325t-pcb-token \
  /etc/monero-k325t/mainnet.conf \
  /etc/monero-k325t/testnet.conf \
  /etc/monero-k325t/testnet-mining-address; do
  [[ -f "$f" ]] || { echo "Missing required file: $f" >&2; exit 1; }
done
[[ -x "$MONERO_ROOT/monerod" ]] || { echo "Missing $MONERO_ROOT/monerod" >&2; exit 1; }

python3 - <<'PY'
import json
p='/etc/hashburst/config.json'
d=json.load(open(p))
if not isinstance(d,dict): raise SystemExit('invalid master config')
forbidden=('85.233.199.35','77.90.188.153')
def walk(v,path='$'):
    if isinstance(v,dict):
        for k,x in v.items(): walk(x,f'{path}.{k}')
    elif isinstance(v,list):
        for i,x in enumerate(v): walk(x,f'{path}[{i}]')
    elif isinstance(v,str):
        for token in forbidden:
            if token in v: raise SystemExit(f'candidate physical address found in master config at {path}')
walk(d)
print('MASTER_CONFIG_ADDRESS_PASS')
PY

install -m 0600 /etc/hashburst/config.json "$ST/etc/hashburst/config.master.json"
install -m 0600 /etc/hashburst/k325t-pcb-token "$ST/etc/hashburst/k325t-pcb-token"
cp -a /etc/monero-k325t/. "$ST/etc/monero-k325t/"
cp -a "$MONERO_ROOT" "$ST/opt/monero/0.18.5.1"

for svc in hashburst-master.service monero-k325t-mainnet.service monero-k325t-testnet.service; do
  p="$(systemctl show -p FragmentPath --value "$svc")"
  [[ -n "$p" && -f "$p" ]] || { echo "Cannot locate unit: $svc" >&2; exit 1; }
  install -m 0644 "$p" "$ST/systemd/$svc"
done

cat > "$ST/MANIFEST.txt" <<EOF
created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
source_node=master-node
monero_root=$MONERO_ROOT
contains_secret=yes:k325t-pcb-token
contains_blockchain_data=no
EOF

tar -C "$TMP" -czf "$OUT" seed
chmod 0600 "$OUT"
if id synapta >/dev/null 2>&1; then chown synapta:synapta "$OUT"; fi
sha256sum "$OUT" | tee "$OUT.sha256"
if id synapta >/dev/null 2>&1; then chown synapta:synapta "$OUT.sha256"; fi

echo "DR seed created: $OUT"
echo "Contains the shared PCB token: transport it only over authenticated SSH/SCP and delete it after installation."
