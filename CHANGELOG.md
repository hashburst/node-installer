# Changelog

## 2.1.6 - 2026-08-17
- Reload the running blockchain node environment during TEP reinstall/upgrade so updated `TEP_PUBKEY` and bootstrap settings are consumed before renewed node registration.
- Preserve the existing blockchain Peer ID, reward/signing wallet, TEP X25519 identity, private-IPFS swarm identity, admin/panel secrets and relay policy across the reinstall path.
- Distinguish fresh-install and reinstall service lifecycle: fresh nodes use `systemctl enable --now hashburst-node.service`; already-running nodes use a controlled `systemctl restart hashburst-node.service` after identity/environment preparation.
- Extend installer regression coverage to verify both lifecycle branches and identity preservation.
- Enrich missing TEP runtime identity from the authoritative local `/api/nodes` registry without overwriting mutable NAT coordinates learned by authenticated TEP traffic.
- Reconcile registered peers omitted by `/api/tep/peers` from `/api/nodes`, preserving an already-observed public NAT endpoint across periodic peer synchronization.
- Make heartbeat authentication fail closed when stable X25519 identity is unavailable; remove the host-local `node.key` fallback from the v2.1.6 runtime heartbeat path.
- Add distinct heartbeat diagnostics for missing peer identity, X25519 derivation failure and AES-256-GCM authentication failure.
- Keep AES-256-GCM/X25519 primitives, heartbeat wire format, authenticated NAT endpoint refresh, relay policy, HB-Files and blockchain P2P behavior unchanged.
- Field validation on NAT/dynamic-IP `node-7` passed with stable blockchain identity and authenticated rendezvous traffic.
- Completed network rollout on `blockchainapi.one`, `node-6`, `n4`, `master-node` and `node-7`; all observed TEP nodes reached `peers_online=4/4`, with `pkts_dropped=0` on the rendezvous and on each upgraded public peer snapshot.
- Add v2.1.6 identity-enrichment, registered-peer reconciliation, installer upgrade/idempotency and final release-contract CI gates while retaining the v2.1.2-v2.1.5 compatibility suites.

## 2.1.5
- Integrate the production-validated HB-TEP-APP/1 transport into the canonical installer.
- Add `bin/hb-tep-onboard` for idempotent TEP activation and full-node enrollment.
- Require AES-256-GCM/X25519 support for TEP onboarding; no plaintext or HMAC-only APP mode is accepted by the installer gate.
- Preserve existing `/var/lib/hashburst/tep` key material and refuse silent replacement of an existing stable `HB_TEP_PEER_ID`.
- Generate/read the TEP X25519 public key before a full/blockchain node starts, write `TEP_PUBKEY` into the HashBurst node environment, then bind TEP to the stable blockchain Peer ID returned by the local RPC health endpoint.
- Add public rendezvous bootstrap identity for first contact and trusted one-hop NAT relay configuration without embedding private network secrets.
- Refresh registered dynamic/NAT peer coordinates from authenticated heartbeats only after full node identity and registered TEP public-key validation.
- Allow the canonical `blockchainapi.one` infrastructure node to use its own TEP identity as the local rendezvous for loopback storage-summary failover.
- Keep relay capability disabled by default on ordinary nodes; a node only trusts the configured rendezvous for relay delivery.
- Restrict the v2.1.5 local rendezvous path to `storage.summary`; no generic HTTP, shell or administrative tunnel is introduced.
- Keep edge HB-Files summary port 8091 unexposed and use HB-TEP for NAT/edge summary access.
- Integrate the storage aggregator TEP adapter, separate routing `tep_node_id` from storage `summary_node_id`, and support both repository-package and production flat module layouts.
- Production-validate storage aggregator cutover on 64.31.4.9 with node-6 over direct TEP, relay disabled, accounting `ok`, stable PIDs/NRestarts, and unchanged mining/controller listeners.
- Add v2.1.5 onboarding, authenticated NAT runtime and release regression CI while preserving all v2.1.2-v2.1.4 replication/accounting safety contracts.
