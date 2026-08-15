#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT="${1:-$(pwd)}"
FAKE="$(mktemp -d /tmp/hb-v215-fake.XXXXXX)"
STORAGE="/tmp/hb-v215-sandbox-storage"
UFW_LOG="/tmp/hb-v215-ufw.log"
SYS_LOG="/tmp/hb-v215-systemctl.log"
NODE_STATE="/tmp/hb-v215-node-active"
PEER_ID="12D3KooWSandboxV215StablePeerIdentity"
PUBKEY="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
RENDEZVOUS="12D3KooWCiH3B8E84UNsop5epp7vNXfC6oSg2iyB4wjyCm6a84ow"

cleanup() {
  set +e
  rm -rf "$FAKE" "$STORAGE"
  rm -f "$UFW_LOG" "$SYS_LOG" "$NODE_STATE" /tmp/hb-tep-onboard-status.json
  rm -f /tmp/swarm.key /tmp/list.json
  rm -rf /etc/hashburst /var/lib/hashburst
  rm -rf /opt/hashburst-tep /opt/hashburst-files /opt/hashburst/replication
  rm -f /usr/local/bin/hashburst-node
  rm -f /etc/systemd/system/hashburst-*.service /etc/systemd/system/ipfs-private.service /etc/systemd/system/ipfs-public.service
}
trap cleanup EXIT

[ "$(id -u)" = 0 ] || { echo "sandbox must run as root" >&2; exit 1; }
[ ! -e /etc/hashburst ] || { echo "refusing to run sandbox on a host with /etc/hashburst" >&2; exit 1; }

cat > "$FAKE/systemctl" <<'EOF'
#!/usr/bin/env bash
set -u
printf '%s\n' "$*" >> /tmp/hb-v215-systemctl.log
target="${@: -1}"
if [ "${1:-}" = "is-active" ]; then
  case "$target" in
    hashburst-node|hashburst-node.service)
      [ -f /tmp/hb-v215-node-active ] && exit 0 || exit 1
      ;;
    ipfs|ipfs.service|ipfs-public|ipfs-public.service) exit 1 ;;
    *) exit 0 ;;
  esac
fi
if [ "${1:-}" = "enable" ] && [ "${2:-}" = "--now" ]; then
  case "$target" in
    hashburst-node|hashburst-node.service) touch /tmp/hb-v215-node-active ;;
  esac
fi
if [ "${1:-}" = "restart" ]; then
  case "$target" in
    hashburst-node|hashburst-node.service) touch /tmp/hb-v215-node-active ;;
  esac
fi
exit 0
EOF

cat > "$FAKE/curl" <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail
url=""
for arg in "\$@"; do
  case "\$arg" in http://*|https://*) url="\$arg";; esac
done
case "\$url" in
  http://127.0.0.1:47778/)
    node_id="\$(awk -F= '/^HB_TEP_NODE_ID=/{print \$2}' /etc/hashburst/hashburst-tep.env 2>/dev/null || true)"
    relay="\$(awk -F= '/^HB_TEP_RELAY_ENABLED=/{print \$2}' /etc/hashburst/hashburst-tep.env 2>/dev/null || true)"
    peer="\$(awk -F= '/^HB_TEP_PEER_ID=/{print \$2}' /etc/hashburst/hashburst-tep.env 2>/dev/null || true)"
    ready=false
    [ -n "\$peer" ] && ready=true
    [ "\$relay" = "1" ] && relay_json=true || relay_json=false
    printf '{"node_id":"%s","pubkey":"%s","crypto_mode":"AES-256-GCM","app_ready":%s,"relay":%s}\n' "\$node_id" "$PUBKEY" "\$ready" "\$relay_json"
    ;;
  http://127.0.0.1:8009/api/health)
    grep -q '^TEP_PUBKEY=$PUBKEY$' /etc/hashburst/env
    grep -q '^REWARD_ADDRESS=0x[0-9A-Fa-f]\{40\}$' /etc/hashburst/env
    grep -q '^NODE_KEYSTORE=/var/lib/hashburst/wallet$' /etc/hashburst/env
    grep -q '^NODE_KEYSTORE_PASSWORD_FILE=/etc/hashburst/wallet.pass$' /etc/hashburst/env
    printf '{"status":"ok","peerID":"%s","chainId":1337}\n' "$PEER_ID"
    ;;
  http://127.0.0.1:8091/health)
    printf '{"ok":true,"status":"ok"}\n'
    ;;
  *)
    echo "unexpected curl URL: \$url" >&2
    exit 22
    ;;
