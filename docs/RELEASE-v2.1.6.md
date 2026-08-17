# HashBurst Node Installer v2.1.6

Status: release preparation, not tagged, not merged to main.

## Scope

v2.1.6 is a stabilization release focused on reinstall/upgrade correctness for full/blockchain nodes using HB-TEP behind NAT or dynamic addressing.

The release candidate combines two narrowly-scoped fixes:

- restart an already-running blockchain node after TEP/bootstrap environment preparation so systemd reloads `EnvironmentFile`;
- repair incomplete runtime TEP identity by enriching missing stable `peer_id` and X25519 `pubkey` from the authoritative local `/api/nodes` registry while preserving mutable NAT coordinates.

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

## Field evidence from first node-7 validation

The first controlled `node-7` test validated the lifecycle portion of the release:

- `hashburst-node.service` was already active and received a controlled restart;
- blockchain health returned successfully;
- blockchain Peer ID, TEP node ID, TEP peer ID, TEP public key, reward address, wallet and persistent TEP key remained unchanged;
- UDP heartbeat traffic continued after restart.

The same test also proved that restart/reload alone was insufficient:

- `node-7` remained `peers_online=0/4`;
- every newly received heartbeat was still dropped;
- authentication failures continued for all registered peers.

Follow-up inspection found the decisive inconsistency on `blockchainapi.one`:

- `/api/nodes` contained the correct `node-7` blockchain Peer ID and TEP public key;
- the live TEP peer view contained the current NAT address for `node-7` but had empty `pubkey` and null `peer_id`;
- the other three live peers had complete stable identities and were online.

This means the mutable NAT coordinate path was working while stable identity metadata was incomplete in the TEP runtime view.

## v2.1.6 identity repair

The release-preparation runtime therefore:

- queries the local `/api/nodes` registry only when a TEP peer is missing stable `peer_id` or `pubkey`;
- fills only those stable identity fields and never overwrites the mutable IP/UDP coordinate already learned or supplied by TEP discovery;
- requires a valid 32-byte X25519 public key before heartbeat encryption/decryption;
- fails closed if identity enrichment is unavailable or invalid;
- distinguishes missing identity, X25519 key-derivation failure and AES-GCM authentication failure in the journal;
- keeps APP/relay authentication and transport policy unchanged.

## Regression gates

Required before release:

- complete v2.1.2-v2.1.5 regression suite remains green;
- v2.1.5 installer/onboarding tests remain green;
- v2.1.5 fresh-install/reinstall sandbox remains green;
- v2.1.5 authenticated NAT runtime tests remain green;
- v2.1.6 identity-enrichment tests remain green;
- v2.1.6 preparation contract remains green;
- fresh install uses `enable --now` and not the reinstall restart path;
- reinstall of an active blockchain node uses `restart` after TEP/bootstrap environment preparation;
- blockchain Peer ID remains unchanged;
- TEP node ID and TEP peer ID remain unchanged;
- TEP public key and persistent private key remain unchanged;
- reward address, wallet keystore and wallet password remain unchanged;
- private-IPFS swarm key remains unchanged.

## Final field validation gate

Release is blocked until one final controlled validation is performed after the v2.1.6 runtime candidate is green in CI.

Required success evidence:

- on `blockchainapi.one`, live TEP state for `node-7` contains the same stable `peer_id` and TEP pubkey exposed by `/api/nodes` while retaining the observed NAT address;
- `node-7` reaches at least one authenticated TEP peer;
- received heartbeat packets are no longer discarded one-for-one;
- the rendezvous marks `node-7` online after authenticated traffic;
- no persistent identity, wallet or private key changes occur;
- no new APP/relay regressions appear.

If the final field gate fails, the installer version remains at 2.1.5 and the release stays blocked.

## Version promotion

The canonical installer must remain at `VERSION="2.1.5"` during preparation.

Only after the final `node-7` field gate passes should the release branch:

1. promote the canonical installer to `VERSION="2.1.6"`;
2. update version-specific release regression assertions;
3. regenerate release checksums if required by the packaging workflow;
4. run exact-head CI;
5. review the final diff;
6. merge through a reviewed PR;
7. run main-branch CI;
8. create the `v2.1.6` tag only after all release gates are green.
