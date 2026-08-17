# HashBurst Node Installer v2.1.6

Status: release candidate, not tagged, not merged to main.

Final field validation: PASS.

## Scope

v2.1.6 is a stabilization release focused on reinstall/upgrade correctness for full/blockchain nodes using HB-TEP behind NAT or dynamic addressing.

The release candidate combines three narrowly-scoped fixes:

- restart an already-running blockchain node after TEP/bootstrap environment preparation so systemd reloads `EnvironmentFile`;
- repair incomplete runtime TEP identity by enriching missing stable `peer_id` and X25519 `pubkey` from the authoritative local `/api/nodes` registry while preserving mutable NAT coordinates;
- reconcile the live TEP peer set with `/api/nodes` so a registered NAT node omitted by `/api/tep/peers` is restored instead of permanently discarded.

The runtime heartbeat path is also hardened to fail closed when stable X25519 identity is unavailable instead of falling back to the host-local `node.key`.

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

The first controlled `node-7` test validated the lifecycle portion of the release:

- `hashburst-node.service` was already active and received a controlled restart;
- blockchain health returned successfully;
- blockchain Peer ID, TEP node ID, TEP peer ID, TEP public key, reward address, wallet and persistent TEP key remained unchanged;
- UDP heartbeat traffic continued after restart.

That first test also proved that restart/reload alone was insufficient: `node-7` remained `peers_online=0/4` and received heartbeats were still dropped one-for-one.

Follow-up inspection found the decisive inconsistency on `blockchainapi.one`: `/api/nodes` contained the correct `node-7` blockchain Peer ID and TEP public key, while `/api/tep/peers` omitted `node-7`. Before the reconciliation fix, dynamic TEP discovery repeatedly recreated `node-7` from its NAT address and the periodic blockchain sync removed it again.

The final controlled field validation used release-candidate runtime head `d74a49180b050121ce30900e9da237f9fe3d8b19` on `blockchainapi.one` and `node-7`.

On `blockchainapi.one` the final state was:

- `peers_total=4` and `peers_online=4`;
- `pkts_dropped=0` while authenticated traffic continued;
- `node-7` remained online at observed NAT address `79.12.5.136:47777`;
- `node-7` retained blockchain Peer ID `12D3KooWCkg4pM31Lzc4ZmAsJMFkW27escTNA9GwTTM8u8Q2T1b6`;
- `node-7` retained TEP X25519 pubkey `4c7c258dd89a4b6e87fbe081077d6ed822d7e29df2760197b6edaa1a5a1ced10`.

On `node-7` after restarting only `hashburst-tep.service`:

- the local TEP peer set remained at four registered peers;
- `blockchainapi.one` authenticated successfully and remained online;
- over the measured window `pkts_recv` increased from 39 to 83 while `pkts_dropped` increased from 28 to 60, so received traffic was no longer discarded one-for-one;
- `peers_online` remained at least 1;
- `hashburst-node.service` remained active and blockchain `/api/health` returned `status=ok`, `chainId=1337`, and the unchanged blockchain Peer ID.

The remaining AES-GCM failures from `node-6`, `n4`, and `master-node` are consistent with those peers still running the pre-v2.1.6 runtime. The upgraded rendezvous interoperates successfully with upgraded `node-7`; network-wide rollout should update the remaining peers to the same release before expecting all four peer relationships to authenticate symmetrically.

## v2.1.6 identity repair

The runtime:

- queries the local `/api/nodes` registry when a TEP peer is missing stable `peer_id` or `pubkey`;
- fills only stable identity fields and does not overwrite mutable IP/UDP coordinates learned from authenticated traffic;
- reconciles the runtime peer set against registered blockchain nodes when `/api/tep/peers` omits a registered NAT node;
- preserves a previously observed NAT endpoint when restoring that registered peer;
- requires a valid 32-byte X25519 public key before heartbeat encryption/decryption;
- fails closed if identity enrichment is unavailable or invalid;
- distinguishes missing identity, X25519 key-derivation failure and AES-GCM authentication failure in the journal;
- keeps APP/relay authentication and transport policy unchanged.

## Release gates

The release candidate requires:

- complete v2.1.2-v2.1.5 compatibility regressions green;
- v2.1.5 installer/onboarding behavior retained;
- fresh-install/reinstall identity preservation retained;
- v2.1.5 authenticated NAT runtime behavior retained;
- v2.1.6 identity-enrichment and registered-peer reconciliation tests green;
- v2.1.6 installer upgrade/idempotency sandbox green;
- final v2.1.6 release contract green;
- exact-head CI green after canonical version promotion.

## Deployment note

Because only `blockchainapi.one` and `node-7` were upgraded during the final field test, `node-6`, `n4`, and `master-node` may continue producing AES-GCM heartbeat failures toward `node-7` until they receive the v2.1.6 runtime. This is a rollout/version-skew condition, not a failure of the final node-7 gate: authenticated traffic between the upgraded rendezvous and upgraded NAT node is established and stable.

## Version promotion

The field gate has passed and the canonical installer is promoted to `VERSION="2.1.6"` on the release branch.

Before tag creation:

1. run exact-head CI;
2. review the final diff;
3. merge PR #9 through review;
4. retarget/reconcile the release PR onto `main` and verify its diff;
5. merge the reviewed release PR;
6. require green main-branch CI;
7. create the `v2.1.6` tag only from the verified main commit.