esac
EOF

cat > "$FAKE/ipfs" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
case "${1:-}" in
  version) echo "ipfs version 0.29.0" ;;
  init)
    mkdir -p "${IPFS_PATH:?}"
    printf '{}\n' > "$IPFS_PATH/config"
    ;;
  swarm)
    if [ "${2:-}" = "peers" ]; then
      echo "/ip4/85.233.199.35/tcp/4011/p2p/peer-primary"
    fi
    ;;
  *) : ;;
esac
EOF

cat > "$FAKE/ufw" <<'EOF'
#!/usr/bin/env bash
set -u
if [ "${1:-}" = "status" ]; then
  echo "Status: active"
  exit 0
fi
printf '%s\n' "$*" >> /tmp/hb-v215-ufw.log
exit 0
EOF

cat > "$FAKE/ss" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

cat > "$FAKE/journalctl" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

cat > "$FAKE/apt-get" <<'EOF'
#!/usr/bin/env bash
echo "unexpected apt-get in sandbox" >&2
exit 90
EOF

chmod 0755 "$FAKE"/*
export PATH="$FAKE:$PATH"

mkdir -p /tmp
cat > /tmp/swarm.key <<'EOF'
/key/swarm/psk/1.0.0/
/base16/
1111111111111111111111111111111111111111111111111111111111111111
EOF
SWARM_SHA="$(sha256sum /tmp/swarm.key | awk '{print $1}')"

ARGS=(
  --role full
  --storage-role edge
  --storage-backend filesystem
  --storage-path "$STORAGE"
  --capacity-gb 10
  --swarm-master-ip 85.233.199.35
  --swarm-peer-id peer-primary
  --aggregator-ip 64.31.4.9
  --node-name sandbox-edge
  --miner
  --public-ipfs-mode managed
)

cd "$ROOT"

echo "SANDBOX FRESH INSTALL"
rm -f "$NODE_STATE"
: > "$SYS_LOG"
./install.sh "${ARGS[@]}"

# A fresh node must take the start path, not the reinstall restart path.
grep -q '^enable --now hashburst-node.service$' "$SYS_LOG"
! grep -q '^restart hashburst-node.service$' "$SYS_LOG"
[ -f "$NODE_STATE" ]

[ "$(sha256sum /etc/hashburst/swarm.key | awk '{print $1}')" = "$SWARM_SHA" ]
grep -q '^VERSION="2.1.5"$' install.sh
grep -q '"version": "2.1.5"' /etc/hashburst/install-state.json
grep -q '^NODE_ID=sandbox-edge$' /etc/hashburst/env
grep -q '^MINER_ENABLED=true$' /etc/hashburst/env
grep -q '^HB_STORAGE_ROLE=edge$' /etc/hashburst/env
grep -q '^HB_FILES_BIND=127.0.0.1$' /etc/hashburst/env
grep -q '^HB_IPFS_PRIVATE_API=http://127.0.0.1:5011$' /etc/hashburst/env
grep -q "^TEP_PUBKEY=$PUBKEY$" /etc/hashburst/env
grep -q '^REWARD_ADDRESS=0x[0-9A-Fa-f]\{40\}$' /etc/hashburst/env
grep -q '^NODE_KEYSTORE=/var/lib/hashburst/wallet$' /etc/hashburst/env
grep -q '^NODE_KEYSTORE_PASSWORD_FILE=/etc/hashburst/wallet.pass$' /etc/hashburst/env
[ "$(stat -c %a /var/lib/hashburst/wallet)" = 700 ]
[ "$(stat -c %a /etc/hashburst/wallet.pass)" = 600 ]
WALLET_FILE="$(find /var/lib/hashburst/wallet -maxdepth 1 -type f -name 'UTC--*' -print -quit)"
[ -n "$WALLET_FILE" ]
[ "$(stat -c %a "$WALLET_FILE")" = 600 ]
grep -q '^HB_TEP_NODE_ID=sandbox-edge$' /etc/hashburst/hashburst-tep.env
grep -q "^HB_TEP_PEER_ID=$PEER_ID$" /etc/hashburst/hashburst-tep.env
grep -q '^HB_TEP_RELAY_ENABLED=0$' /etc/hashburst/hashburst-tep.env
grep -q "^HB_TEP_TRUSTED_RENDEZVOUS=$RENDEZVOUS$" /etc/hashburst/hashburst-tep.env
grep -q '^ExecStart=/usr/bin/python3 -m tep.hb_tep_runtime$' /etc/systemd/system/hashburst-tep.service
! grep -q 'allow from 64.31.4.9 to any port 8091' "$UFW_LOG"
grep -q 'delete allow 8091/tcp' "$UFW_LOG"

ADMIN1="$(awk -F= '/^HB_ADMIN_SECRET=/{print $2}' /etc/hashburst/env)"
PANEL1="$(awk -F= '/^HB_PANEL_SECRET=/{print $2}' /etc/hashburst/env)"
REWARD1="$(awk -F= '/^REWARD_ADDRESS=/{print $2}' /etc/hashburst/env)"
TEP_NODE_ID1="$(awk -F= '/^HB_TEP_NODE_ID=/{print $2}' /etc/hashburst/hashburst-tep.env)"
TEP_PEER_ID1="$(awk -F= '/^HB_TEP_PEER_ID=/{print $2}' /etc/hashburst/hashburst-tep.env)"
TEP_PUBKEY1="$(awk -F= '/^TEP_PUBKEY=/{print $2}' /etc/hashburst/env)"
WALLET_SHA1="$(sha256sum "$WALLET_FILE" | awk '{print $1}')"
PASS_SHA1="$(sha256sum /etc/hashburst/wallet.pass | awk '{print $1}')"
[ -n "$ADMIN1" ]
[ -n "$PANEL1" ]
[ -n "$REWARD1" ]
[ -n "$TEP_NODE_ID1" ]
[ -n "$TEP_PEER_ID1" ]
[ -n "$TEP_PUBKEY1" ]

echo "SANDBOX REINSTALL NORMALIZATION"
sed -i 's/^HB_TEP_RELAY_ENABLED=.*/HB_TEP_RELAY_ENABLED=1/' /etc/hashburst/hashburst-tep.env
sed -i 's/^HB_TEP_TRUSTED_RENDEZVOUS=.*/HB_TEP_TRUSTED_RENDEZVOUS=stale-peer/' /etc/hashburst/hashburst-tep.env
: > "$SYS_LOG"
./install.sh "${ARGS[@]}"

