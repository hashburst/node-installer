#!/usr/bin/env bash
set -euo pipefail

VERSION="2.1.4"
ROLE="storage"                    # storage | full | blockchain | edge
STORAGE_ROLE=""                  # primary | secondary | edge
STORAGE_BACKEND="auto"           # auto | zfs | filesystem
STORAGE_PATH=""
ZFS_DATASET="datapool/hashburst"
CAPACITY_GB=""
SWARM_MASTER_IP=""
SWARM_PEER_ID=""
AGGREGATOR_IP=""
NODE_NAME=""
PRIMARY="no"
MINER="false"
PUBLIC_IPFS_MODE="auto"          # auto | reuse | managed | disabled
FILES_BIND="auto"                # auto | 127.0.0.1 | 0.0.0.0
ALLOW_UNFIREWALLED_SUMMARY="no"
KUBO_VERSION="v0.29.0"
DRY_RUN="no"

usage() {
  cat <<USAGE
HashBurst Node Installer v${VERSION}

Usage:
  sudo ./install.sh [options]

Core options:
  --role storage|full|blockchain|edge
  --storage-role primary|secondary|edge
  --storage-backend auto|zfs|filesystem
  --storage-path PATH
  --zfs-dataset DATASET
  --capacity-gb N
  --primary                         shorthand for --storage-role primary
  --node-name NAME
  --miner

Federation:
  --swarm-master-ip IP
  --swarm-peer-id PEER_ID
  --aggregator-ip IP

IPFS public daemon handling:
  --public-ipfs-mode auto|reuse|managed|disabled
  --files-bind auto|127.0.0.1|0.0.0.0
  --allow-unfirewalled-summary       explicit override for remote :8091

Safety:
  --dry-run

Examples:
  sudo ./install.sh --role full --storage-role primary \
    --storage-backend filesystem --storage-path /datapool/hashburst \
    --capacity-gb 5120 --public-ipfs-mode auto

  sudo ./install.sh --role storage --storage-role secondary \
    --capacity-gb 400 --swarm-master-ip 85.233.199.35 \
    --swarm-peer-id PEER --aggregator-ip 64.31.4.9

  sudo ./install.sh --role edge --storage-role edge \
    --capacity-gb 100 --swarm-master-ip 85.233.199.35 --swarm-peer-id PEER
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --role) ROLE="$2"; shift 2;;
    --storage-role) STORAGE_ROLE="$2"; shift 2;;
    --storage-backend) STORAGE_BACKEND="$2"; shift 2;;
    --storage-path) STORAGE_PATH="$2"; shift 2;;
    --zfs-dataset) ZFS_DATASET="$2"; shift 2;;
    --capacity-gb) CAPACITY_GB="$2"; shift 2;;
    --swarm-master-ip) SWARM_MASTER_IP="$2"; shift 2;;
    --swarm-peer-id) SWARM_PEER_ID="$2"; shift 2;;
    --aggregator-ip) AGGREGATOR_IP="$2"; shift 2;;
    --node-name) NODE_NAME="$2"; shift 2;;
    --public-ipfs-mode) PUBLIC_IPFS_MODE="$2"; shift 2;;
    --files-bind) FILES_BIND="$2"; shift 2;;
    --allow-unfirewalled-summary) ALLOW_UNFIREWALLED_SUMMARY="yes"; shift;;
    --kubo-version) KUBO_VERSION="$2"; shift 2;;
    --primary) PRIMARY="yes"; STORAGE_ROLE="primary"; shift;;
    --miner) MINER="true"; shift;;
    --dry-run) DRY_RUN="yes"; shift;;
    -h|--help) usage; exit 0;;
    *) echo "ERROR: unknown option: $1" >&2; usage; exit 2;;
  esac
done

case "$ROLE" in storage|full|blockchain|edge) ;; *) echo "ERROR: invalid --role" >&2; exit 2;; esac
case "$STORAGE_BACKEND" in auto|zfs|filesystem) ;; *) echo "ERROR: invalid --storage-backend" >&2; exit 2;; esac
case "$PUBLIC_IPFS_MODE" in auto|reuse|managed|disabled) ;; *) echo "ERROR: invalid --public-ipfs-mode" >&2; exit 2;; esac
case "$FILES_BIND" in auto|127.0.0.1|0.0.0.0) ;; *) echo "ERROR: invalid --files-bind" >&2; exit 2;; esac

[ "$(id -u)" = 0 ] || { echo "ERROR: run as root" >&2; exit 1; }
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -n "$NODE_NAME" ] || NODE_NAME="hb-$(hostname)"

if [ "$ROLE" = "blockchain" ]; then
  STORAGE_ROLE="none"
