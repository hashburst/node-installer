#!/usr/bin/env bash
##############################################################
# HashBurst Node Installer v2.0
# Ubuntu 24.04 LTS — complete node setup
#
# Usage:
#   ./install.sh \
#     --domain domain.tld \
#     --email  admin@domain.tld \
#     --rpc-port 8009 \
#     --p2p-port 30307 \
#     --reward 0xHB_WALLET_ADDRESS \
#     --bootstrap "/ip4/64.31.4.9/tcp/30307/p2p/12D3KooWCiH3B8E84UNsop5epp7vNXfC6oSg2iyB4wjyCm6a84ow"
#
# Installs:
#   - nginx + PHP-FPM + Let's Encrypt HTTPS
#   - HashBurst Blockchain Node (compiled from source)
#   - HB-TEP v2.1 (Transport Encrypted Protocol, UDP 47777)
#   - HashBurst Admin Panel (localhost:8088, SSH tunnel only)
#   - systemd services for all components
#   - UFW firewall
#
# Persistence files created on first boot:
#   /var/lib/hashburst/blockchain.dat   — blockchain blocks (gob binary)
#   /var/lib/hashburst/blockchain.idx   — block index (20 bytes/entry)
#   /var/lib/hashburst/node_p2p.key     — stable P2P identity (Ed25519)
#   /var/lib/hashburst/node_registered.flag — NODE_REGISTRATION sent flag
#   /etc/hashburst/env                  — node configuration (chmod 600)
##############################################################
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

[[ $EUID -ne 0 ]] && error "Run as root: sudo ./install.sh"

# ── Defaults ──────────────────────────────────────────────
DOMAIN=""
SERVER_IP=$(curl -4 -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
EMAIL=""
RPC_PORT=8009
P2P_PORT=30307
NODE_ID=""
REWARD_ADDRESS="0x0000000000000000000000000000000000000000"
BOOTSTRAP_PEERS=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --domain)    DOMAIN="$2";         shift 2 ;;
    --ip)        SERVER_IP="$2";      shift 2 ;;
    --email)     EMAIL="$2";          shift 2 ;;
    --rpc-port)  RPC_PORT="$2";       shift 2 ;;
    --p2p-port)  P2P_PORT="$2";       shift 2 ;;
    --node-id)   NODE_ID="$2";        shift 2 ;;
    --reward)    REWARD_ADDRESS="$2"; shift 2 ;;
    --bootstrap) BOOTSTRAP_PEERS="$2";shift 2 ;;
    *) warn "Unknown: $1"; shift ;;
  esac
done

[[ -z "$DOMAIN" ]] && error "Required: --domain DOMAIN"
[[ -z "$EMAIL"  ]] && EMAIL="admin@${DOMAIN}"
[[ -z "$NODE_ID" ]] && NODE_ID="${DOMAIN//./-}"

WEBROOT="/var/www/${DOMAIN}/public"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

info "HashBurst Node Installer v2.0"
info "  Domain:    $DOMAIN"
info "  IP:        $SERVER_IP"
info "  Node ID:   $NODE_ID"
info "  RPC port:  $RPC_PORT"
info "  P2P port:  $P2P_PORT"
echo ""

# ── 1. System ─────────────────────────────────────────────
info "[1/12] System update..."
apt-get update -qq && apt-get upgrade -y -qq
ok "System updated"

# ── 2. Packages ───────────────────────────────────────────
info "[2/12] Installing packages..."
apt-get install -y -qq \
    nginx \
    php8.3-fpm php8.3-cli php8.3-curl php8.3-mbstring php8.3-xml php8.3-bcmath \
    certbot python3-certbot-nginx \
    python3 python3-pip golang-go \
    git curl jq unzip ufw fail2ban
pip3 install cryptography --quiet 2>/dev/null || \
    warn "cryptography not installed — TEP uses HMAC fallback"
ok "Packages installed"

# ── 3. Firewall ───────────────────────────────────────────
info "[3/12] Firewall..."
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow 80/tcp   comment 'HTTP'
ufw allow 443/tcp  comment 'HTTPS'
ufw allow ${P2P_PORT}/tcp comment 'HashBurst P2P'
ufw allow 4001/tcp comment 'IPFS Swarm'
ufw allow 47777/udp comment 'HB-TEP'
ufw --force enable
ok "Firewall configured"

# ── 4. Directories ────────────────────────────────────────
info "[4/12] Directories..."
mkdir -p "$WEBROOT/api/v2/nodes" /var/www/certbot
mkdir -p /opt/hashburst-panel /opt/hashburst-tep
mkdir -p /var/lib/hashburst/tep /var/log/hashburst
mkdir -p /etc/hashburst
chown -R www-data:www-data "/var/www/${DOMAIN}"
ok "Directories created"

