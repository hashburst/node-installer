# HashBurst Replication Controller

## Policy

Default policy is `N=3`, `M=2`:

- N: minimum target of confirmed copies across eligible nodes.
- M: minimum confirmed copies that must be on `committable` nodes.
- Edge copies are `best-effort`; they may satisfy the total N target but never the M commercial guarantee.

## Data flow

```text
HB-Files upload
    |
    +-- local Kubo add(pin=true) -> CID
    |
    +-- optional registration hook
            |
            v
      Replication Controller
            |
       desired jobs
            |
            v
      Replica Agent (outbound poll)
            |
            v
      local Kubo 127.0.0.1:5011
            |
        pin/add + pin/ls
            |
            v
      confirmed report
```

The controller never connects to a remote Kubo RPC.

## Edge intermittency

An offline edge replica enters grace for 6 hours by default. It is not counted as an online confirmed replica while offline, but the controller delays replacement until grace expires. Committable replicas use a much shorter default grace because they support the M threshold.

## Rollout

1. Install package; services remain disabled.
2. Start controller in `observe` mode on a protected endpoint.
3. Start agents and observe heartbeat/capacity/pin state.
4. Enable the HB-Files registration hook only after node identities and authentication are verified.
5. Switch controller to `pin-only` to permit additive repair.
6. Validate M committable enforcement before relying on edge replicas.
7. Keep automatic UNPIN disabled throughout v2.1.3 rollout.

Do not expose the controller's plain HTTP listener directly to the public Internet with bearer tokens. Use localhost plus a protected TLS reverse proxy/private transport when remote agents are enabled.

## Known limits

The controller is single-writer in v2.1.3. Agent `pin/ls` reports are operational verification, not Byzantine proof of storage. If all copies of a CID disappear, the controller cannot reconstruct the data. Automatic trimming/unpin and failure-domain-aware placement are deferred to the v2.1.4 hardening path.
