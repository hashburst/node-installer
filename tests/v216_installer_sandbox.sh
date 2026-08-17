#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${1:-$(pwd)}"
TMP="$(mktemp /tmp/hb-v216-installer-sandbox.XXXXXX.sh)"
cleanup() { rm -f "$TMP"; }
trap cleanup EXIT

# Reuse the proven v2.1.5 fresh/reinstall sandbox, changing only the release
# version assertions. This exercises the same real installer path twice:
# first as a fresh 2.1.6 install, then as a 2.1.6 -> 2.1.6 reinstall.
# Identity/wallet/swarm preservation and the controlled restart contract remain
# asserted by the underlying sandbox.
sed \
  -e 's/^grep -q '\''^VERSION="2\.1\.5"\$'\'' install\.sh$/grep -q '\''^VERSION="2.1.6"$'\'' install.sh/' \
  -e 's/^grep -q '\''"version": "2\.1\.5"'\'' \/etc\/hashburst\/install-state\.json$/grep -q '\''"version": "2.1.6"'\'' \/etc\/hashburst\/install-state.json/' \
  -e 's/V215_INSTALLER_SANDBOX_PASS/V216_INSTALLER_SANDBOX_PASS/' \
  "$ROOT/tests/v215_installer_sandbox.sh" > "$TMP"
chmod 0755 "$TMP"

sudo env PATH="$PATH" bash "$TMP" "$ROOT"
