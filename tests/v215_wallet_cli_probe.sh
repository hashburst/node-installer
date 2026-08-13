#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT="${1:-$(pwd)}"
TMP="$(mktemp -d /tmp/hb-v215-wallet-probe.XXXXXX)"
PASS="$TMP/wallet.pass"
WALLET="$TMP/keystore"
trap 'rm -rf "$TMP"' EXIT

printf '%s\n' "$(openssl rand -hex 32)" > "$PASS"
chmod 600 "$PASS"
mkdir -m 700 "$WALLET"

set +e
OUT="$($ROOT/bin/hashburst-node wallet new --dir "$WALLET" --password-file "$PASS" 2>&1)"
RC=$?
set -e

printf 'wallet_cli_rc=%s\n' "$RC"
printf '%s\n' "$OUT"
find "$WALLET" -maxdepth 2 -type f -printf 'wallet_file=%f size=%s mode=%m\n' || true

[ "$RC" -eq 0 ] || exit "$RC"

ADDR="$(printf '%s\n' "$OUT" | grep -Eo '0x[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64}' | head -1 || true)"
if [ -z "$ADDR" ]; then
  ADDR="$(python3 - "$WALLET" <<'PY'
import json, pathlib, re, sys
root=pathlib.Path(sys.argv[1])
for p in sorted(root.rglob('*.json')):
    try:
        d=json.loads(p.read_text())
    except Exception:
        continue
    for key in ('address','Address'):
        v=d.get(key)
        if isinstance(v,str) and re.fullmatch(r'(?:0x)?[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64}',v):
            print(v); raise SystemExit
PY
)"
fi

[ -n "$ADDR" ] || { echo 'wallet address not discoverable' >&2; exit 1; }
printf 'wallet_address=%s\n' "$ADDR"

echo 'binary_keystore_env_contract:'
strings "$ROOT/bin/hashburst-node" | grep -E 'NODE_KEYSTORE|KEYSTORE.*PASS|PASSWORD.*FILE|NODE_.*PASS' | sort -u | head -50 || true

echo V215_WALLET_CLI_PROBE_PASS
