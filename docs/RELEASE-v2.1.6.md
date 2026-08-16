# HashBurst Node Installer v2.1.6

Status: release preparation, not tagged, not merged to main.

## Scope

v2.1.6 is a stabilization release focused on reinstall/upgrade correctness for full/blockchain nodes using HB-TEP behind NAT or dynamic addressing.

The runtime change under validation is intentionally narrow:

- detect whether `hashburst-node.service` is already active before TEP onboarding;
- write/preserve TEP and blockchain environment data first;
- restart an already-running blockchain node so systemd reloads `EnvironmentFile`;
- keep the fresh-install path on `systemctl enable --now hashburst-node.service`;
- preserve stable blockchain Peer ID, reward/signing wallet, TEP X25519 identity and private-IPFS swarm identity.

## Unchanged contracts

v2.1.6 does not intentionally change:

- AES-256-GCM/X25519 cryptography;
- heartbeat wire format;
- authenticated NAT endpoint refresh;
- relay trust policy;
- `storage.summary` relay scope;
- HB-Files API behavior;
- blockchain P2P transport;
- replication enablement or UNPIN safety gates.

## Regression gates

Required before release:

- complete v2.1.2-v2.1.5 regression suite remains green;
- v2.1.5 installer/onboarding tests remain green;
- v2.1.5 fresh-install/reinstall sandbox remains green;
- v2.1.5 authenticated NAT runtime tests remain green;
- v2.1.6 preparation contract remains green;
- fresh install uses `enable --now` and not the reinstall restart path;
- reinstall of an active blockchain node uses `restart` after TEP/bootstrap environment preparation;
- blockchain Peer ID remains unchanged;
- TEP node ID and TEP peer ID remain unchanged;
- TEP public key and persistent private key remain unchanged;
- reward address, wallet keystore and wallet password remain unchanged;
- private-IPFS swarm key remains unchanged.

## Field validation gate

Release is blocked until the controlled `node-7` test is complete.

Required field evidence:

- `hashburst-node.service` is active before the test and receives a controlled restart;
- blockchain health returns after restart;
- all persistent identifiers and wallet/key fingerprints remain unchanged;
- TEP state is sampled immediately after restart and again after at least 60 seconds;
- `peers_online`, `pkts_recv`, `pkts_dropped` and authentication journal messages are compared;
- rollback remains available from the pre-test backup.

Expected success signal for the current fix:

- at least one registered TEP peer becomes online;
- received packets are not discarded one-for-one;
- repeated `Auth failed` messages materially decrease or disappear.

If `peers_online` remains zero and received packets continue to be dropped one-for-one, the release remains blocked and diagnosis moves to registered TEP public-key propagation/shared-key derivation rather than NAT reachability.

## Version promotion

The canonical installer must remain at `VERSION="2.1.5"` during preparation.

Only after the `node-7` field gate passes should the release branch:

1. promote the canonical installer to `VERSION="2.1.6"`;
2. update version-specific release regression assertions;
3. regenerate release checksums if required by the packaging workflow;
4. run exact-head CI;
5. review the final diff;
6. merge through a reviewed PR;
7. run main-branch CI;
8. create the `v2.1.6` tag only after all release gates are green.