elif [ -z "$STORAGE_ROLE" ]; then
  [ "$ROLE" = "edge" ] && STORAGE_ROLE="edge" || STORAGE_ROLE="secondary"
fi
case "$STORAGE_ROLE" in primary|secondary|edge|none) ;; *) echo "ERROR: invalid --storage-role" >&2; exit 2;; esac

run() { if [ "$DRY_RUN" = yes ]; then printf '[dry-run]'; printf ' %q' "$@"; echo; else "$@"; fi; }
need_file() { [ -f "$1" ] || { echo "ERROR: required package file missing: $1" >&2; exit 1; }; }
port_listening() { ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "(^|:)$1$"; }

need_file "$SCRIPT_DIR/bin/hashburst-node"
need_file "$SCRIPT_DIR/replication/hb_replication_controller.py"
need_file "$SCRIPT_DIR/replication/hb_replication_controller_v214.py"
need_file "$SCRIPT_DIR/replication/hb_replication_v214_db.py"
need_file "$SCRIPT_DIR/replication/hb_replica_agent.py"
need_file "$SCRIPT_DIR/replication/hb_replica_agent_v214.py"
need_file "$SCRIPT_DIR/hbfiles/hb_ipfs.py"
need_file "$SCRIPT_DIR/config/replication-controller.env.example"
need_file "$SCRIPT_DIR/config/replica-agent.env.example"
need_file "$SCRIPT_DIR/tep/hb_tep.py"
need_file "$SCRIPT_DIR/tep/hb_tep_app.py"
need_file "$SCRIPT_DIR/tep/hb_tep_client.py"
need_file "$SCRIPT_DIR/tep/hb_tep_relay.py"
need_file "$SCRIPT_DIR/tep/hb_tep_services.py"
need_file "$SCRIPT_DIR/tep/hb_tep_wire.py"
need_file "$SCRIPT_DIR/config/hashburst-tep.env.example"
need_file "$SCRIPT_DIR/systemd/hashburst-tep.service"
if [ "$ROLE" != blockchain ]; then
  need_file "$SCRIPT_DIR/hbfiles/hb_files.py"
  need_file "$SCRIPT_DIR/ipfs-scripts/01-install-ipfs-dual-noZFS.sh"
fi

ZFS_OK="no"
if command -v zfs >/dev/null 2>&1 && command -v zpool >/dev/null 2>&1 \
   && zfs list -H "$ZFS_DATASET" >/dev/null 2>&1; then
  ZFS_OK="yes"
fi
if [ "$STORAGE_BACKEND" = auto ]; then
  [ "$ZFS_OK" = yes ] && STORAGE_BACKEND="zfs" || STORAGE_BACKEND="filesystem"
fi
if [ "$STORAGE_BACKEND" = zfs ] && [ "$ZFS_OK" != yes ]; then
  echo "ERROR: ZFS backend requested but dataset '$ZFS_DATASET' is not usable." >&2
  echo "Use --storage-backend filesystem --storage-path /datapool/hashburst only if that mount is intentionally managed outside ZFS." >&2
  exit 1
fi
if [ -z "$STORAGE_PATH" ]; then
  if [ "$STORAGE_BACKEND" = zfs ]; then
    STORAGE_PATH="$(zfs get -H -o value mountpoint "$ZFS_DATASET")"
  else
    STORAGE_PATH="/var/lib/hashburst"
  fi
fi

if [ "$ROLE" != blockchain ] && [ "$STORAGE_ROLE" != primary ]; then
  [ -f /tmp/swarm.key ] || [ -f /etc/hashburst/swarm.key ] || {
    echo "ERROR: swarm.key missing. Non-primary nodes must receive the network swarm.key; refusing to create a split private network." >&2
    exit 1
  }
  [ -n "$SWARM_MASTER_IP" ] && [ -n "$SWARM_PEER_ID" ] || {
    echo "ERROR: non-primary storage/edge node requires --swarm-master-ip and --swarm-peer-id" >&2
    exit 1
  }
fi

UFW_ACTIVE="no"
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then UFW_ACTIVE="yes"; fi
if [ "$FILES_BIND" = auto ]; then
  if [ "$STORAGE_ROLE" != edge ] && [ -n "$AGGREGATOR_IP" ] && [ "$UFW_ACTIVE" = yes ]; then FILES_BIND="0.0.0.0"; else FILES_BIND="127.0.0.1"; fi
