# HB-TEP aggregator wiring (Step 7B)

Storage aggregator TEP nodes use a process boundary, not an in-process TEP client.
`aggregator/hb_tep_adapter.py` sends exactly one local request to
`http://127.0.0.1:47778/app/storage-summary` by default. Only the loopback port may
be overridden with `HB_TEP_IPC_PORT` for tests or non-default local packaging; the
host, path, method, and service are fixed in code.

The request body contains exactly the configured stable TEP routing `node_id` and
`peer_id`. The daemon owns X25519/AES-GCM, peer lookup, direct UDP, relay fallback,
replay protection, and relay selection. The aggregator receives only the validated
summary plus local transport metadata (`direct` or `relay`). Remote summaries cannot
forge that metadata.

TEP routing identity and storage-summary identity are separate contracts:

- `tep_node_id` is the stable identity sent to the local TEP daemon for routing.
- `tep_peer_id` is the stable peer identity used by TEP authentication/routing.
- `summary_node_id` is the expected `node_id` inside the returned storage summary.
- if `summary_node_id` is omitted, it falls back to `tep_node_id` (then `name`) for
  backward compatibility with deployments where routing and storage identities match.

Example for node6, where the production TEP route is `node-6` but the storage service
reports `hb-storage-node6`:

```json
{
  "name": "node-6",
  "transport": "tep",
  "tep_node_id": "node-6",
  "tep_peer_id": "12D3KooW...",
  "summary_node_id": "hb-storage-node6",
  "role": "secondary",
  "capacity_class": "committable"
}
```

The existing aggregator role and accounting rules remain unchanged. In particular,
a TEP-reached edge remains best-effort and never contributes to sellable capacity.
An unavailable/malformed IPC response or a mismatched `summary_node_id` marks that
node offline and does not relax the primary/committable accounting gates.
