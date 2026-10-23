#!/usr/bin/env bash
# The bundled blockchain executable is Linux x86_64. Reject unsupported hosts
# before installing packages or modifying services.
hashburst_require_supported_platform() {
  local hb_os hb_arch
  hb_os="$(uname -s)" || return 1
  hb_arch="$(uname -m)" || return 1
  if [ "$hb_os" != Linux ] || [ "$hb_arch" != x86_64 ]; then
    echo "ERROR: this installer bundles a Linux x86_64 node; unsupported host: $hb_os/$hb_arch" >&2
    echo "Use a platform-specific package after its runtime acceptance; no changes performed." >&2
    return 1
  fi
}