fi
if [ "$FILES_BIND" = "0.0.0.0" ] && [ "$UFW_ACTIVE" != yes ] && [ "$ALLOW_UNFIREWALLED_SUMMARY" != yes ]; then
  echo "ERROR: refusing to expose :8091 on all interfaces while UFW is inactive. Enable UFW or pass --allow-unfirewalled-summary explicitly." >&2
  exit 1
fi

if [ "$PUBLIC_IPFS_MODE" = auto ]; then
  if port_listening 5001 || systemctl is-active --quiet ipfs 2>/dev/null || systemctl is-active --quiet ipfs-public 2>/dev/null; then
    PUBLIC_IPFS_MODE="reuse"
  else
    PUBLIC_IPFS_MODE="managed"
  fi
fi
if [ "$PUBLIC_IPFS_MODE" = managed ] && port_listening 5001; then
  echo "ERROR: :5001 already in use; choose --public-ipfs-mode reuse or disabled." >&2
  exit 1
fi

cat <<INFO
============================================================
 HashBurst Node Installer v${VERSION}
 node:             ${NODE_NAME}
 role:             ${ROLE}
 storage role:     ${STORAGE_ROLE}
 storage backend:  ${STORAGE_BACKEND}
 storage path:     ${STORAGE_PATH}
 public IPFS mode: ${PUBLIC_IPFS_MODE}
 HB-Files bind:    ${FILES_BIND}
============================================================
INFO
[ "$DRY_RUN" = yes ] && exit 0

install -d -m 0755 /etc/hashburst /var/log/hashburst /var/lib/hashburst
install -m 0755 "$SCRIPT_DIR/bin/hashburst-node" /usr/local/bin/hashburst-node

if [ "$ROLE" != blockchain ]; then
  if [ -f /tmp/swarm.key ]; then install -m 0600 /tmp/swarm.key /etc/hashburst/swarm.key; fi
  if [ "$STORAGE_ROLE" = primary ] && [ ! -f /etc/hashburst/swarm.key ]; then
    umask 077
    { printf '/key/swarm/psk/1.0.0/\n/base16/\n'; od -A none -t x1 -N 32 /dev/urandom | tr -d ' \n'; printf '\n'; } > /etc/hashburst/swarm.key
  fi
fi

OLD_ADMIN=""; OLD_PANEL=""
if [ -f /etc/hashburst/env ]; then
  OLD_ADMIN="$(grep '^HB_ADMIN_SECRET=' /etc/hashburst/env | head -1 | cut -d= -f2- || true)"
  OLD_PANEL="$(grep '^HB_PANEL_SECRET=' /etc/hashburst/env | head -1 | cut -d= -f2- || true)"
fi
ADMIN_SECRET="${OLD_ADMIN:-$(openssl rand -hex 32)}"
PANEL_SECRET="${OLD_PANEL:-$(openssl rand -hex 32)}"

# v2.1.4 ships the replication lifecycle components on every role but never
# enables controller/agent automatically. Observe mode and both UNPIN gates are
# fail-closed defaults. hb_ipfs.py is also installed adjacent to the agent so
# direct systemd execution does not depend on PYTHONPATH.
install -d -m 0755 /opt/hashburst/replication /var/lib/hashburst/replication
cp -a "$SCRIPT_DIR/replication/." /opt/hashburst/replication/
install -m 0644 "$SCRIPT_DIR/hbfiles/hb_ipfs.py" /opt/hashburst/replication/hb_ipfs.py
if [ ! -f /etc/hashburst/replication-controller.env ]; then
  install -m 0600 "$SCRIPT_DIR/config/replication-controller.env.example" /etc/hashburst/replication-controller.env
fi
if [ ! -f /etc/hashburst/replica-agent.env ]; then
  install -m 0600 "$SCRIPT_DIR/config/replica-agent.env.example" /etc/hashburst/replica-agent.env
fi

# HB-TEP is packaged on every role but never enabled automatically.
# Existing production configuration is preserved.
install -d -m 0755 /opt/hashburst-tep/tep /var/lib/hashburst/tep
cp -a "$SCRIPT_DIR/tep/." /opt/hashburst-tep/tep/
if [ ! -f /etc/hashburst/hashburst-tep.env ]; then
  install -m 0600 "$SCRIPT_DIR/config/hashburst-tep.env.example" /etc/hashburst/hashburst-tep.env
fi

