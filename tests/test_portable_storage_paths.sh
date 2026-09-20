#!/usr/bin/env bash
set -euo pipefail

INSTALLER="ipfs-scripts/01-install-ipfs-dual-noZFS.sh"
VERIFY="ipfs-scripts/02-verify.sh"

test -f "$INSTALLER"
test -f "$VERIFY"

if grep -nE \
  'PUB_REPO="/datapool|PRV_REPO="/datapool' \
  "$VERIFY"
then
    echo "FAIL: fixed IPFS repository path found"
    exit 1
fi

grep -q 'HB_STORAGE_ROOT:-/var/lib/hashburst' "$INSTALLER"
grep -q 'HB_STORAGE_ROOT:-/var/lib/hashburst' "$VERIFY"
grep -q 'HB_PUBLIC_IPFS_MODE:-auto' "$INSTALLER"
grep -q 'HB_PUBLIC_IPFS_MODE:-auto' "$VERIFY"

bash -n "$INSTALLER"
bash -n "$VERIFY"

echo "PASS_PORTABLE_STORAGE_PATHS"
