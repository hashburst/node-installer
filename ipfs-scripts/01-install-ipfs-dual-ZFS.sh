#!/usr/bin/env bash
# Compatibility wrapper: v2.1 uses one IPFS setup path; the installer resolves
# the ZFS mountpoint first and passes it through HB_STORAGE_ROOT.
set -euo pipefail
exec "$(dirname "$0")/01-install-ipfs-dual-noZFS.sh" "$@"
