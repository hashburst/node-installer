#!/usr/bin/env bash
set -euo pipefail

KUBO_VERSION="${HB_KUBO_VERSION:-v0.29.0}"
ARCH="${HB_ARCH:-amd64}"
HB_ROOT="${HB_STORAGE_ROOT:-/var/lib/hashburst}"
PUBLIC_MODE="${HB_PUBLIC_IPFS_MODE:-auto}"   # reuse|managed|disabled
PUB_REPO="$HB_ROOT/ipfs-public"
PRV_REPO="$HB_ROOT/ipfs-private"
PUB_API=5001; PUB_GW=8080; PUB_SWARM=4001
PRV_API=5011; PRV_GW=8090; PRV_SWARM=4011

mkdir -p "$HB_ROOT" "$PRV_REPO" /etc/hashburst
[ -f /etc/hashburst/swarm.key ] || {
  echo "ERROR: /etc/hashburst/swarm.key missing; private IPFS is fail-closed." >&2
  exit 1
}

# Never downgrade or replace an existing Kubo binary. Package/runtime upgrades
# are a separate maintenance operation because repo migrations may be involved.
if ! command -v ipfs >/dev/null 2>&1; then
  tmp="$(mktemp -d /tmp/hashburst-kubo.XXXXXX)"
  trap 'rm -rf "$tmp"' EXIT
  base="https://dist.ipfs.tech/kubo/${KUBO_VERSION}"
  tarball="kubo_${KUBO_VERSION}_linux-${ARCH}.tar.gz"
  curl -fL --retry 3 --connect-timeout 10 "$base/$tarball" -o "$tmp/$tarball"
  curl -fL --retry 3 --connect-timeout 10 "$base/$tarball.sha512" -o "$tmp/$tarball.sha512"
  (cd "$tmp" && sha512sum -c "$tarball.sha512")
  tar -xzf "$tmp/$tarball" -C "$tmp"
  install -m 0755 "$tmp/kubo/ipfs" /usr/local/bin/ipfs
  echo "Installed $(ipfs version)"
else
  echo "Reusing existing $(ipfs version); no implicit downgrade/upgrade."
fi

# Public plane is optional. Existing public daemons remain untouched in reuse mode.
case "$PUBLIC_MODE" in
  reuse)
    echo "Public IPFS: reusing pre-existing daemon; no repository/service changes."
    ;;
  disabled)
    echo "Public IPFS: disabled by policy."
    ;;
  managed)
    mkdir -p "$PUB_REPO"
    if [ ! -f "$PUB_REPO/config" ]; then IPFS_PATH="$PUB_REPO" ipfs init --profile server >/dev/null; fi
    IPFS_PATH="$PUB_REPO" ipfs config Addresses.API "/ip4/127.0.0.1/tcp/$PUB_API"
    IPFS_PATH="$PUB_REPO" ipfs config Addresses.Gateway "/ip4/127.0.0.1/tcp/$PUB_GW"
    IPFS_PATH="$PUB_REPO" ipfs config --json Addresses.Swarm "[\"/ip4/0.0.0.0/tcp/$PUB_SWARM\",\"/ip6/::/tcp/$PUB_SWARM\"]"
    cat > /etc/systemd/system/ipfs-public.service <<UNIT
[Unit]
Description=IPFS public daemon managed by HashBurst
After=network-online.target
Wants=network-online.target
[Service]
Environment=IPFS_PATH=$PUB_REPO
ExecStart=$(command -v ipfs) daemon --migrate=true
Restart=on-failure
RestartSec=10
LimitNOFILE=65536
[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
    systemctl enable --now ipfs-public
    ;;
  *) echo "ERROR: invalid HB_PUBLIC_IPFS_MODE=$PUBLIC_MODE" >&2; exit 2;;
esac

# Private plane: always managed by HashBurst for storage/full/edge nodes.
if [ ! -f "$PRV_REPO/config" ]; then IPFS_PATH="$PRV_REPO" ipfs init --profile server >/dev/null; fi
install -m 0600 /etc/hashburst/swarm.key "$PRV_REPO/swarm.key"
IPFS_PATH="$PRV_REPO" ipfs bootstrap rm --all >/dev/null 2>&1 || true
IPFS_PATH="$PRV_REPO" ipfs config Addresses.API "/ip4/127.0.0.1/tcp/$PRV_API"
IPFS_PATH="$PRV_REPO" ipfs config Addresses.Gateway "/ip4/127.0.0.1/tcp/$PRV_GW"
IPFS_PATH="$PRV_REPO" ipfs config --json Addresses.Swarm "[\"/ip4/0.0.0.0/tcp/$PRV_SWARM\",\"/ip6/::/tcp/$PRV_SWARM\"]"

cat > /etc/systemd/system/ipfs-private.service <<UNIT
[Unit]
Description=HashBurst private IPFS sovereign network
After=network-online.target
Wants=network-online.target
[Service]
Environment=IPFS_PATH=$PRV_REPO
Environment=LIBP2P_FORCE_PNET=1
ExecStart=$(command -v ipfs) daemon --migrate=true
Restart=on-failure
RestartSec=10
LimitNOFILE=65536
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now ipfs-private
systemctl is-active --quiet ipfs-private || { journalctl -u ipfs-private -n 50 --no-pager; exit 1; }