if [ "$ROLE" != blockchain ]; then
  HB_STORAGE_ROOT="$STORAGE_PATH" HB_PUBLIC_IPFS_MODE="$PUBLIC_IPFS_MODE" \
    HB_KUBO_VERSION="$KUBO_VERSION" bash "$SCRIPT_DIR/ipfs-scripts/01-install-ipfs-dual-noZFS.sh"
  PRV_REPO="$STORAGE_PATH/ipfs-private"

  if [ "$STORAGE_ROLE" != primary ]; then
    IPFS_PATH="$PRV_REPO" ipfs bootstrap rm --all >/dev/null 2>&1 || true
    IPFS_PATH="$PRV_REPO" ipfs bootstrap add "/ip4/${SWARM_MASTER_IP}/tcp/4011/p2p/${SWARM_PEER_ID}"
    systemctl restart ipfs-private
  fi

  install -d -m 0755 /opt/hashburst-files
  cp -a "$SCRIPT_DIR/hbfiles/." /opt/hashburst-files/
  if [ -f /tmp/list.json ]; then install -m 0600 /tmp/list.json /etc/hashburst/list.json; fi
fi

cat > /etc/hashburst/env <<ENV
NODE_ID=${NODE_NAME}
MINER_ENABLED=${MINER}
HB_ADMIN_SECRET=${ADMIN_SECRET}
HB_PANEL_SECRET=${PANEL_SECRET}
HB_LIST_JSON_PATH=/etc/hashburst/list.json
HB_FILES_DEFAULT_QUOTA_GB=2
HB_IPFS_PRIVATE_API=http://127.0.0.1:5011
HB_STORAGE_ROLE=${STORAGE_ROLE}
HB_STORAGE_BACKEND=${STORAGE_BACKEND}
HB_ZFS_DATASET=${ZFS_DATASET}
STORAGE_DIR=${STORAGE_PATH}
HB_FILES_STORAGE=${STORAGE_PATH}/files
HB_FILES_META=${STORAGE_PATH}/files-meta
HB_FILES_BIND=${FILES_BIND}
HB_FILES_PUBLIC_SUMMARY_BIND=0.0.0.0
HB_FILES_PORT=8091
HB_FILES_MAX_MB=10240
HB_AUTH_MODE=legacy
HB_REPL_HOOK_ENABLED=0
HB_REPL_CONTROLLER=http://127.0.0.1:8095
HB_REPL_HOOK_TOKEN=
ENV
[ -n "$CAPACITY_GB" ] && echo "HB_CAPACITY_LIMIT_GB=${CAPACITY_GB}" >> /etc/hashburst/env
chmod 600 /etc/hashburst/env

cat > /etc/hashburst/install-state.json <<STATE
{
  "version": "${VERSION}",
  "node_id": "${NODE_NAME}",
  "role": "${ROLE}",
  "storage_role": "${STORAGE_ROLE}",
  "storage_backend": "${STORAGE_BACKEND}",
  "storage_path": "${STORAGE_PATH}",
  "zfs_dataset": "${ZFS_DATASET}",
  "public_ipfs_mode": "${PUBLIC_IPFS_MODE}"
}
STATE
chmod 600 /etc/hashburst/install-state.json

cp "$SCRIPT_DIR"/systemd/*.service /etc/systemd/system/
systemctl daemon-reload

if [ "$ROLE" = full ] || [ "$ROLE" = blockchain ]; then
  systemctl enable --now hashburst-node
  systemctl is-active --quiet hashburst-node || { journalctl -u hashburst-node -n 40 --no-pager; exit 1; }
fi
if [ "$ROLE" != blockchain ]; then
  systemctl enable --now hashburst-files hashburst-files-panel
  systemctl is-active --quiet hashburst-files || { journalctl -u hashburst-files -n 40 --no-pager; exit 1; }
fi

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
  ufw allow 4011/tcp comment 'HashBurst private IPFS swarm'
  if [ "$PUBLIC_IPFS_MODE" = managed ]; then ufw allow 4001/tcp comment 'IPFS public swarm'; fi
  if [ -n "$AGGREGATOR_IP" ]; then
    ufw delete allow 8091/tcp >/dev/null 2>&1 || true
    ufw allow from "$AGGREGATOR_IP" to any port 8091 proto tcp comment 'HashBurst storage summary'
  elif [ "$STORAGE_ROLE" != edge ]; then
    echo "WARNING: UFW active but no --aggregator-ip supplied; :8091 was not opened."
  fi
else
  echo "WARNING: UFW is not active. HB-Files binds only to localhost by default; publish summary through nginx/TEP or explicitly configure a protected listener."
fi

if [ "$ROLE" != blockchain ]; then
  echo "Private IPFS peer check:"
  IPFS_PATH="$STORAGE_PATH/ipfs-private" ipfs swarm peers 2>/dev/null | head -10 || true
  echo "Local HB-Files health:"
  curl -fsS http://127.0.0.1:8091/health | python3 -m json.tool | head -30
fi

echo "Installation complete. State: /etc/hashburst/install-state.json"
