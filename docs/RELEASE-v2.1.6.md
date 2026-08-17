# HashBurst Node Installer v2.1.6

Status: release ready after completed field rollout; merge/tag gates remain.

Final field validation: PASS.
Network rollout validation: PASS.

## Scope

v2.1.6 is a stabilization release focused on reinstall/upgrade correctness for full/blockchain nodes using HB-TEP behind NAT or dynamic addressing.

The release combines three narrowly-scoped fixes:

- restart an already-running blockchain node after TEP/bootstrap environment preparation so systemd reloads `EnvironmentFile`;
- repair incomplete runtime TEP identity by enriching missing stable `peer_id` and X25519 `pubkey` from the authoritative local `/api/nodes` registry while preserving mutable NAT coordinates;
- reconcile the live TEP peer set with `/api/nodes` so a registered NAT node omitted by `/api/tep/peers` is restored instead of permanently discarded.

The runtime heartbeat path is hardened to fail closed when stable X25519 identity is unavailable instead of falling back to the host-local `node.key`.

## Unchanged contracts

v2.1.6 does not intentionally change:

- AES-256-GCM/X25519 cryptographic primitives;
- heartbeat wire format;
- authenticated NAT endpoint refresh semantics;
- relay trust policy;
- `storage.summary` relay scope;
- HB-Files API behavior;
- blockchain P2P transport;
- replication enablement or UNPIN safety gates.

## Field validation evidence

The first controlled `node-7` lifecycle test validated the reinstall path: `hashburst-node.service` was already active and received a controlled restart after environment preparation; blockchain health returned; blockchain Peer ID, TEP node ID, TEP peer ID, TEP public key, reward address, wallet and persistent TEP key remained unchanged.

That first test also proved restart/reload alone was insufficient: `node-7` remained `peers_online=0/4` and received heartbeats were still dropped one-for-one.

Follow-up inspection identified the decisive runtime inconsistency on `blockchainapi.one`: `/api/nodes` contained the correct `node-7` blockchain Peer ID and TEP public key while `/api/tep/peers` omitted `node-7`. Before reconciliation, dynamic TEP discovery recreated `node-7` from its NAT address and the periodic blockchain peer sync removed it again.

The reconciliation candidate restored the registered peer from `/api/nodes`, preserved the authenticated public NAT endpoint `79.12.5.136:47777`, and maintained the stable node-7 identities:

- blockchain Peer ID `12D3KooWCkg4pM31Lzc4ZmAsJMFkW27escTNA9GwTTM8u8Q2T1b6`;
- TEP X25519 pubkey `4c7c258dd89a4b6e87fbe081077d6ed822d7e29df2760197b6edaa1a5a1ced10`.

On the rendezvous, the validated state reached `peers_total=4`, `peers_online=4`, and `pkts_dropped=0` while authenticated traffic continued. On node-7, the receive/drop pattern stopped being one-for-one, blockchain health remained `status=ok`, and the blockchain Peer ID remained unchanged.

## Completed network rollout

The release runtime was then rolled out one peer at a time without restarting `hashburst-node.service`:

- `blockchainapi.one` (`64.31.4.9`): 4/4 online, dropped=0;
- `node-6` (`77.90.188.155`): 4/4 online, dropped=0;
- `n4` (`77.90.188.157`): 4/4 online, dropped=0;
- `master-node` (`85.233.199.35`): 4/4 online, dropped=0;
- `node-7` (observed NAT `79.12.5.136`): progressed from 1/4 to 2/4, 3/4 and finally 4/4 as the remaining peers were upgraded.

The final node-7 status showed `blockchainapi.one`, `node-6`, `n4`, and `master-node` all online with their registered stable Peer IDs and TEP public keys. No rollback was required.

## v2.1.6 runtime behavior

The runtime:

- queries the local `/api/nodes` registry when a TEP peer is missing stable `peer_id` or `pubkey`;
- fills only stable identity fields and does not overwrite mutable IP/UDP coordinates learned from authenticated traffic;
- reconciles the runtime peer set against registered blockchain nodes when `/api/tep/peers` omits a registered NAT node;
- preserves a previously observed NAT endpoint when restoring that peer;
- requires a valid 32-byte X25519 public key before heartbeat encryption/decryption;
- fails closed if identity enrichment is unavailable or invalid;
- distinguishes missing identity, X25519 key-derivation failure and AES-GCM authentication failure in the journal;
- keeps APP/relay authentication and transport policy unchanged.

## Release gates

Completed before merge:

- v2.1.2-v2.1.5 compatibility regressions green;
- v2.1.5 installer/onboarding behavior retained;
- fresh-install/reinstall identity preservation retained;
- authenticated NAT runtime behavior retained;
- v2.1.6 identity-enrichment and registered-peer reconciliation tests green;
- v2.1.6 installer upgrade/idempotency sandbox green;
- final v2.1.6 release contract green;
- exact-head CI green after canonical version promotion;
- field validation and five-node rollout green.

## Version promotion and release

The canonical installer is `VERSION="2.1.6"` on the release branch.

Release sequence:

1. final review PR #9 and PR #10;
2. merge PR #9 to `main`;
3. retarget PR #10 to `main` and verify exact diff/CI;
4. merge PR #10;
5. require green main-branch CI;
6. create tag `v2.1.6` from the verified main commit;
7. publish the GitHub release/archive with these notes.
