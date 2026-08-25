#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run as root.' >&2; exit 1; }
[[ $# -eq 3 ]] || { echo "Usage: $0 SEED.tgz master_node_v220.py prepare_dr1_config.py" >&2; exit 2; }
SEED="$1"; MASTER="$2"; CFGHELPER="$3"
for cmd in python3 rsync systemctl ss getent id useradd groupadd; do command -v "$cmd" >/dev/null || { echo "Missing command: $cmd" >&2; exit 1; }; done
for f in "$SEED" "$MASTER" "$CFGHELPER"; do [[ -f "$f" ]] || { echo "Missing $f" >&2; exit 1; }; done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
tar -C "$TMP" -xzf "$SEED"
ST="$TMP/seed"
[[ -d "$ST" ]] || { echo 'Invalid seed archive' >&2; exit 1; }

# The existing segmentation worker owns 10.0.0.2:9000 and must remain untouched.
if ! ss -lntp | grep -q '10\.0\.0\.2:9000'; then
  echo 'Warning: expected neurallity segmentation listener 10.0.0.2:9000 was not observed.' >&2
fi

if ! getent group synapta >/dev/null 2>&1; then groupadd --system synapta; fi
if ! id synapta >/dev/null 2>&1; then
  useradd --system --gid synapta --home-dir /opt/hashburst --shell /usr/sbin/nologin synapta
fi

install -d -m 0755 /opt/hashburst /opt/monero /etc/hashburst /etc/monero-k325t
rsync -a "$ST/opt/monero/0.18.5.1/" /opt/monero/0.18.5.1/
install -m 0644 "$ST/etc/monero-k325t/mainnet.conf" /etc/monero-k325t/mainnet.conf
install -m 0644 "$ST/etc/monero-k325t/testnet.conf" /etc/monero-k325t/testnet.conf
install -m 0644 "$ST/etc/monero-k325t/testnet-mining-address" /etc/monero-k325t/testnet-mining-address
install -o root -g synapta -m 0640 "$ST/etc/hashburst/k325t-pcb-token" /etc/hashburst/k325t-pcb-token

# Install the actual XD675 service units before selecting ownership for payload
# files. This keeps DR1 compatible if the master service identity ever changes.
for svc in hashburst-master.service monero-k325t-mainnet.service monero-k325t-testnet.service; do
  install -m 0644 "$ST/systemd/$svc" "/etc/systemd/system/$svc"
done
systemctl daemon-reload

MASTER_UNIT=/etc/systemd/system/hashburst-master.service
master_user="$(sed -n 's/^User=//p' "$MASTER_UNIT" | head -n1)"
master_group="$(sed -n 's/^Group=//p' "$MASTER_UNIT" | head -n1)"
[[ -n "$master_user" ]] || master_user=root
if [[ "$master_user" != root ]]; then
  if ! id "$master_user" >/dev/null 2>&1; then
    [[ -n "$master_group" ]] || master_group="$master_user"
    getent group "$master_group" >/dev/null 2>&1 || groupadd --system "$master_group"
    useradd --system --gid "$master_group" --home-dir /opt/hashburst --shell /usr/sbin/nologin "$master_user"
  fi
  [[ -n "$master_group" ]] || master_group="$(id -gn "$master_user")"
else
  master_group="${master_group:-root}"
fi

python3 "$CFGHELPER" "$ST/etc/hashburst/config.master.json" "$TMP/config.dr1.json"
if [[ "$master_user" == root ]]; then
  install -o root -g root -m 0600 "$TMP/config.dr1.json" /etc/hashburst/config.json
else
  install -o root -g "$master_group" -m 0640 "$TMP/config.dr1.json" /etc/hashburst/config.json
fi
install -o "$master_user" -g "$master_group" -m 0755 "$MASTER" /opt/hashburst/master_node.py
install -d -o "$master_user" -g "$master_group" -m 0750 /var/log/hashburst
python3 -m py_compile /opt/hashburst/master_node.py

# Create any unprivileged service identity required by the imported Monero units.
for pair in 'monero-k325t-mainnet.service:/etc/monero-k325t/mainnet.conf' 'monero-k325t-testnet.service:/etc/monero-k325t/testnet.conf'; do
  svc="${pair%%:*}"
  cfg="${pair#*:}"
  unit="/etc/systemd/system/$svc"
  u="$(sed -n 's/^User=//p' "$unit" | head -n1)"
  g="$(sed -n 's/^Group=//p' "$unit" | head -n1)"
  [[ -n "$u" ]] || u=root
  if [[ "$u" != root ]]; then
    [[ -n "$g" ]] || g="$u"
    getent group "$g" >/dev/null 2>&1 || groupadd --system "$g"
    id "$u" >/dev/null 2>&1 || useradd --system --gid "$g" --home-dir "/var/lib/$u" --shell /usr/sbin/nologin "$u"
  else
    g="${g:-root}"
  fi
  d="$(sed -n 's/^[[:space:]]*data-dir[[:space:]]*=[[:space:]]*//p' "$cfg" | head -n1)"
  l="$(sed -n 's/^[[:space:]]*log-file[[:space:]]*=[[:space:]]*//p' "$cfg" | head -n1)"
  if [[ -n "$d" ]]; then install -d -o "$u" -g "$g" -m 0750 "$d"; fi
  if [[ -n "$l" ]]; then install -d -o "$u" -g "$g" -m 0750 "$(dirname "$l")"; fi
done

# Primary-only services remain off until HA is armed.
systemctl disable hashburst-master.service >/dev/null 2>&1 || true
systemctl stop hashburst-master.service >/dev/null 2>&1 || true
systemctl disable hashburst-k325t.service >/dev/null 2>&1 || true
systemctl stop hashburst-k325t.service >/dev/null 2>&1 || true

# DR infrastructure daemons are allowed to run while the candidate remains armed=false.
systemctl enable --now monero-k325t-mainnet.service monero-k325t-testnet.service

# Verify that loopback:9000 remains available for the HA-managed master while
# the segmentation worker continues to own only 10.0.0.2:9000.
if ss -lntp | grep -q '127\.0\.0\.1:9000'; then
  echo 'Unexpected listener already owns 127.0.0.1:9000' >&2
  exit 1
fi
ss -lntp | grep -E '10\.0\.0\.2:9000|127\.0\.0\.1:9000' || true

sha256sum /opt/hashburst/master_node.py
systemctl --no-pager --full status monero-k325t-mainnet.service monero-k325t-testnet.service || true

echo 'DR1_SEED_INSTALL_PASS'
echo 'K325T application still needs stage-only installation from the v2.2 K325T repository.'
