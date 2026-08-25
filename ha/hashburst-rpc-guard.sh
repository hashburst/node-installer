#!/usr/bin/env bash
set -euo pipefail

PORT="${HB_RPC_GUARD_PORT:-8009}"

[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
[[ "$PORT" =~ ^[0-9]+$ ]] || { echo "Invalid HB_RPC_GUARD_PORT: $PORT" >&2; exit 2; }
(( PORT >= 1 && PORT <= 65535 )) || { echo "Invalid HB_RPC_GUARD_PORT: $PORT" >&2; exit 2; }

command -v iptables >/dev/null 2>&1 || {
  echo "iptables is required for the HashBurst RPC ingress guard." >&2
  exit 1
}

if ! iptables -C INPUT ! -i lo -p tcp --dport "$PORT" -j DROP 2>/dev/null; then
  iptables -I INPUT 1 ! -i lo -p tcp --dport "$PORT" -j DROP
fi

if command -v ip6tables >/dev/null 2>&1; then
  if ! ip6tables -C INPUT ! -i lo -p tcp --dport "$PORT" -j DROP 2>/dev/null; then
    ip6tables -I INPUT 1 ! -i lo -p tcp --dport "$PORT" -j DROP
  fi
fi

echo "HashBurst RPC ingress guard active: TCP/${PORT} is loopback-only."