# A running node must reload EnvironmentFile through restart during onboarding.
grep -q '^restart hashburst-node.service$' "$SYS_LOG"
! grep -q '^enable --now hashburst-node.service$' "$SYS_LOG"
RESTART_LINE="$(grep -n '^restart hashburst-node.service$' "$SYS_LOG" | head -1 | cut -d: -f1)"
POST_INSTALL_ENABLE_LINE="$(grep -n '^enable --now hashburst-node$' "$SYS_LOG" | head -1 | cut -d: -f1)"
[ -n "$RESTART_LINE" ]
[ -n "$POST_INSTALL_ENABLE_LINE" ]
[ "$RESTART_LINE" -lt "$POST_INSTALL_ENABLE_LINE" ]

grep -q '^HB_TEP_RELAY_ENABLED=0$' /etc/hashburst/hashburst-tep.env
grep -q "^HB_TEP_TRUSTED_RENDEZVOUS=$RENDEZVOUS$" /etc/hashburst/hashburst-tep.env
[ "$(awk -F= '/^HB_ADMIN_SECRET=/{print $2}' /etc/hashburst/env)" = "$ADMIN1" ]
[ "$(awk -F= '/^HB_PANEL_SECRET=/{print $2}' /etc/hashburst/env)" = "$PANEL1" ]
[ "$(awk -F= '/^REWARD_ADDRESS=/{print $2}' /etc/hashburst/env)" = "$REWARD1" ]
[ "$(awk -F= '/^HB_TEP_NODE_ID=/{print $2}' /etc/hashburst/hashburst-tep.env)" = "$TEP_NODE_ID1" ]
[ "$(awk -F= '/^HB_TEP_PEER_ID=/{print $2}' /etc/hashburst/hashburst-tep.env)" = "$TEP_PEER_ID1" ]
[ "$(awk -F= '/^TEP_PUBKEY=/{print $2}' /etc/hashburst/env)" = "$TEP_PUBKEY1" ]
[ "$(sha256sum "$WALLET_FILE" | awk '{print $1}')" = "$WALLET_SHA1" ]
[ "$(sha256sum /etc/hashburst/wallet.pass | awk '{print $1}')" = "$PASS_SHA1" ]
[ "$(sha256sum /etc/hashburst/swarm.key | awk '{print $1}')" = "$SWARM_SHA" ]

