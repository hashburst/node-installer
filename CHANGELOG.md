# Changelog

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
