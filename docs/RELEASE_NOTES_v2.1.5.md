# HashBurst Node Installer v2.1.5

Release candidate for automatic HB-TEP onboarding, NAT-safe edge reachability and TEP-aware storage aggregation.

## Installer

`install.sh` reports version 2.1.5 and installs the HashBurst node, HB-Files, private IPFS components, replication code and HB-TEP from one repository checkout. No local Python patching is part of the supported installation procedure.

For `full` and `blockchain` roles, `bin/hb-tep-onboard` installs only missing runtime dependencies, preserves existing TEP key material, writes the X25519 public key into the node environment before blockchain registration, obtains the stable blockchain Peer ID from the local RPC endpoint, binds TEP to that identity and requires AES-256-GCM with `app_ready=true`.

The installer never runs an operating-system upgrade and does not use `pip --break-system-packages`.

## NAT and dynamic-IP edge nodes

The supported workstation model is:

```bash
sudo ./install.sh \
  --role full \
  --storage-role edge \
  --node-name node-7 \
  --swarm-master-ip PRIMARY_IP \
  --swarm-peer-id PRIMARY_PRIVATE_IPFS_PEER_ID \
  --aggregator-ip 64.31.4.9 \
  --miner
```

The private IPFS `swarm.key` remains an out-of-band network secret and is never included in the public repository or release.

An edge node does not expose HB-Files port 8091 to the Internet. Remote storage summary access uses HB-TEP.

The v2.1.5 runtime treats `peer_id + registered X25519 public key + node_id` as stable identity. Public IP and UDP source port are mutable coordinates. A registered peer's coordinates are refreshed from a heartbeat only after successful packet authentication, exact node identity validation and registered TEP public-key validation. An unauthenticated packet cannot create or replace a relay route.

Direct authenticated UDP is attempted first. If direct return reachability fails, the local storage-summary client may use the canonical infrastructure rendezvous. The canonical `blockchainapi.one` node can use itself as the loopback rendezvous for this path. Relay remains restricted to `storage.summary`; it is not a shell, generic HTTP proxy or administrative tunnel.

Relay is hop-by-hop TEP encryption. The release does not claim that relay traffic is hidden from the trusted rendezvous.

## Storage accounting

The storage aggregator supports `direct` and `tep` transports. TEP routing identity (`tep_node_id`) is separate from the expected HB-Files summary identity (`summary_node_id`). Identity mismatches fail closed.

Primary and secondary storage nodes can be `committable`. Edge nodes are always `best-effort` and never increase committable or sellable capacity.

Replication keeps the v2.1.4 safety policy: target N=3 total copies, minimum M=2 committable copies, edge grace handling, generation fencing and destructive UNPIN disabled by default behind independent controller/agent gates.

## Network and security contracts

- TEP UDP: 47777
- TEP status/application IPC: 127.0.0.1:47778
- HashBurst RPC: 8009
- HB-Files node service: 8091
- mining aggregator: 8093, unchanged
- storage aggregator: 8094
- replication controller: 127.0.0.1:8095 by default
- private Kubo RPC: 127.0.0.1:5011

The public nginx layer must not expose `/api/tep/app/*`.

## Validation gates

GitHub Actions covers Python and shell syntax, legacy v2.1.2 regressions, v2.1.3 replication compatibility, v2.1.4 lifecycle safety, HB-TEP-APP protocol/security/relay/NAT/daemon tests, TEP-aware aggregator tests, v2.1.5 installer onboarding, authenticated dynamic-NAT routing and the v2.1.5 release contract.

A release tag is created only after the exact candidate commit is green and controlled production rollout gates pass.

## Upgrade behavior

Existing HashBurst secrets, private IPFS federation material, blockchain identity and TEP X25519 identity must be preserved. Replacement of an existing stable `HB_TEP_PEER_ID` is refused rather than performed silently.
