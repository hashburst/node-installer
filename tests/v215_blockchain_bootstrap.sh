#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT="${1:-$(pwd)}"
TMP="$(mktemp -d /tmp/hb-v215-chain.XXXXXX)"
ENV_FILE="$TMP/env"
WALLET_DIR="$TMP/wallet"
PASS_FILE="$TMP/wallet.pass"
CHAIN_DIR="$TMP/chain"
LOG="$TMP/node.log"
RPC_PORT=18009
P2P_PORT=31307
PID=""

cleanup() {
  set +e
  [ -n "$PID" ] && kill "$PID" 2>/dev/null || true
  [ -n "$PID" ] && wait "$PID" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT

cat > "$ENV_FILE" <<EOF
NODE_ID=sandbox-full
MINER_ENABLED=false
STORAGE_DIR=$CHAIN_DIR
TEP_PUBKEY=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
EXTERNAL_IP=127.0.0.1
RPC_PORT=$RPC_PORT
P2P_PORT=$P2P_PORT
P2P_KEY_PATH=$CHAIN_DIR/node_p2p.key
RPC_ENDPOINT=http://127.0.0.1:$RPC_PORT
EOF
chmod 600 "$ENV_FILE"
mkdir -m 700 "$CHAIN_DIR"

run_bootstrap() {
  HB_WALLET_ALLOW_NONROOT=1 \
  HB_NODE_ENV="$ENV_FILE" \
  HB_NODE_BIN="$ROOT/bin/hashburst-node" \
  HB_WALLET_DIR="$WALLET_DIR" \
  HB_WALLET_PASS_FILE="$PASS_FILE" \
  bash "$ROOT/bin/hb-node-wallet-bootstrap"
}

echo "BLOCKCHAIN WALLET FIRST BOOT"
run_bootstrap
REWARD1="$(awk -F= '/^REWARD_ADDRESS=/{print $2}' "$ENV_FILE")"
KEYSTORE1="$(awk -F= '/^NODE_KEYSTORE=/{print substr($0,index($0,"=")+1)}' "$ENV_FILE")"
PASS_ENV1="$(awk -F= '/^NODE_KEYSTORE_PASSWORD_FILE=/{print substr($0,index($0,"=")+1)}' "$ENV_FILE")"
[[ "$REWARD1" =~ ^0x[0-9A-Fa-f]{40}$ ]]
[ "$PASS_ENV1" = "$PASS_FILE" ]
[ -f "$KEYSTORE1" ]
[ "$(find "$WALLET_DIR" -maxdepth 1 -type f -name 'UTC--*' | wc -l)" -eq 1 ]
[ "$(stat -c %a "$PASS_FILE")" = 600 ]
[ "$(stat -c %a "$KEYSTORE1")" = 600 ]

KS_SHA1="$(sha256sum "$KEYSTORE1" | awk '{print $1}')"
PASS_SHA1="$(sha256sum "$PASS_FILE" | awk '{print $1}')"

echo "BLOCKCHAIN WALLET REINSTALL"
run_bootstrap
REWARD2="$(awk -F= '/^REWARD_ADDRESS=/{print $2}' "$ENV_FILE")"
KEYSTORE2="$(awk -F= '/^NODE_KEYSTORE=/{print substr($0,index($0,"=")+1)}' "$ENV_FILE")"
PASS_ENV2="$(awk -F= '/^NODE_KEYSTORE_PASSWORD_FILE=/{print substr($0,index($0,"=")+1)}' "$ENV_FILE")"
[ "$REWARD2" = "$REWARD1" ]
[ "$KEYSTORE2" = "$KEYSTORE1" ]
[ "$PASS_ENV2" = "$PASS_ENV1" ]
[ "$(find "$WALLET_DIR" -maxdepth 1 -type f -name 'UTC--*' | wc -l)" -eq 1 ]
[ "$(sha256sum "$KEYSTORE1" | awk '{print $1}')" = "$KS_SHA1" ]
[ "$(sha256sum "$PASS_FILE" | awk '{print $1}')" = "$PASS_SHA1" ]

echo "BLOCKCHAIN REAL BINARY HEALTH"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
"$ROOT/bin/hashburst-node" >"$LOG" 2>&1 &
PID=$!

HEALTH=""
for _ in $(seq 1 80); do
  if HEALTH="$(curl -fsS --max-time 1 "http://127.0.0.1:$RPC_PORT/api/health" 2>/dev/null)"; then
    break
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    cat "$LOG" >&2
    echo "blockchain exited before health became ready" >&2
    exit 1
  fi
  sleep 0.25
done
[ -n "$HEALTH" ] || { cat "$LOG" >&2; echo "blockchain health timeout" >&2; exit 1; }

python3 - "$HEALTH" <<'PY'
import json,sys
d=json.loads(sys.argv[1])
assert d.get('status') == 'ok'
assert d.get('chainId') == 1337
peer=d.get('peerID') or d.get('peer_id')
assert isinstance(peer,str) and peer
print('blockchain_peer_id='+peer)
PY

kill "$PID"
wait "$PID" || true
PID=""

if grep -Eq 'REWARD_ADDRESS not set|NODE_KEYSTORE non impostato|NODE_KEYSTORE_PASSWORD_FILE non leggibile' "$LOG"; then
  cat "$LOG" >&2
  echo "blockchain identity bootstrap regression" >&2
  exit 1
fi

echo V215_BLOCKCHAIN_BOOTSTRAP_PASS
