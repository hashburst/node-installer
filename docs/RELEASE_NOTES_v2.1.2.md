# HashBurst Node Installer v2.1.2 — GitHub-ready stable candidate

v2.1.2 is a safety/packaging release based on the production-validated v2.1.1 deployment.

## Fixed

1. **Storage aggregator port safety**
   - systemd unit now uses `HB_AGGREGATOR_PORT=8094`.
   - `hb_aggregator_server.py` now defaults to `8094`.
   - unit sets `HB_AGGREGATOR_TIMEOUT=3` to prevent an unreachable edge node from delaying the public aggregate for the previous 6-second default.
   - `8093` remains reserved for the mining aggregator.

2. **Fail-safe offline classification**
   - online nodes derive capacity class from their validated role.
   - offline nodes use explicit configured `capacity_class`, then configured `role`.
   - an offline/unclassified node now reports `capacity_class: "unknown"`; it never implicitly becomes `committable`.

3. **Static node configuration clarity**
   - example configuration includes explicit `role` and `capacity_class` for primary, secondary and edge nodes.
   - edge nodes should explicitly use `role: "edge"` and `capacity_class: "best-effort"`.

4. **Explorer integration**
   - adds an idempotent CSP-safe patcher for existing public explorers.
   - maps the storage panel to `capacity_committable_gb` and `capacity_best_effort_gb`.

## Port contract

- `8091` — storage node public summary (restricted to aggregator where directly exposed)
- `8093` — mining aggregator
- `8094` — storage network aggregator
- `18094` — optional temporary dry-run port only

## Production acceptance reference

Validated topology values remain:

- committable: 5520 GB
- reserved stakeholders: 3736 GB
- free sellable: 1784 GB
- offline edge contributes 0 GB to best-effort online capacity and never increases sellable capacity
- unreachable edge timeout approximately 3 seconds
