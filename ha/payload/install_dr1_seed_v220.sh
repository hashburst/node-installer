#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run as root.' >&2; exit 1; }
[[ $# -eq 1 ]] || { echo "Usage: $0 SEED.tgz" >&2; exit 2; }

SEED="$1"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CFGHELPER="$ROOT_DIR/ha/payload/prepare_dr1_config.py"
EXPECTED_NODE_ID="hashburst-dr1"
EXPECTED_PEER_ID="12D3KooWCcBw87pFYFyHwxK6YCQGCpjLcbxPJLxWxx7qdKah1bnN"
EXPECTED_TEP_PUBKEY="b75568b136da2bead662ef80573fa938169198c9aa7c047236faed2eaa193348"

for cmd in python3 rsync systemctl ss getent id useradd groupadd sha256sum nproc df awk; do
  command -v "$cmd" >/dev/null || { echo "Missing command: $cmd" >&2; exit 1; }
done
[[ -f "$SEED" ]] || { echo "Missing $SEED" >&2; exit 1; }
[[ -f "$CFGHELPER" ]] || { echo "Missing $CFGHELPER" >&2; exit 1; }

NODE_ID="$(grep -h '^NODE_ID=' /etc/hashburst/env 2>/dev/null | tail -1 | cut -d= -f2-)"
PEER_ID="$(grep -h '^HB_TEP_PEER_ID=' /etc/hashburst/hashburst-tep.env 2>/dev/null | tail -1 | cut -d= -f2-)"
TEP_PUBKEY="$(grep -h '^TEP_PUBKEY=' /etc/hashburst/hashburst-tep.env /etc/hashburst/env 2>/dev/null | tail -1 | cut -d= -f2-)"
[[ "$NODE_ID" == "$EXPECTED_NODE_ID" ]] || { echo "Unexpected node identity: $NODE_ID" >&2; exit 1; }
[[ "$PEER_ID" == "$EXPECTED_PEER_ID" ]] || { echo "Unexpected libp2p identity: $PEER_ID" >&2; exit 1; }
[[ "$TEP_PUBKEY" == "$EXPECTED_TEP_PUBKEY" ]] || { echo "Unexpected TEP identity: $TEP_PUBKEY" >&2; exit 1; }

echo '===== DR1 RESOURCES ====='
echo "CPUs: $(nproc)"
awk '/MemTotal/ {printf "MemTotal: %.1f GiB\n", $2/1024/1024}' /proc/meminfo
mkdir -p /var/lib/hashburst
FREE_BYTES="$(df -PB1 /var/lib/hashburst | awk 'NR==2 {print $4}')"
awk -v b="$FREE_BYTES" 'BEGIN {printf "Free storage: %.1f GiB\n", b/1024/1024/1024}'
MIN_FREE=$((120 * 1024 * 1024 * 1024))
[[ "$FREE_BYTES" -ge "$MIN_FREE" ]] || { echo 'At least 120 GiB free is required for the two pruned Monero DR datasets.' >&2; exit 1; }
MEM_KIB="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
if [[ "$MEM_KIB" -lt $((32 * 1024 * 1024)) ]]; then
  echo 'WARNING: less than 32 GiB RAM detected; conservative Monero concurrency will be used.' >&2
fi

if systemctl is-active --quiet neurallity-seg-worker.service 2>/dev/null; then
  echo 'Existing segmentation worker: active (left untouched).'
fi
if ss -lntp | grep -q '10\.0\.0\.2:9000'; then
  echo 'Existing 10.0.0.2:9000 listener preserved.'
fi
if ss -lntp | grep -q '127\.0\.0\.1:9000'; then
  echo 'Unexpected listener already owns 127.0.0.1:9000' >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
tar -C "$TMP" -xzf "$SEED"
ST="$TMP/seed"
[[ -d "$ST" ]] || { echo 'Invalid seed archive' >&2; exit 1; }
(
  cd "$ST"
  sha256sum -c SHA256SUMS
)

install -d -m 0755 /opt/hashburst /opt/monero /etc/hashburst /etc/hashburst/monero /var/log/hashburst
rsync -a --delete "$ST/opt/monero/0.18.5.1/" /opt/monero/0.18.5.1/
install -o root -g root -m 0755 "$ST/opt/hashburst/master_node.py" /opt/hashburst/master_node.py
python3 -m py_compile /opt/hashburst/master_node.py
python3 "$CFGHELPER" "$ST/etc/hashburst/config.master.json" "$TMP/config.dr1.json"
install -o root -g root -m 0600 "$TMP/config.dr1.json" /etc/hashburst/config.json

for account in monero-mainnet monero-testnet; do
  getent group "$account" >/dev/null 2>&1 || groupadd --system "$account"
  id "$account" >/dev/null 2>&1 || useradd --system --gid "$account" --home-dir "/var/lib/hashburst/monero/$account" --shell /usr/sbin/nologin "$account"
done

python3 - "$ST/etc/hashburst/monero/mainnet.conf" "/etc/hashburst/monero/mainnet.conf" mainnet "$(nproc)" <<'PY'
import os,sys
from pathlib import Path
src,dst,network,cpu_s=sys.argv[1:]
cpu=max(1,int(cpu_s))
lines=Path(src).read_text().splitlines()
values={}
order=[]
for line in lines:
    s=line.strip()
    if not s or s.startswith('#') or '=' not in s:
        continue
    k,v=s.split('=',1)
    k=k.strip(); v=v.strip()
    if k not in values: order.append(k)
    values[k]=v
base='/var/lib/hashburst/monero/mainnet' if network=='mainnet' else '/var/lib/hashburst/monero/testnet'
values['data-dir']=base+'/data'
values['log-file']='/var/log/hashburst/monero-'+network+'.log'
values['rpc-bind-ip']='127.0.0.1'
values['max-concurrency']=str(min(8,max(2,cpu//2)))
values['prep-blocks-threads']=str(min(2,max(1,cpu//4)))
values['out-peers']='8'
for k in values:
    if k not in order: order.append(k)
out='\n'.join(f'{k}={values[k]}' for k in order)+'\n'
tmp=Path(dst+'.tmp'); tmp.write_text(out); os.chmod(tmp,0o644); os.replace(tmp,dst)
PY

python3 - "$ST/etc/hashburst/monero/testnet.conf" "/etc/hashburst/monero/testnet.conf" testnet "$(nproc)" <<'PY'
import os,sys
from pathlib import Path
src,dst,network,cpu_s=sys.argv[1:]
cpu=max(1,int(cpu_s))
lines=Path(src).read_text().splitlines()
values={}
order=[]
for line in lines:
    s=line.strip()
    if not s or s.startswith('#') or '=' not in s:
        continue
    k,v=s.split('=',1)
    k=k.strip(); v=v.strip()
    if k not in values: order.append(k)
    values[k]=v
base='/var/lib/hashburst/monero/testnet'
values['data-dir']=base+'/data'
values['log-file']='/var/log/hashburst/monero-testnet.log'
values['rpc-bind-ip']='127.0.0.1'
values['max-concurrency']=str(min(4,max(1,cpu//4)))
values['prep-blocks-threads']='1'
values['out-peers']='4'
for k in values:
    if k not in order: order.append(k)
out='\n'.join(f'{k}={values[k]}' for k in order)+'\n'
tmp=Path(dst+'.tmp'); tmp.write_text(out); os.chmod(tmp,0o644); os.replace(tmp,dst)
PY

install -d -o monero-mainnet -g monero-mainnet -m 0750 /var/lib/hashburst/monero/mainnet /var/lib/hashburst/monero/mainnet/data
install -d -o monero-testnet -g monero-testnet -m 0750 /var/lib/hashburst/monero/testnet /var/lib/hashburst/monero/testnet/data
install -o monero-mainnet -g monero-mainnet -m 0640 /dev/null /var/log/hashburst/monero-mainnet.log
install -o monero-testnet -g monero-testnet -m 0640 /dev/null /var/log/hashburst/monero-testnet.log

install -m 0644 "$ST/systemd/hashburst-master.service" /etc/systemd/system/hashburst-master.service
install -m 0644 "$ST/systemd/hashburst-monero-mainnet.service" /etc/systemd/system/hashburst-monero-mainnet.service
install -m 0644 "$ST/systemd/hashburst-monero-testnet.service" /etc/systemd/system/hashburst-monero-testnet.service
systemctl daemon-reload

systemctl disable hashburst-master.service >/dev/null 2>&1 || true
systemctl stop hashburst-master.service >/dev/null 2>&1 || true
systemctl enable --now hashburst-monero-mainnet.service hashburst-monero-testnet.service

/opt/monero/0.18.5.1/monerod --version | head -n1
sleep 3
systemctl is-active --quiet hashburst-monero-mainnet.service
systemctl is-active --quiet hashburst-monero-testnet.service

if ss -lntp | grep -q '127\.0\.0\.1:9000'; then
  echo 'Unexpected listener appeared on 127.0.0.1:9000' >&2
  exit 1
fi
ss -lntup | grep -E ':(18080|18081|28080|28081|9000)\b' || true

sha256sum /opt/hashburst/master_node.py /etc/hashburst/config.json /etc/hashburst/monero/mainnet.conf /etc/hashburst/monero/testnet.conf

echo 'DR1_SEED_INSTALL_PASS'
echo 'The HashBurst master remains stopped. Monero synchronization now proceeds locally on DR1.'
echo 'Do not arm HA until both Monero readiness checks and the full 3/3 voter gate pass.'
