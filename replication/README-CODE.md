# HashBurst Replication Controller v0.1 - code phase

This directory contains the first implementation of the approved design.

Implemented in this code phase:

- SQLite desired/observed state store.
- N total / M committable placement.
- deterministic placement with capacity safety margin.
- controller HTTP API using Python stdlib only.
- outbound-polling replica agent.
- local Kubo `pin/add` and `pin/ls` verification.
- edge grace handling.
- fast committable failure handling after its shorter grace.
- retry/backoff for failed jobs.
- periodic pin verification jobs.
- committable vs best-effort replica-byte metrics.
- pin-only enforcement mode.

Safety defaults:

- controller mode defaults to `observe`.
- no UNPIN jobs are scheduled by the controller.
- agent refuses UNPIN unless `HB_REPL_ALLOW_UNPIN=1`.
- Kubo remains on localhost (`127.0.0.1:5011`).
- controller requires an admin token and node authentication.

Not yet integrated automatically into `hb_files.py`:

`hb_replication_client.py` is provided as the integration boundary. The HB-Files
upload/delete hook should only be enabled during rollout after observe-mode state
has been validated. This separation is intentional: this code phase must not
silently change production upload/delete semantics.

Known v0.1 limitations:

- single active controller only.
- no Byzantine proof-of-storage.
- no on-chain node discovery.
- no geographic/failure-domain placement.
- no automatic unpin or trimming.
- no claim of recoverability when every known source is unavailable.
- tests are intentionally not included yet; they are the next approved phase.
