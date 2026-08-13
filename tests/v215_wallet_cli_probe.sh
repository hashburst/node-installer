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
[ "$RC" -eq 0 ] || { printf '%s\n' "$OUT" >&2; exit "$RC"; }

KEYSTORE="$(find "$WALLET" -maxdepth 1 -type f -name 'UTC--*' -print -quit)"
[ -n "$KEYSTORE" ]
[ "$(find "$WALLET" -maxdepth 1 -type f -name 'UTC--*' | wc -l)" -eq 1 ]
[ "$(stat -c %a "$KEYSTORE")" = 600 ]

ADDR="$(printf '%s\n' "$OUT" | grep -Eo '0x[0-9A-Fa-f]{40}' | head -1 || true)"
[ -n "$ADDR" ] || { echo 'wallet address not discoverable' >&2; exit 1; }
printf 'wallet_address=%s\n' "$ADDR"
echo V215_WALLET_CLI_PROBE_PASS