# ── 5. Secrets ────────────────────────────────────────────
info "[5/12] Generating secrets..."
ADMIN_SECRET=$(openssl rand -hex 24)
PANEL_SECRET=$(openssl rand -hex 16)
cat > /etc/hashburst/env << ENVEOF
HBT_REWARD_ADDRESS=${REWARD_ADDRESS}
REWARD_ADDRESS=${REWARD_ADDRESS}
HB_ADMIN_SECRET=${ADMIN_SECRET}
HB_PANEL_SECRET=${PANEL_SECRET}
NODE_ID=${NODE_ID}
EXTERNAL_IP=${SERVER_IP}
RPC_PORT=${RPC_PORT}
P2P_PORT=${P2P_PORT}
P2P_KEY_PATH=/var/lib/hashburst/node_p2p.key
STORAGE_DIR=/var/lib/hashburst
RPC_ENDPOINT=https://${DOMAIN}/api/hashburst
BOOTSTRAP_PEERS=${BOOTSTRAP_PEERS}
TEP_PUBKEY=
ENVEOF
chmod 600 /etc/hashburst/env
ok "Secrets saved in /etc/hashburst/env"

# ── 6. Application files ──────────────────────────────────
info "[6/12] Copying application files..."
cp "$SCRIPT_DIR/opt/hashburst-tep/hb_tep.py"   /opt/hashburst-tep/hb_tep.py
cp "$SCRIPT_DIR/opt/hashburst-panel/panel.py"   /opt/hashburst-panel/panel.py
chmod 755 /opt/hashburst-tep/hb_tep.py /opt/hashburst-panel/panel.py
ok "Application files copied"

# ── 7. Compile HashBurst Node ─────────────────────────────
info "[7/12] Compiling HashBurst node..."
BLOCKCHAIN_SRC="/opt/hashburst-blockchain/GO"
mkdir -p "$BLOCKCHAIN_SRC"

# Use bundled source (preferred) or clone from GitHub
if [ -d "$SCRIPT_DIR/../blockchain/GO" ]; then
    cp -r "$SCRIPT_DIR/../blockchain/GO/." "$BLOCKCHAIN_SRC/"
    info "Using bundled source"
else
    info "Cloning from GitHub..."
    git clone https://github.com/hashburst/blockchain /tmp/hb-src
    cp -r /tmp/hb-src/GO/. "$BLOCKCHAIN_SRC/"
fi

cd "$BLOCKCHAIN_SRC"
go mod tidy
go build -o /usr/local/bin/hashburst-node .
ok "hashburst-node compiled: $(ls -lh /usr/local/bin/hashburst-node | awk '{print $5}')"

# ── 8. clusters.php ───────────────────────────────────────
info "[8/12] Deploying clusters.php..."
if [ -f "$SCRIPT_DIR/../clusters.php" ]; then
    cp "$SCRIPT_DIR/../clusters.php" "$WEBROOT/api/v2/nodes/clusters.php"
    chown www-data:www-data "$WEBROOT/api/v2/nodes/clusters.php"
    chmod 640 "$WEBROOT/api/v2/nodes/clusters.php"
    ok "clusters.php deployed"
fi

# Add HB_ADMIN_SECRET to PHP-FPM environment
echo "env[HB_ADMIN_SECRET] = ${ADMIN_SECRET}" >> /etc/php/8.3/fpm/pool.d/www.conf
systemctl restart php8.3-fpm

# ── 9. nginx (HTTP only for Certbot) ─────────────────────
info "[9/12] nginx (HTTP first)..."
NGINXCONF="/etc/nginx/sites-available/${DOMAIN}.conf"
cat > "$NGINXCONF" << NGINXEOF
limit_req_zone \$binary_remote_addr zone=api_limit:10m rate=30r/m;
server {
    listen 80; listen [::]:80;
    server_name ${DOMAIN} www.${DOMAIN};
    root ${WEBROOT};
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { try_files \$uri \$uri/ /index.html; }
}
NGINXEOF
ln -sf "$NGINXCONF" "/etc/nginx/sites-enabled/${DOMAIN}.conf"
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
ok "nginx configured (HTTP)"

# ── 10. SSL Certificate ───────────────────────────────────
info "[10/12] SSL certificate..."
if certbot --nginx -d "$DOMAIN" -d "www.$DOMAIN" \
   --agree-tos --email "$EMAIL" --redirect --non-interactive; then
    ok "SSL certificate obtained for $DOMAIN"
