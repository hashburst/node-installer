# Changelog

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
- Replication controller/agent remain opt-in; destructive UNPIN remains double-gated and disabled by default.

## 2.1.4
- Add safe global release lifecycle with durable reference tombstones and request/audit metadata.
- Add object generation fencing so stale PIN/VERIFY/UNPIN jobs cannot mutate newer desired state.
- Add destructive UNPIN as a double-gated operation: controller `HB_REPL_UNPIN_ENABLED=1`, agent `HB_REPL_ALLOW_UNPIN=1`, plus just-in-time per-job authorization.
- Add a 15-minute final-release grace period before physical UNPIN scheduling.
- Add safe over-replica trimming that preserves the configured N total / M committable floor.
- Add startup recovery for authorized-but-unconfirmed UNPIN operations and periodic reconciliation.
- Add failure-domain metadata fields to node state for placement evolution.
- Fix replica-agent packaging by installing `hb_ipfs.py` adjacent to the replication agent, removing the runtime dependency on a fragile `PYTHONPATH` layout.
- Keep controller bind localhost-only by default, mode `observe` by default, and both destructive gates disabled by default.
- Add v2.1.4 lifecycle and release-contract CI coverage while preserving v2.1.2/v2.1.3 functional regressions.

## 2.1.3
- Add native HashBurst replication controller and pull-based replica agent.
- Add N/M replication policy (default N=3 total, M=2 committable).
- Add edge grace handling, repair placement, recursive pin verification, retries, and job leases.
- Add idempotent logical `reference_id` tracking so controller registration/release retries do not inflate refcounts.
- Add opt-in HB-Files replication registration hook; disabled by default during rollout.
- Install replication components and systemd units without automatically enabling them.
- Keep Kubo RPC localhost-only and keep automatic UNPIN disabled.
- Add replication regression tests and CI coverage while preserving all v2.1.2 port/accounting contracts.

## 2.1.2
- Reserve 8093 for mining and make 8094 the storage aggregator default everywhere.
- Set production storage aggregator timeout to 3 seconds.
- Fail safe to `capacity_class: unknown` for offline nodes without explicit class/role.
- Add explicit roles to the storage node example configuration.
- Add CSP-safe public explorer storage-schema patcher and regression tests.

## 2.1.1
- Preserve configured `best-effort` classification for offline edge nodes.

## 2.1.0
- Introduce primary/secondary/edge roles and committable vs best-effort capacity accounting.
