# HB-TEP-APP/1 Protocol

Status: staging candidate, not deployed.

## Transport

HB-TEP-APP/1 is carried inside the existing HB-TEP v2 encrypted payload. The legacy wire header remains 36 bytes (`HBT\x02`, version byte, packet type byte, 16-byte wire node-id prefix, 12-byte nonce, 16-bit encrypted payload length).

Frozen packet types for this candidate:

- `0x01` `PKT_HEARTBEAT` (legacy, unchanged)
- `0x20` `PKT_APP_REQUEST`
- `0x21` `PKT_APP_RESPONSE`
- `0x22` `PKT_APP_ERROR`
- `0x23` `PKT_RELAY_REQUEST`
- `0x24` `PKT_RELAY_RESPONSE`

APP/RELAY requires X25519 + AES-256-GCM, a configured local stable `peer_id`, and a pre-registered remote peer with both TEP public key and stable peer-id. The HMAC compatibility fallback is not sufficient for APP/RELAY.

## Identity

The 16-byte wire node-id field is only a lookup hint. Full identity is bound after authenticated decryption using the encrypted application envelope: `node_id + peer_id + registered TEP public key`. Ambiguous 16-byte prefixes fail closed.

Observed IP address and UDP port are transport coordinates, not identity. They may be updated only after a packet is authenticated and identity-bound.

## Application service

The v1 allowlist contains exactly `storage.summary`. Its request payload must be `{}`. Remote callers cannot select URL, host, port, path, HTTP method, headers, command, filesystem path, Kubo API, or admin endpoint.

The server-side handler performs a fixed local `GET http://127.0.0.1:8091/api/public/storage-summary` with bounded timeout and response size.

## Replay

Application requests carry a cryptographically-random request-id, nonce, timestamp, and bounded TTL. Replayed request-ids are rejected by the application replay cache even when the outer TEP packet has a fresh AES-GCM nonce.

## Relay

Relay v1 is trusted infrastructure and uses authenticated hop-by-hop TEP encryption:

`A -> R (relay_req) -> B (relay_req) -> R (relay_res) -> A (relay_res)`.

The original inner APP request remains embedded for target/service correlation. A target accepts relay delivery only from configured trusted rendezvous peers. A rendezvous accepts relay clients only from its explicit allowlist. Nested arbitrary relay chains are not supported.
