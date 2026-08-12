# HB-TEP local IPC (Step 7A)

The production TEP daemon keeps its existing loopback status listener on `127.0.0.1:47778` and adds one fixed local RPC endpoint: `POST /app/storage-summary`.

Request JSON contains exactly `node_id` and `peer_id`. Callers cannot provide a URL, path, port, method, headers, service name, command, or arbitrary payload.

The daemon constructs `storage.summary` internally and uses the existing authenticated HB-TEP-APP/1 transport. Direct UDP is attempted first with a 1.2 second budget; configured rendezvous peers may then be tried, with at most two relay attempts and a 3 second total RPC budget. Request body is limited to 8 KiB and response body to 32 KiB.

The IPC server remains loopback-only and uses a threaded HTTP server so a bounded RPC does not block status reads. Rendezvous peer IDs are daemon-local configuration through repeated `--rendezvous-peer` flags or comma-separated `HB_TEP_RENDEZVOUS_PEERS`; they are never selected by an IPC request.
