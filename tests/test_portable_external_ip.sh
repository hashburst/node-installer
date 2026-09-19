#!/usr/bin/env bash
set -euo pipefail

HB_FILES="hbfiles/hb_files.py"
HB_PANEL="hbfiles/hb_files_panel.py"
INSTALLER="install.sh"

for file in "$HB_FILES" "$HB_PANEL" "$INSTALLER"; do
    test -f "$file"
done

if grep -n '85\.233\.199\.35' \
    "$HB_FILES" \
    "$HB_PANEL" \
    "$INSTALLER"
then
    echo "FAIL: infrastructure-specific address remains"
    exit 1
fi

grep -Fq \
  "os.environ.get('EXTERNAL_IP', '').strip()" \
  "$HB_FILES"

grep -Fq \
  "if server_ip else ''" \
  "$HB_FILES"

grep -Fq \
  'EXTERNAL_IP=""' \
  "$INSTALLER"

grep -Fq -- \
  '--external-ip ADDRESS' \
  "$INSTALLER"

grep -Fq -- \
  '--external-ip) EXTERNAL_IP="$2"; shift 2;;' \
  "$INSTALLER"

grep -Fq \
  'EXTERNAL_IP=${EXTERNAL_IP}' \
  "$INSTALLER"

grep -Fq \
  '"external_ip": "${EXTERNAL_IP}"' \
  "$INSTALLER"

grep -Fq -- \
  '--external-ip is required for primary and secondary storage nodes' \
  "$INSTALLER"

bash -n "$INSTALLER"

echo "PASS_PORTABLE_EXTERNAL_IP"
