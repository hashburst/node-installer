# HashBurst — Node Installer v2.1.6

Self-contained HashBurst installer for blockchain, HB-Files sovereign storage,
private IPFS federation, HB-TEP encrypted transport, replication components and
optional mining participation.

The installer is intended to produce a repeatable node from a published release.
**No local Python patching is part of the installation procedure.**

## v2.1.6 highlights

- canonical HB-TEP-APP/1 daemon and local IPC
- X25519 + AES-256-GCM TEP transport
- automatic, idempotent TEP onboarding for `full` / `blockchain` nodes
- stable blockchain Peer ID bound to TEP identity
- TEP public key inserted before blockchain `NODE_REGISTRATION`
- blockchain-DNS peer discovery with public rendezvous bootstrap
- registered NAT peers reconciled from authoritative `/api/nodes`
- authenticated NAT coordinates preserved across blockchain peer-sync replacement
- heartbeat authentication fails closed when stable X25519 identity is unavailable
- running blockchain nodes are restarted only after TEP/bootstrap environment preparation so systemd reloads `EnvironmentFile`
- NAT/edge relay trust configuration while relay service remains disabled by default
- storage aggregator TEP transport with separate routing and storage-summary identities
- production flat-layout support for `hb_aggregator.py` + `hb_tep_adapter.py`
- durable replication N=3 / M=2 safety inherited from earlier releases
- Kubo private RPC remains localhost-only
- edge storage never contributes committable/sellable capacity
- five-node field rollout validated with all TEP peers online

## Supported installation model

The supported production target is Ubuntu x86_64 with root or sudo access, Python 3,
`curl`, systemd and Kubo/IPFS as installed or managed by this package.

A machine can install all HashBurst software from the public GitHub release, but
joining the **private IPFS federation** still requires the network `swarm.key`.
That key is a secret and is deliberately **not** stored in this public repository.
Existing installations keep `/etc/hashburst/swarm.key`; a new non-primary node
must receive the key through a secure out-of-band channel.

## Quick start — primary full node

```bash
sudo ./install.sh --role full --primary --miner
```

The installer detects ZFS vs filesystem storage, installs the full node and
HB-Files stack, prepares the private IPFS repository, installs TEP, generates or
preserves TEP key material, starts TEP long enough to obtain the X25519 public
key, writes that key into the HashBurst node environment, starts the blockchain
node, obtains its stable blockchain Peer ID, binds TEP to that identity and
verifies `AES-256-GCM` with `app_ready=true`.

## Quick start — full edge/NAT workstation

The edge machine must already have `/etc/hashburst/swarm.key` or receive the
same network key as `/tmp/swarm.key` before installation.

```bash
sudo ./install.sh \
  --role full \
  --storage-role edge \
  --node-name node-7 \
  --swarm-master-ip 85.233.199.35 \
  --swarm-peer-id 12D3KooWRHr6kYKuHqZ2mhyFujJ1DzcrobyE7vKyvT8pabooun3f \
  --aggregator-ip 64.31.4.9 \
  --miner
```

For an edge node, HB-Files remains local and port 8091 is not opened toward the
Internet. Remote storage summary access is expected to use HB-TEP. The TEP
rendezvous bootstrap contains only public identity material (IP, peer ID,
X25519 public key); no private federation secret is embedded in the package.

v2.1.6 additionally reconciles registered nodes from `/api/nodes` when a NAT node
is omitted by `/api/tep/peers`, while preserving an authenticated public NAT
endpoint already observed at runtime.

## What `install.sh` configures

1. validates role, storage backend and private-network prerequisites
2. preserves existing HashBurst admin/panel secrets
3. installs the HashBurst node binary
4. installs private/public Kubo according to the selected role and existing services
5. joins the private IPFS swarm without creating a new key on a non-primary node
6. installs HB-Files and keeps Kubo private RPC on `127.0.0.1:5011`
7. installs replication controller/agent code without automatically enabling them
8. installs the canonical TEP package and hardened systemd unit
9. runs `bin/hb-tep-onboard`
10. requires Python `cryptography` with X25519/AES-GCM
11. preserves existing TEP keys and refuses silent stable-peer-ID replacement
12. for full/blockchain nodes, completes TEP enrollment using the local blockchain Peer ID
13. on reinstall, reloads the blockchain process only after updated TEP/bootstrap environment preparation
14. verifies the relevant services before reporting installation complete

