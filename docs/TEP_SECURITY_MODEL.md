# HB-TEP-APP/1 Security Model

Status: staging candidate, not deployed.

## Trust boundaries

- Blockchain/local peer registry: authoritative identity metadata.
- TEP X25519/AES-GCM session: authenticated transport hop.
- Rendezvous relay: trusted infrastructure in v1.
- HB-Files public storage-summary endpoint: fixed local service only.

## Threats and controls

- Peer impersonation: registered public key + full encrypted `node_id/peer_id` binding.
- Dynamic-IP spoofing: observed endpoint updates only after authenticated APP/RELAY packets.
- Replay: bounded request-id replay cache plus timestamp/TTL validation.
- SSRF/admin tunnelling: one service allowlist; empty payload; server-controlled loopback URL/path/method/headers.
- Generic tunnelling: no `http.proxy`, TCP proxy, shell, arbitrary path, or arbitrary port service exists.
- Relay abuse: explicit relay-client allowlist, registered target requirement, service allowlist, bounded pending work, one trusted relay hop.
- Oversized messages: application request/response and TEP framing limits are validated.
- Ambiguous 16-byte legacy node-id prefix: fail closed for APP/RELAY.
- HMAC fallback downgrade: APP/RELAY requires X25519/AES-GCM and is disabled otherwise.

## Not claimed

This candidate does not provide Byzantine proof-of-storage, controller HA, end-to-end encryption opaque to the trusted rendezvous, or a proof that every symmetric NAT is traversable without a reachable relay.