echo "SANDBOX SWARM KEY MISMATCH"
cp /etc/hashburst/swarm.key /tmp/original-swarm.key
cat > /tmp/swarm.key <<'EOF'
/key/swarm/psk/1.0.0/
/base16/
2222222222222222222222222222222222222222222222222222222222222222
EOF
if ./install.sh "${ARGS[@]}" --dry-run >/tmp/hb-v215-mismatch.out 2>&1; then
  echo "swarm mismatch unexpectedly succeeded" >&2
  exit 1
fi
grep -q 'refusing federation key replacement' /tmp/hb-v215-mismatch.out
cmp -s /tmp/original-swarm.key /etc/hashburst/swarm.key
cp /tmp/original-swarm.key /tmp/swarm.key

echo "SANDBOX NODE ID MISMATCH"
cp /etc/hashburst/hashburst-tep.env /tmp/tep-env-good
sed -i 's/^HB_TEP_NODE_ID=.*/HB_TEP_NODE_ID=wrong-node/' /etc/hashburst/hashburst-tep.env
if bin/hb-tep-onboard sandbox-edge full edge >/tmp/hb-v215-nodeid.out 2>&1; then
  echo "TEP node-id mismatch unexpectedly succeeded" >&2
  exit 1
fi
grep -q 'refusing to replace existing HB_TEP_NODE_ID' /tmp/hb-v215-nodeid.out
cp /tmp/tep-env-good /etc/hashburst/hashburst-tep.env

echo "SANDBOX MISSING RUNTIME PREFLIGHT"
mv tep/hb_tep_runtime.py tep/hb_tep_runtime.py.sandbox-save
trap 'mv -f "$ROOT/tep/hb_tep_runtime.py.sandbox-save" "$ROOT/tep/hb_tep_runtime.py" 2>/dev/null || true; cleanup' EXIT
if ./install.sh "${ARGS[@]}" --dry-run >/tmp/hb-v215-runtime.out 2>&1; then
  echo "missing runtime unexpectedly succeeded" >&2
  exit 1
fi
grep -q 'required package file missing' /tmp/hb-v215-runtime.out
mv tep/hb_tep_runtime.py.sandbox-save tep/hb_tep_runtime.py
trap cleanup EXIT

echo "V215_INSTALLER_SANDBOX_PASS"