## TEP security defaults

- UDP transport port: `47777`
- status/local application IPC: `127.0.0.1:47778`
- cryptography: X25519 + AES-256-GCM
- APP protocol: `HB-TEP-APP/1`
- exposed application service: `storage.summary`
- relay capability: disabled by default
- trusted rendezvous: explicitly configured by stable peer ID
- application requests require pre-registered peer ID + X25519 public key
- heartbeat authentication requires stable X25519 identity and is fail-closed
- replay protection and authenticated routing are fail-closed
- the local IPC does not accept arbitrary URLs, ports, methods or headers

The nginx/public explorer layer must never proxy `/api/tep/app/*`; application
IPC is localhost-only.

## Storage accounting and aggregator

Port contracts:

- `8091`: node storage summary / HB-Files
- `8093`: mining aggregator — reserved, not used by storage
- `8094`: storage network aggregator
- `8095`: replication controller, localhost-only by default
- `5011`: private Kubo RPC, localhost-only

The storage aggregator supports `transport: direct` and `transport: tep`.
For TEP nodes, `tep_node_id` is the authenticated routing identity and
`summary_node_id` is the expected HB-Files summary identity. They are separate
contracts so an existing storage installation can keep its established summary
node ID without weakening TEP routing authentication.

Primary/secondary nodes are committable. Edge replicas are best-effort and never
increase sellable capacity.

## Durable replication

The replication lifecycle introduced in earlier releases remains part of v2.1.6:

- default desired copies N=3
- minimum committable copies M=2
- edge grace 6 hours
- controller desired-state + pull-based agents
- SQLite state and idempotent leases
- failure-domain-aware placement
- generation fencing
- final-release grace
- safe trim floors
- automatic UNPIN disabled by default

The installer packages replication services but does not automatically enable the
controller or agents. Destructive UNPIN still requires both controller and agent
gates plus job authorization.

## Important identity files

Do not delete these during an update:

- `/etc/hashburst/swarm.key` — private IPFS federation secret
- `/var/lib/hashburst/node_p2p.key` — stable blockchain/libp2p identity
- `/var/lib/hashburst/tep/node_x25519.key` — stable TEP X25519 identity
- `/etc/hashburst/hashburst-tep.env` — TEP runtime configuration

v2.1.6 preserves the existing stable identities during reinstall/upgrade and
treats replacement of an existing `HB_TEP_PEER_ID` as an error rather than
silently changing identity.

## Verification

After installing a full node:

```bash
curl -fsS http://127.0.0.1:8009/api/health | python3 -m json.tool
curl -fsS http://127.0.0.1:47778/ | python3 -m json.tool
systemctl --no-pager --full status hashburst-node hashburst-tep.service
```

For TEP, the release gate expects `crypto_mode: AES-256-GCM`, `app_ready: true`
and a peer set consistent with the registered network.

## CI / release safety

GitHub Actions runs:

- Python and shell syntax checks
- v2.1.2 release regressions
- v2.1.3 replication compatibility
- v2.1.4 lifecycle safety and release contract
- v2.1.5 installer/onboarding compatibility and release contract
- HB-TEP-APP protocol/security/client/relay/NAT/daemon tests
- aggregator TEP and flat-layout tests
- v2.1.6 TEP identity/reconciliation tests
- v2.1.6 installer upgrade/idempotency sandbox
- v2.1.6 release contract

Historical regression names remain intentionally versioned because they protect
backward compatibility; they do not indicate that the current release is older.

v2.1.6 was validated in the field and rolled out across the current five-node
TEP network before publication.

## Release

Current release: **v2.1.6**.

See `docs/RELEASE-v2.1.6.md`, `docs/V2.1.6-RELEASE-CHECKLIST.md` and the GitHub
Release for the final rollout evidence and release state.

## License

MIT — see `LICENSE`.
