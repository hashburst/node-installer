#!/usr/bin/env bash
set -eu
source "$(dirname "$0")/../lib/platform-guard.sh"
uname() { case "$1" in -s) echo "$TEST_OS";; -m) echo "$TEST_ARCH";; esac; }
TEST_OS=Linux TEST_ARCH=x86_64 hashburst_require_supported_platform
for spec in Linux:aarch64 Darwin:arm64 Darwin:x86_64 MINGW64_NT:x86_64 Linux:armv7l; do
  TEST_OS="${spec%:*}"; TEST_ARCH="${spec#*:}"
  if hashburst_require_supported_platform 2>/dev/null; then
    echo "unsupported platform admitted: $spec" >&2; exit 1
  fi
done
echo PLATFORM_GUARD_OK
