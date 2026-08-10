#!/usr/bin/env bash
# Read-only operational probe for the current HashBurst topology.
set -u
AGG="${1:-https://blockchainapi.one}"
probe(){ url="$1"; echo "=== $url"; curl -fsS --connect-timeout 4 --max-time 10 "$url" | head -c 4096; echo; }
probe "$AGG/api/network/storage"
probe "$AGG/api/hashburst/mining/summary"
for n in 85.233.199.35 77.90.188.155; do
  echo "=== storage node $n"
  curl -fsS --connect-timeout 4 --max-time 8 "http://$n:8091/api/public/storage-summary" | head -c 4096 || echo "unreachable from this vantage point"
  echo
done
