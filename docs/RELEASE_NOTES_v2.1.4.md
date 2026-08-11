# HashBurst Node Installer v2.1.4

v2.1.4 advances the native HashBurst replication controller from the v2.1.3 durable-replication foundation to a safe object-release lifecycle.

## Safety defaults

- Controller bind remains `127.0.0.1:8095` by default.
- Controller mode remains `observe` by default.
- Physical UNPIN requires controller `HB_REPL_UNPIN_ENABLED=1`.
- The node agent independently requires `HB_REPL_ALLOW_UNPIN=1`.
- Every destructive UNPIN also requires just-in-time authorization for the exact node, job, lease, CID and object generation immediately before local `pin/rm`.
- Final logical release waits `HB_REPL_DELETE_GRACE_SEC=900` seconds before an UNPIN can be scheduled.
- The installer ships controller/agent units but does not automatically enable them.

## Lifecycle additions

- Durable logical-reference tombstones preserve idempotency after release.
- Final release increments the object generation and marks existing replica desired state false.
- Pending PIN/VERIFY work from older generations becomes stale.
- Re-registration is rejected while an UNPIN has crossed the authorization boundary and its physical outcome is unknown.
- Startup recovery converts an authorized-but-unconfirmed destructive operation into a verification path instead of assuming either success or failure.
- Extra replicas can be trimmed only when the N total target and M committable minimum remain satisfied.

## Packaging fix

`hb_ipfs.py` is installed into `/opt/hashburst/replication/hb_ipfs.py` as well as the HB-Files tree. This makes direct systemd execution of the replica agent self-contained and fixes the missing-module failure discovered during the v2.1.3 production rollout.

## Failure domains

v2.1.4 adds persisted node metadata fields for `failure_domain`, `provider`, `region`, and `rack`. These fields are available for placement evolution and audit. They do not constitute a Byzantine proof-of-storage or a formal geographic durability guarantee.

## Explicit non-claims

v2.1.4 does not implement controller HA/multi-writer consensus, Byzantine proof-of-storage, on-chain discovery, erasure coding, or a proof that independent failure domains are always available. A single active controller remains the supported topology.

## Recommended rollout

1. Upgrade controller code and schema while preserving the existing SQLite database backup.
2. Start the v2.1.4 controller in `observe` with `HB_REPL_UNPIN_ENABLED=0`.
3. Upgrade replica agents with `HB_REPL_ALLOW_UNPIN=0`.
4. Validate continuous heartbeat and Kubo capacity reporting.
5. Onboard the second committable node and validate real M=2 behavior.
6. Move to `pin-only` and test a canary CID before broad PIN scheduling.
7. Only after release/refcount/recovery tests pass in production, explicitly enable both UNPIN gates for a controlled canary release.

Never expose the private Kubo API (`5011`) publicly. Agents must use the local Kubo API only.
