# HashBurst Node Installer

[![Ubuntu](https://img.shields.io/badge/Ubuntu-24.04%20LTS-orange)](https://ubuntu.com)
[![Go](https://img.shields.io/badge/Go-1.22-blue)](https://golang.org)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![Network](https://img.shields.io/badge/Chain%20ID-1337-purple)](https://blockchainapi.one/api/hashburst/health)
[![Node 5](https://img.shields.io/badge/Node%205-blockchainapi.one-brightgreen)](https://blockchainapi.one)

One-command installer for a full HashBurst mainnet node on Ubuntu 24.04 LTS.

---

## What is a HashBurst Node (TEP over UDP)

A HashBurst node is a full participant in the HashBurst blockchain mainnet.
Each node validates transactions, mines blocks using APoW + PoH consensus,
maintains a compact local ledger (~700 bytes/block), and communicates with
other nodes both via libp2p P2P and via HB-TEP (a direct UDP encrypted tunnel
that keeps nodes reachable even when Cloudflare or DNS is unavailable).

On first boot, every node publishes a `NODE_REGISTRATION` transaction to the
blockchain, recording its Peer ID, IP address, TEP public key, and RPC endpoint.
This makes the blockchain itself a decentralized DNS — nodes discover each other
without relying on any external infrastructure.

---

## Consensus Architecture

### Proof of History (PoH)

PoH is the cryptographic clock of each node. Rather than waiting for network
consensus to establish block time, each node independently proves that time
has passed by executing a sequential SHA-512 chain:

```
prev_poh -> SHA-512 -> SHA-512 -> ... (400,000 iterations) -> new_poh
```

This sequence is non-parallelizable. It proves that approximately 40ms of CPU
time has elapsed since the previous block, without any coordination with other
nodes. Each block stores only the final PoH value (8 bytes) — the intermediate
chain is discarded, keeping the ledger compact.

### Adaptive Proof of Work (APoW)

APoW is the validation layer on top of PoH. Difficulty = 4 means the block
hash must begin with 4 leading zero hex characters (16 zero bits). On modern
hardware this takes milliseconds, not minutes.

**Result:** blocks produced every 100–500ms, orders of magnitude faster than
Bitcoin (10 minutes) or Ethereum (12 seconds), with a ledger roughly 1000x
smaller than Bitcoin at comparable block counts.

---

## Node Components

| Component | Description | Port |
|-----------|-------------|------|
| HashBurst Node (Go) | Blockchain core: mining, RPC API, P2P, blockchain DNS | 8009 (RPC), 30307 (P2P) |
| HB-TEP v2.1 (Python) | Encrypted UDP tunnel — Cloudflare/DNS independent | 47777 (UDP) |
| Admin Panel (Python) | Local management panel — SSH tunnel only | 8088 (localhost) |
| nginx | Reverse proxy, HTTPS termination, Let's Encrypt | 80, 443 |
| PHP-FPM | clusters.php API endpoint | via nginx |

---

## Persistence Files

All state is stored in `/var/lib/hashburst/`:

| File | Content | Delete safe? |
|------|---------|-------------|
| `blockchain.dat` | All blocks (gob binary, ~700 bytes/block) | Only for full reset |
| `blockchain.idx` | Block index (20 bytes/entry, offset lookup) | Only for full reset |
| `node_p2p.key` | Ed25519 P2P identity (stable Peer ID) | **Never** |
| `node_registered.flag` | NODE_REGISTRATION dedup flag | Only for full reset |
| `tep/peers.json` | TEP peer cache from blockchain DNS | Safe — auto-regenerated |

Configuration: `/etc/hashburst/env` (chmod 600, root-only)

---

## Blockchain DNS (TEP)

Every node registers itself in the blockchain on first boot via a
`NODE_REGISTRATION` transaction:

```json
{
  "node_id":      "domain-tld",
  "peer_id":      "12D3KooW...",
  "multiaddrs":   ["/ip4/SERVER_IP/tcp/30307/p2p/12D3KooW..."],
  "tep_pubkey":   "50506353bd0ac23a...",
  "tep_port":     47777,
  "rpc_endpoint": "https://domain.tld/api/hashburst",
  "external_ip":  "SERVER_IP",
  "chain_id":     1337
}
```

This record is immutable once mined. Nodes use it to:

- Connect to each other via libp2p without hardcoded addresses (`/api/nodes`)
- Establish direct encrypted TEP tunnels using the registered X25519 pubkey (`/api/tep/peers`)
- Bootstrap new nodes automatically via `ConnectFromBlockchainDNS()`

NODE_REGISTRATION uses triple deduplication to guarantee exactly-once semantics:

1. Flag file on disk (`node_registered.flag`) — survives restarts
2. Blockchain DNS check on startup — reads from persisted chain
3. `AddTransactionOnce` in mempool — prevents duplicates within a session

---

## HB-TEP — Transport Encrypted Protocol

HB-TEP (US Patent 11799659B2) is a UDP Layer 3 tunnel between nodes that
operates independently from HTTPS, DNS, and Cloudflare:

```
Packet format:
  Magic(4) | Version(1) | Type(1) | NodeID(16) | Nonce(12) | PayloadLen(2)
  AES-256-GCM encrypted payload
  GCM Auth Tag (16 bytes)
```

Key exchange uses X25519 ECDH — the public key of each node is registered in
the blockchain DNS. Every 10 seconds each node sends a heartbeat to all peers.
Every 60 seconds the node reads `/api/tep/peers` from the local blockchain
node and updates the peer list from on-chain data (`dns_source: "blockchain"`).
If the blockchain node is unavailable, it falls back to the static `peers.json`
(`dns_source: "static"`).

**Why it matters:** if Cloudflare goes down, DNS is poisoned, or the public
HTTPS endpoint is unreachable, nodes continue communicating directly via UDP.

---

## API Endpoints

All endpoints are exposed via nginx at `https://domain.tld/api/hashburst/`:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/hashburst/health` | Node health: status, blockHeight, peerID, chainId |
| GET | `/api/hashburst/status` | Full status: peers, tps, miner address, version |
| GET | `/api/hashburst/blocks` | Last 10 blocks |
| GET | `/api/hashburst/transactions` | All transactions with type annotation |
| GET | `/api/hashburst/nodes` | Blockchain DNS — all registered nodes |
| GET | `/api/hashburst/tep/peers` | TEP peer list (used by HB-TEP module) |
| GET | `/api/hashburst/storage` | Disk storage statistics |
| GET | `/api/tep/` | HB-TEP status (dns_source, peers, crypto_mode) |

---

## Mainnet Nodes

| Node | Domain | IP | P2P Peer ID | P2P Port | TEP Port |
|------|--------|----|-------------|----------|----------|
| Node 5 | blockchainapi.one | 64.31.4.9 | 12D3KooWCiH3B8E84UNsop5epp7vNXfC6oSg2iyB4wjyCm6a84ow | 30307 | 47777 |
| Node 4 | hashburst.io | 77.90.188.157 | QmHashBurstNode4 | 30306 | 47777 |

Bootstrap multiaddrs:

```
/ip4/64.31.4.9/tcp/30307/p2p/12D3KooWCiH3B8E84UNsop5epp7vNXfC6oSg2iyB4wjyCm6a84ow
/ip4/77.90.188.157/tcp/30306/p2p/QmHashBurstNode4
```

Chain ID: **1337**

---

## Prerequisites

- Ubuntu 24.04 LTS (fresh install, root access)
- Domain with DNS A record pointing to the server IP
- Open ports: 22/tcp (SSH), 80/tcp (HTTP), 443/tcp (HTTPS), P2P_PORT/tcp, 47777/udp (TEP)
- Minimum: 4 GB RAM, 2 CPU cores, 20 GB disk
- Recommended: 8 GB RAM, 4 CPU cores, 100 GB disk

---

## Quick Install

### Step 1 — DNS

Configure your DNS records at your domain registrar:

```
A    domain.tld      -> SERVER_IP
A    www.domain.tld  -> SERVER_IP
```

Verify propagation before proceeding:

```bash
dig domain.tld +short
# Must return: SERVER_IP
```

### Step 2 — Upload

```bash
# Option A: upload the zip
scp hashburst-node-package.zip root@SERVER_IP:/tmp/
ssh root@SERVER_IP
cd /tmp && unzip hashburst-node-package.zip && cd node-installer

# Option B: clone from GitHub
ssh root@SERVER_IP
git clone https://github.com/hashburst/node-installer
cd node-installer/hashburst-node-installer
```

### Step 3 — Run

```bash
sudo ./install.sh \
  --domain domain.tld \
  --email  admin@domain.tld \
  --rpc-port 8009 \
  --p2p-port 30307 \
  --reward 0xWALLET_ADDRESS \
  --bootstrap "/ip4/<SERVER_IP>/tcp/30307/p2p/1234567890ABCDEFGHILMN..."
```

`0xWALLET_ADDRESS` must be a valid HashBurst-compatible Ethereum-format address
(`0x` prefix, 40 hex characters) where mining rewards (HBT) will be sent.

### Step 4 — Save TEP Pubkey

After first start, retrieve and persist the TEP public key:

```bash
TEP_PK=$(curl -s http://127.0.0.1:47778/ | jq -r .pubkey)
echo "TEP_PUBKEY=${TEP_PK}" >> /etc/hashburst/env
systemctl restart hashburst-node
```

### Step 5 — Verify

```bash
# Node health
curl https://domain.tld/api/hashburst/health | jq
# Expected:
# {
#   "status": "ok",
#   "blockHeight": 2,
#   "node": "domain-tld",
#   "peerID": "123456ABC...",
#   "chainId": 1337
# }

# Confirm this node is registered in the blockchain DNS
curl https://domain.tld/api/hashburst/nodes | jq '.[].node_id'

# TEP status
curl http://127.0.0.1:47778/ | jq '{dns_source, crypto_mode, peers_online: .stats.peers_online}'

# Storage
curl http://localhost:8009/api/storage | jq
```

---

## Installer Options

```
./install.sh [OPTIONS]

Required:
  --domain DOMAIN        Domain name (e.g. node6.example.com)
  --email  EMAIL         Email for Let's Encrypt certificate

Optional:
  --ip         IP        Public IP (auto-detected from ifconfig.me if omitted)
  --rpc-port   PORT      Node HTTP RPC port (default: 8009)
  --p2p-port   PORT      libp2p TCP port (default: 30307)
  --node-id    ID        Node identifier (default: domain with dots replaced by dashes)
  --reward     ADDRESS   Wallet address for HBT mining rewards
  --bootstrap  ADDRS     Comma-separated libp2p multiaddrs for P2P bootstrap
```

---

## TEP over UDP - Node Installer

1. Updates the system and installs packages: nginx, PHP 8.3-FPM, Go, Python 3, certbot, ufw, fail2ban
2. Configures UFW firewall: allows SSH, HTTP, HTTPS, P2P port TCP, TEP port UDP
3. Creates directories: `/var/lib/hashburst/`, `/var/log/hashburst/`, `/etc/hashburst/`
4. Generates `HB_ADMIN_SECRET` (48 hex chars) and `HB_PANEL_SECRET` (32 hex chars) via `openssl rand` and saves them to `/etc/hashburst/env` (chmod 600)
5. Copies HB-TEP daemon (`hb_tep.py`) and admin panel (`panel.py`)
6. Compiles the HashBurst node from Go source using `go build`
7. Obtains a Let's Encrypt HTTPS certificate via Certbot
8. Writes the nginx HTTPS configuration with proxy rules for `/api/hashburst/`, `/api/tep/`, `/ipfs/`
9. Installs and enables systemd services: `hashburst-node`, `hashburst-tep`, `hashburst-panel`
10. Configures fail2ban with nginx rate-limiting rules

---

## Generating Secrets

Both secrets are generated automatically by the installer.
To regenerate them manually at any time:

**PANEL_SECRET** — protects admin panel access:

```bash
openssl rand -hex 16
# Example output: d5bc402c364b54f136f8648fc66bd313

# Apply:
echo "HB_PANEL_SECRET=$(openssl rand -hex 16)" >> /etc/hashburst/env
systemctl restart hashburst-panel
```

**ADMIN_SECRET** — protects privileged node API access:

```bash
openssl rand -hex 24
# Example output: 8b806342cbfc4ca00eccf5125855b1df3134d1c65e8d465ec25fa7ee1449beb6

# Apply:
echo "HB_ADMIN_SECRET=$(openssl rand -hex 24)" >> /etc/hashburst/env
systemctl restart hashburst-node
```

---

## Configuration

`/etc/hashburst/env` (chmod 600, readable only by root):

```bash
HBT_REWARD_ADDRESS=0xWALLET_ADDRESS  # Wallet for HBT mining rewards
HB_ADMIN_SECRET=...                  # Admin secret (privileged API access)
HB_PANEL_SECRET=...                  # Panel secret (admin panel login)
NODE_ID=domain-tld                   # Unique node identifier
REWARD_ADDRESS=0xWALLET_ADDRESS      # Same as HBT_REWARD_ADDRESS
RPC_PORT=8009                        # HTTP RPC port
P2P_PORT=30307                       # libp2p TCP port
P2P_KEY_PATH=/var/lib/hashburst/node_p2p.key  # Ed25519 identity key path
STORAGE_DIR=/var/lib/hashburst       # Blockchain data directory
EXTERNAL_IP=SERVER_IP                # Public IP (recorded in blockchain DNS)
RPC_ENDPOINT=https://domain.tld/api/hashburst  # Public RPC URL
TEP_PUBKEY=hex...                    # X25519 pubkey for HB-TEP (set after first boot)
BOOTSTRAP_PEERS=...                  # Comma-separated libp2p multiaddrs
```

After any change to this file:

```bash
systemctl restart hashburst-node hashburst-tep hashburst-panel
```

---

## Admin Panel

The admin panel runs exclusively on `127.0.0.1:8088` and is never publicly exposed.
Access requires an SSH tunnel from your local machine:

```bash
ssh -L 8088:127.0.0.1:8088 root@SERVER_IP
```

Then open in browser:

```
http://127.0.0.1:8088/?secret=PANEL_SECRET
```

The panel shows: service status (nginx, php-fpm, hashburst-tep, hashburst-node),
block height, TEP peer count and crypto mode, disk usage, memory, system load,
and links to node logs, health JSON, and token generation.

---

## Mainnet Deploy Checklist

Before considering a node fully operational on the mainnet:

- [ ] DNS A record propagated — `dig domain.tld +short` returns correct IP
- [ ] Ports 80/tcp, 443/tcp, P2P_PORT/tcp, 47777/udp open in hosting panel and UFW
- [ ] `install.sh` completed without errors
- [ ] `curl https://domain.tld/api/hashburst/health | jq` returns `"status": "ok"`
- [ ] TEP pubkey saved: `grep TEP_PUBKEY /etc/hashburst/env` is non-empty
- [ ] NODE_REGISTRATION confirmed: `curl https://domain.tld/api/hashburst/nodes | jq '.[].node_id'` shows this node
- [ ] All services active: `systemctl status hashburst-node hashburst-tep hashburst-panel nginx`
- [ ] Admin panel reachable via SSH tunnel

---

## Reset Procedure (dev/test only)

To wipe the blockchain and re-run NODE_REGISTRATION from scratch:

```bash
systemctl stop hashburst-node
rm -f /var/lib/hashburst/blockchain.dat \
      /var/lib/hashburst/blockchain.idx \
      /var/lib/hashburst/node_registered.flag
# Do NOT delete node_p2p.key
systemctl start hashburst-node
```

**Do NOT delete `node_p2p.key`** — it contains the Ed25519 private key from which
the Peer ID is derived. Deleting it generates a new Peer ID, invalidating the
existing NODE_REGISTRATION in the blockchain DNS and breaking P2P connectivity
with nodes that already know the old Peer ID.

---

## Updating the Node

```bash
# Pull latest Go source
cd /opt/hashburst-blockchain/GO
git pull

# Recompile
go build -o /usr/local/bin/hashburst-node .

# Restart
systemctl restart hashburst-node
```

---

## Useful Commands

```bash
# Service status
systemctl status hashburst-node hashburst-tep hashburst-panel nginx --no-pager

# Live logs
tail -f /var/log/hashburst/node.log
tail -f /var/log/hashburst/tep.log
journalctl -u hashburst-node -f --no-pager

# Node health
curl -s https://domain.tld/api/hashburst/health | jq
curl -s http://localhost:8009/api/status | jq

# Block height
curl -s http://localhost:8009/api/status | jq .blockHeight

# All registered nodes (blockchain DNS)
curl -s http://localhost:8009/api/nodes | jq

# NODE_REGISTRATION transactions only
curl -s http://localhost:8009/api/transactions \
  | jq '.[] | select(.tx_type == "NODE_REGISTRATION")'

# TEP status and peer list
curl -s http://127.0.0.1:47778/ | jq '{dns_source, crypto_mode, peers_online: .stats.peers_online}'

# TEP peers from blockchain
curl -s http://localhost:8009/api/tep/peers | jq

# Storage info
curl -s http://localhost:8009/api/storage | jq

# SSL certificate info
certbot certificates

# Restart all HashBurst services
systemctl restart hashburst-node hashburst-tep hashburst-panel

# SSH host key changed after reinstall (run on local machine)
ssh-keygen -R SERVER_IP
```

---

## TEP Peer Configuration

`/var/lib/hashburst/tep/peers.json` is updated automatically every 60 seconds
from the blockchain DNS. When a new node registers via NODE_REGISTRATION, all
existing nodes will discover it automatically within the next sync cycle
(`dns_source: "blockchain"`).

For immediate manual addition as static fallback:

```json
{
  "peers": [
    {"id": "node4-hashburst-io",  "ip": "77.90.188.157", "port": 47777},
    {"id": "node5-blockchainapi", "ip": "64.31.4.9",     "port": 47777},
    {"id": "your-new-node",       "ip": "NEW_NODE_IP",   "port": 47777}
  ]
}
```

```bash
systemctl restart hashburst-tep
```

---

## Repository Structure

```
hashburst-node-installer/
|-- install.sh                     Master installer script
|-- opt/
|   |-- hashburst-tep/
|   |   `-- hb_tep.py              HB-TEP v2.1 (AES-256-GCM, X25519, blockchain DNS)
|   `-- hashburst-panel/
|       `-- panel.py               Admin panel (English, localhost:8088 only)
|-- etc/
|   |-- nginx/sites-available/     nginx HTTPS config template
|   |-- systemd/system/            systemd units: node, tep, panel
|   `-- hashburst/                 env file template
|-- docs/
|   `-- INSTALL.md                 Detailed step-by-step installation guide
`-- .github/
    `-- workflows/ci.yml           CI: Go build, Python lint (ruff), shellcheck
```

---

## Related Repositories

| Repository | Description |
|-----------|-------------|
| [hashburst/blockchain](https://github.com/hashburst/blockchain) | Go blockchain node source (production mainnet) |
| [hashburst/blockchain-hvm-framework](https://github.com/hashburst/blockchain-hvm-framework) | HBT-20/HBT-721 smart contract VM and DeFi framework |
| [hashburst/HashBurst-Blockchain-WebApp-Core](https://github.com/hashburst/HashBurst-Blockchain-WebApp-Core) | Web application and block explorer |
| [hashburst/HPC-Cryptominer-Open-Source](https://github.com/hashburst/HPC-Cryptominer-Open-Source) | HPC miner for the HashBurst network |

---

## License

MIT