else
    warn "Certbot failed — configure DNS first, then run:"
    warn "  certbot --nginx -d $DOMAIN -d www.$DOMAIN --agree-tos --email $EMAIL --redirect"
fi

# ── 11. nginx (full HTTPS config) ────────────────────────
info "[11/12] nginx HTTPS config..."
cat > "$NGINXCONF" << NGINXEOF
limit_req_zone \$binary_remote_addr zone=api_limit:10m rate=30r/m;

server {
    listen 80; listen [::]:80;
    server_name ${DOMAIN} www.${DOMAIN};
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://${DOMAIN}\$request_uri; }
}

server {
    listen 443 ssl http2; listen [::]:443 ssl http2;
    server_name ${DOMAIN} www.${DOMAIN};
    root ${WEBROOT};
    index index.php index.html;

    ssl_certificate     /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:SSL:10m;

    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Access-Control-Allow-Origin "*" always;
    add_header Access-Control-Allow-Methods "GET, POST, OPTIONS" always;
    add_header Access-Control-Allow-Headers "Content-Type, Authorization" always;

    access_log /var/log/nginx/${DOMAIN}.access.log;
    error_log  /var/log/nginx/${DOMAIN}.error.log warn;
    client_max_body_size 20M;

    location / { try_files \$uri \$uri/ /index.html; }

    location ~ \.php$ {
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:/run/php/php8.3-fpm.sock;
        fastcgi_param SCRIPT_FILENAME \$document_root\$fastcgi_script_name;
        include fastcgi_params;
    }

    location /api/hashburst/ {
        limit_req zone=api_limit burst=20 nodelay;
        proxy_pass http://127.0.0.1:${RPC_PORT}/api/;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 30s;
        if (\$request_method = OPTIONS) { return 204; }
    }

    location /ipfs/ {
        proxy_pass http://127.0.0.1:8080/ipfs/;
        proxy_read_timeout 120s;
    }

    location /api/tep/ {
        proxy_pass http://127.0.0.1:47778/;
        proxy_read_timeout 5s;
    }

    location ~* (whitelist_tokens\.jsonl|\.env|\.git) { deny all; return 404; }
    location ~ /\. { deny all; }
}
NGINXEOF
nginx -t && systemctl reload nginx
ok "nginx HTTPS configured"

# ── 12. systemd services ──────────────────────────────────
info "[12/12] systemd services..."
sed "s/NODE_ID_PLACEHOLDER/${NODE_ID}/" \
    "$SCRIPT_DIR/etc/systemd/system/hashburst-tep.service" \
    > /etc/systemd/system/hashburst-tep.service
cp "$SCRIPT_DIR/etc/systemd/system/hashburst-node.service"  /etc/systemd/system/
cp "$SCRIPT_DIR/etc/systemd/system/hashburst-panel.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hashburst-tep hashburst-panel hashburst-node
ok "Services started"

# fail2ban
cat > /etc/fail2ban/jail.d/hashburst.conf << 'F2B'
[nginx-limit-req]
enabled = true; port = http,https
logpath = /var/log/nginx/error.log
maxretry = 20; bantime = 3600
F2B
systemctl restart fail2ban

# ── Summary ───────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════════════"
ok "INSTALLATION COMPLETE — HashBurst Node"
echo "══════════════════════════════════════════════════════"
echo ""
echo "  HTTPS:     https://${DOMAIN}"
echo "  Node API:  https://${DOMAIN}/api/hashburst/health"
echo "  Node DNS:  https://${DOMAIN}/api/hashburst/api/nodes"
echo "  P2P:       ${SERVER_IP}:${P2P_PORT}"
echo "  TEP:       ${SERVER_IP}:47777 (UDP)"
echo ""
echo "  Admin panel (SSH tunnel):"
echo "    ssh -L 8088:127.0.0.1:8088 root@${SERVER_IP}"
echo "    http://127.0.0.1:8088/?secret=${PANEL_SECRET}"
echo ""
warn "REQUIRED AFTER INSTALL:"
warn "1. Set reward wallet: nano /etc/hashburst/env → HBT_REWARD_ADDRESS=0xYOUR_ADDRESS"
warn "2. After first start, update TEP_PUBKEY: curl http://127.0.0.1:47778/ | jq .pubkey"
warn "   echo 'TEP_PUBKEY=<pubkey>' >> /etc/hashburst/env && systemctl restart hashburst-node"
warn "3. Verify: curl https://${DOMAIN}/api/hashburst/health | jq"
echo ""
echo "  Secrets: /etc/hashburst/env (chmod 600)"
