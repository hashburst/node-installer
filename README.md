# HashBurst — Node Installer

Complete, self-contained installer for a HashBurst node: blockchain +
sovereign storage (HB-Files) on a private IPFS network.

**Works on both ZFS and non-ZFS machines.** The installer auto-detects the
environment and picks the correct IPFS setup, so the `no such pool 'datapool'`
error on non-ZFS hosts cannot happen.

The HB-Files code ships fully patched (authentication, IPFS backend, client-side
encryption, capacity accounting, public summary endpoint, role awareness) — no
patch steps are needed.

## Contents

```
hashburst-node/
  install.sh                     guided installer (auto-detects ZFS)
  bin/hashburst-node             blockchain node binary
  hbfiles/                       complete HB-Files code (all layers applied)
  ipfs-scripts/
    01-install-ipfs-dual-ZFS.sh    dual IPFS on ZFS (datapool)
    01-install-ipfs-dual-noZFS.sh  dual IPFS on a normal filesystem
    02-verify.sh                   IPFS verification
  systemd/                       service units (node, files, panel)
  aggregator/                    network aggregator (for the reference node)
```

## Prerequisites

- Ubuntu x86_64, root access, Python 3, curl
- For a node that joins the network: `/tmp/swarm.key` copied from the primary
- For a storage node: `/tmp/list.json` copied from the primary

## Quick start — storage node (non-ZFS), joins an existing network

```bash
# 1. copy the shared secret and stakeholder list from the primary node
rsync -avz -e ssh /tmp/swarm.key /tmp/list.json root@NEW_NODE:/tmp/

# 2. copy this package and run the installer
rsync -avz -e ssh hashburst-node/ root@NEW_NODE:/tmp/hashburst-node/
ssh root@NEW_NODE
cd /tmp/hashburst-node
sudo ./install.sh \
  --role storage \
  --capacity-gb 400 \
  --swarm-master-ip 85.233.199.35 \
  --swarm-peer-id 12D3KooWRHr6kYKuHqZ2mhyFujJ1DzcrobyE7vKyvT8pabooun3f \
  --aggregator-ip 64.31.4.9 \
  --node-name node-7
```

The installer:
1. installs the node binary and Kubo (IPFS) if missing
2. installs the shared swarm.key (federation)
3. **detects ZFS** and runs the correct dual-IPFS script
4. bootstraps the private IPFS swarm to the primary
5. installs the complete HB-Files code
6. writes the env (logical capacity cap, storage role)
7. starts the systemd services
8. opens port 8091 only toward the aggregator

## Primary node (first node of a network, with ZFS)

```bash
sudo ./install.sh --role full --primary --miner
```

With `--primary` the installer lets the IPFS script generate a new swarm.key
(the network's shared secret — then copied to every other node). With ZFS
present, storage uses `datapool` and the node is `primary` (keeps the sovereign
accounting for the whole network).

## Options

| Option | Meaning |
|---|---|
| `--role storage\|full\|blockchain` | what to install (default: storage) |
| `--capacity-gb N` | logical storage cap (non-ZFS nodes) |
| `--swarm-master-ip IP` | primary node IP (for federation) |
| `--swarm-peer-id ID` | primary private IPFS peer id |
| `--aggregator-ip IP` | aggregator IP (firewall rule for 8091) |
| `--node-name NAME` | node id (default: hb-<hostname>) |
| `--primary` | this is the first node; generate the swarm.key |
| `--miner` | enable mining on this node |

## After installing a storage node (2 manual steps)

1. On the aggregator, add the node to `/etc/hashburst/storage-nodes.json`:
   ```json
   {"name":"node-7","url":"http://NEW_NODE_IP:8091","role":"edge","capacity_class":"best-effort"}
   ```
   The aggregator includes it within 30s, no restart.

2. Federation test: on the primary, add a file to the private IPFS, then on the
   new node `IPFS_PATH=<private-repo> ipfs cat <CID>`. If it returns the file,
   the private network is shared.

## The aggregator (reference node)

The `aggregator/` folder holds the network aggregator that sums every storage
node's capacity and computes the sovereign accounting over the network total.
Install it on the node that serves the public page:

```bash
cp aggregator/hb_aggregator.py aggregator/hb_aggregator_server.py /opt/hashburst-files/
cp aggregator/storage-nodes.example.json /etc/hashburst/storage-nodes.json
cp aggregator/hashburst-aggregator.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now hashburst-aggregator
```

The storage aggregator listens on `127.0.0.1:8094` by default. Port `8093` is reserved for the mining aggregator and must not be used by storage. The supplied systemd unit also sets `HB_AGGREGATOR_TIMEOUT=3`.

For static discovery, configure both `role` and `capacity_class` explicitly. In particular, every edge should use `"role":"edge"` and `"capacity_class":"best-effort"`. If an offline node has neither a valid configured class nor role, v2.1.2 reports its class as `unknown` rather than assuming `committable`.

## Security notes

- Port 8091 exposes admin endpoints (with the admin secret): the installer opens
  it **only** toward the aggregator IP, never to the whole internet.
- IPFS API ports (5001, 5011) stay bound to localhost.
- The swarm.key is a shared secret: identical md5 across all nodes, or they
  form separate private networks and do not federate.
- No identities are shipped in this package: the node generates its own P2P key
  and wallet on first start. Copying identities between nodes is never done.

## Notes

- The capacity cap on non-ZFS nodes is **logical**, not physical: HB-Files uses
  it for accounting, but the disk can fill beyond it. Monitor `df -h`.
- On ZFS nodes the cap is the ZFS quota on `datapool/hashburst`.
- A node with a pre-existing public IPFS (ports 5001/4001 already used) needs a
  dedicated procedure — the dual-IPFS scripts assume those ports are free.

## GitHub publication

v2.1.2 includes CI under `.github/workflows/ci.yml` and a publication checklist in `docs/GITHUB_PUBLICATION.md`. The repository owner should choose the intended software license before public release; no license is inferred by this package.
