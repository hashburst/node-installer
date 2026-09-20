#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run as root (sudo).' >&2; exit 1; }

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${1:-/home/synapta/hashburst-dr1-seed-v220-hashburst-only.tgz}"
MONERO_ROOT="/opt/monero/0.18.5.1"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ST="$TMP/seed"
mkdir -p "$ST/etc/hashburst/monero" "$ST/systemd" "$ST/opt/monero" "$ST/opt/hashburst"

for f in \
  /etc/hashburst/config.json \
  /etc/hashburst/monero/mainnet.conf \
  /etc/hashburst/monero/testnet.conf \
  "$ROOT_DIR/ha/payload/master_node_v220.py" \
  "$ROOT_DIR/ha/payload/hashburst-master.service" \
  "$ROOT_DIR/ha/payload/hashburst-monero-mainnet.service" \
  "$ROOT_DIR/ha/payload/hashburst-monero-testnet.service"; do
  [[ -f "$f" ]] || { echo "Missing required file: $f" >&2; exit 1; }
done
[[ -x "$MONERO_ROOT/monerod" ]] || { echo "Missing $MONERO_ROOT/monerod" >&2; exit 1; }

python3 - <<'PY'
import json
p='/etc/hashburst/config.json'
d=json.load(open(p))
if not isinstance(d,dict):
    raise SystemExit('invalid master config')
required={'network','tep_host','tep_port','api_port','coins','segmentation'}
missing=sorted(required-set(d))
if missing:
    raise SystemExit('master config missing keys: '+','.join(missing))
forbidden=('85.233.199.35','77.90.188.153')
def walk(v,path='$'):
    if isinstance(v,dict):
        for k,x in v.items(): walk(x,f'{path}.{k}')
    elif isinstance(v,list):
        for i,x in enumerate(v): walk(x,f'{path}[{i}]')
    elif isinstance(v,str):
        for token in forbidden:
            if token in v:
                raise SystemExit(f'candidate physical address found in master config at {path}')
walk(d)
print('MASTER_CONFIG_ADDRESS_PASS')
PY

install -m 0600 /etc/hashburst/config.json "$ST/etc/hashburst/config.master.json"
install -m 0644 /etc/hashburst/monero/mainnet.conf "$ST/etc/hashburst/monero/mainnet.conf"
install -m 0644 /etc/hashburst/monero/testnet.conf "$ST/etc/hashburst/monero/testnet.conf"
install -m 0755 "$ROOT_DIR/ha/payload/master_node_v220.py" "$ST/opt/hashburst/master_node.py"
install -m 0644 "$ROOT_DIR/ha/payload/hashburst-master.service" "$ST/systemd/hashburst-master.service"
install -m 0644 "$ROOT_DIR/ha/payload/hashburst-monero-mainnet.service" "$ST/systemd/hashburst-monero-mainnet.service"
install -m 0644 "$ROOT_DIR/ha/payload/hashburst-monero-testnet.service" "$ST/systemd/hashburst-monero-testnet.service"
cp -a "$MONERO_ROOT" "$ST/opt/monero/0.18.5.1"

cat > "$ST/MANIFEST.txt" <<EOF
created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
source_node=master-node
scope=hashburst-depin-tep-ha-monero
monero_root=$MONERO_ROOT
contains_secret=yes:master-config
contains_chain_data=no
contains_tep_identity=no
contains_libp2p_identity=no
EOF

(
  cd "$ST"
  find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS
)

tar -C "$TMP" -czf "$OUT" seed
chmod 0600 "$OUT"
if id synapta >/dev/null 2>&1; then chown synapta:synapta "$OUT"; fi
sha256sum "$OUT" | tee "$OUT.sha256"
if id synapta >/dev/null 2>&1; then chown synapta:synapta "$OUT.sha256"; fi

echo "DR seed created: $OUT"
echo "The seed contains the master configuration and must be transported only over authenticated SSH/SCP."
echo "No Monero chain database and no TEP/libp2p identity are included."
