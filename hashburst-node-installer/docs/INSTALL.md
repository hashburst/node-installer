# HashBurst Node — Complete Installation Guide

## Prerequisites

- Ubuntu 24.04 LTS (fresh install)
- Root access
- Domain with DNS A record pointing to the server IP
- Open ports: 22/tcp, 80/tcp, 443/tcp, P2P_PORT/tcp, 47777/udp

## Step 1 — DNS Configuration

Configure your DNS records at your domain registrar:

```
A    domain.tld      -> SERVER_IP
A    www.domain.tld  -> SERVER_IP
```

Verify propagation:

```bash
dig domain.tld +short
# Must return: SERVER_IP
```

## Step 2 — Upload the Package

```bash
# Option A: upload the zip
scp hashburst-node-package.zip root@SERVER_IP:/tmp/
ssh root@SERVER_IP
cd /tmp && unzip hashburst-node-package.zip && cd node-installer

# Option B: clone directly on server
ssh root@SERVER_IP
git clone https://github.com/hashburst/node-installer
cd node-installer
```

## Step 3 — Run the Installer

```bash
sudo ./install.sh \
  --domain domain.tld \
  --email  admin@domain.tld \
  --rpc-port 8009 \
  --p2p-port 30307 \
  --reward 0xWALLET_ADDRESS \
  --bootstrap "/ip4/<SERVER_IP>/tcp/30307/p2p/12D3KooWCiH3B8E84UNsop5epp7vNXfC6oSg2iyB4wjyCm6a84ow,/ip4/77.90.188.157/tcp/30306/p2p/QmHashBurstNode4"
```

`0xWALLET_ADDRESS` must be a valid HashBurst-compatible hexadecimal address
(Ethereum-format `0x` prefix, 40 hex characters), for example:
`0xc4708173f7d276758a08b27821d98D94985dcDD1`

## Step 4 — Post-install: TEP pubkey

After first start, retrieve and save the TEP pubkey:

```bash
TEP_PK=$(curl -s http://127.0.0.1:47778/ | jq -r .pubkey)
echo "TEP_PUBKEY=${TEP_PK}" >> /etc/hashburst/env
systemctl restart hashburst-node
```

## Step 5 — Verify

```bash
# Node health
curl https://domain.tld/api/hashburst/health | jq

# Blockchain DNS — node registered
curl https://domain.tld/api/hashburst/api/nodes | jq

# Storage
curl http://localhost:8009/api/storage | jq

# TEP status
curl http://127.0.0.1:47778/ | jq '{dns_source, crypto_mode, peers_online: .stats.peers_online}'
```

## Persistence Files

| File | Purpose |
|------|---------|
| `/var/lib/hashburst/blockchain.dat` | Blockchain blocks (gob binary) |
| `/var/lib/hashburst/blockchain.idx` | Block index (20 bytes/entry) |
| `/var/lib/hashburst/node_p2p.key` | Stable P2P identity (Ed25519) |
| `/var/lib/hashburst/node_registered.flag` | NODE_REGISTRATION sent flag |
| `/etc/hashburst/env` | Node configuration (chmod 600) |

## Reset Procedure (dev/test only)

```bash
systemctl stop hashburst-node
rm -f /var/lib/hashburst/blockchain.dat \
      /var/lib/hashburst/blockchain.idx \
      /var/lib/hashburst/node_registered.flag
systemctl start hashburst-node
```

**Do NOT delete `node_p2p.key`** — it contains the permanent P2P identity
registered in the blockchain DNS.

## Admin Panel

```bash
# From your local machine (SSH tunnel)
ssh -L 8088:127.0.0.1:8088 root@SERVER_IP
```

Then open in your browser: `http://127.0.0.1:8088/?secret=PANEL_SECRET`

The panel secret is stored in `/etc/hashburst/env`:

```bash
grep HB_PANEL_SECRET /etc/hashburst/env
```

## Generating Secrets

**PANEL_SECRET** — used to access the admin panel. Generated automatically
by the installer. To regenerate manually:

```bash
openssl rand -hex 16
# Example output: d5bc402c364b54f136f8648fc66bd313
echo "HB_PANEL_SECRET=$(openssl rand -hex 16)" >> /etc/hashburst/env
systemctl restart hashburst-panel
```

**ADMIN_SECRET** — used to authenticate privileged API calls to the node.
Generated automatically by the installer. To regenerate manually:

```bash
openssl rand -hex 24
# Example output: 8b806342cbfc4ca00eccf5125855b1df3134d1c65e8d465ec25fa7ee1449beb6
echo "HB_ADMIN_SECRET=$(openssl rand -hex 24)" >> /etc/hashburst/env
systemctl restart hashburst-node
```

Both secrets are stored in `/etc/hashburst/env` (chmod 600, root-only).

## Configuration File

All node configuration is in `/etc/hashburst/env`:

```bash
HBT_REWARD_ADDRESS=0xWALLET_ADDRESS  # Wallet for mining rewards (HBT)
HB_ADMIN_SECRET=...                  # Admin secret (privileged API access)
HB_PANEL_SECRET=...                  # Panel secret (admin panel login)
NODE_ID=domain-tld                   # Unique node identifier
REWARD_ADDRESS=0xWALLET_ADDRESS      # Same as HBT_REWARD_ADDRESS
RPC_PORT=8009                        # HTTP RPC port
P2P_PORT=30307                       # libp2p P2P port
BOOTSTRAP_PEERS=...                  # Comma-separated multiaddrs
```

After editing `/etc/hashburst/env`:

```bash
systemctl restart hashburst-node hashburst-tep hashburst-panel
```

## TEP Peer Configuration

Add peers to `/var/lib/hashburst/tep/peers.json`:

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

## NODE_REGISTRATION — Blockchain DNS

Every node automatically registers itself in the blockchain on first boot:

```json
{
  "node_id": "domain-tld",
  "peer_id": "12D3KooW...",
  "multiaddrs": ["/ip4/SERVER_IP/tcp/30307/p2p/12D3KooW..."],
  "tep_pubkey": "hex...",
  "tep_port": 47777,
  "rpc_endpoint": "https://domain.tld/api/hashburst",
  "external_ip": "SERVER_IP"
}
```

This record is immutable in the blockchain and serves as a decentralized DNS entry.
Nodes discover each other via `/api/nodes` — no external DNS required.

## Useful Commands

```bash
# Logs
tail -f /var/log/hashburst/node.log
tail -f /var/log/hashburst/tep.log

# Services status
systemctl status hashburst-node hashburst-tep hashburst-panel nginx

# Block count
curl -s http://localhost:8009/api/status | jq .blockHeight

# Registered nodes (blockchain DNS)
curl -s http://localhost:8009/api/nodes | jq '.[].node_id'

# TEP peers from blockchain
curl -s http://localhost:8009/api/tep/peers | jq

# SSH host key changed after reinstall (run on local machine)
ssh-keygen -R SERVER_IP
```

## Network Topology

| Node | Domain | IP | RPC | P2P | TEP |
|------|--------|----|-----|-----|-----|
| Node 5 | blockchainapi.one | 64.31.4.9 | 8009 | 30307 | 47777 |
| Node 4 | hashburst.io | 77.90.188.157 | 8007 | 30306 | 47777 |
| New node | domain.tld | SERVER_IP | 8009* | 30307* | 47777 |

*adjust ports to avoid conflicts if multiple nodes share the same IP
