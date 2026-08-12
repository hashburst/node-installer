# HB-TEP aggregator wiring (Step 7B)

Storage aggregator TEP nodes use a process boundary, not an in-process TEP client.
`aggregator/hb_tep_adapter.py` sends exactly one local request to
`http://127.0.0.1:47778/app/storage-summary` by default. Only the loopback port may
be overridden with `HB_TEP_IPC_PORT` for tests or non-default local packaging; the
host, path, method, and service are fixed in code.

The request body contains exactly the configured stable `node_id` and `peer_id`.
The daemon owns X25519/AES-GCM, peer lookup, direct UDP, relay fallback, replay
protection, and relay selection. The aggregator receives only the validated summary
plus local transport metadata (`direct` or `relay`). Remote summaries cannot forge
that metadata.

The existing aggregator role and accounting rules remain unchanged. In particular,
a TEP-reached edge remains best-effort and never contributes to sellable capacity.
An unavailable/malformed IPC response marks that node offline and does not relax the
primary/committable accounting gates.
