# HashBurst Node Installer v2.1.3 - Replication Foundation

v2.1.3 introduces the native HashBurst replication controller while preserving the production-safe v2.1.2 storage and port contracts.

## Implemented

- Desired-state replication controller with SQLite state.
- Pull-based replica agent suitable for NAT/dynamic-IP edge nodes.
- Default replication policy: N=3 confirmed copies total, with M=2 on committable nodes.
- Edge offline grace window: 6 hours by default.
- Faster committable repair window: 60 seconds by default.
- Placement excludes offline, disabled, unknown-class, duplicate, and insufficient-capacity targets.
- Kubo recursive `pin/add` followed by `pin/ls` confirmation before a replica is counted as pinned.
- Job leases and replay protection for idempotent retry/report handling.
- Idempotent logical reference IDs for shared-CID refcount tracking.
- Optional HB-Files upload registration hook. It is disabled by default and never claims durable replication when controller registration fails.
- Periodic VERIFY jobs for previously confirmed pins.

## Safety defaults

- `HB_REPL_MODE=observe`.
- `HB_REPL_HOOK_ENABLED=0`.
- `HB_REPL_ALLOW_UNPIN=0`.
- Controller bind default: `127.0.0.1:8095`.
- Kubo private RPC remains local-only at `127.0.0.1:5011`.
- Installer copies replication components and units but does not enable either replication service automatically.
- Existing mining/storage aggregator contract remains unchanged: 8093 mining, 8094 storage aggregator, 8091 storage summary.

## Replication semantics

`N=3` is the target number of confirmed replicas. `M=2` requires at least two of those replicas to be on committable nodes. Edge replicas are best-effort and never increase the sellable/guaranteed committable replica count.

An object is not reported healthy merely because assignments exist. Only confirmed recursive pins count. A grace replica is not reported as online-confirmed, although it temporarily suppresses replacement to avoid churn from intermittent edge nodes.

## HB-Files integration

The upload hook is opt-in. When enabled, HB-Files registers the successful local CID with the controller using the file UUID as an idempotent `reference_id`. If registration fails, the upload response remains successful for the local pinned object but explicitly returns replication state `registration-failed`; no durable replication guarantee is claimed.

Automatic release/unpin integration is intentionally deferred. v2.1.3 is pin-first and additive.

## Not provided by v2.1.3

- Byzantine-resistant proof of storage.
- Controller HA or consensus between controllers.
- On-chain discovery.
- Geographic/provider failure-domain guarantees.
- Erasure coding.
- Automatic trimming or automatic unpin.
- Recovery when no source retains the CID blocks.
- Cryptographic protection against a malicious agent lying about local storage.

These are future work and are not implied by the replication health reported by v2.1.3.
