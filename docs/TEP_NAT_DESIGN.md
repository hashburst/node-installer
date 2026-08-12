# TEP NAT Reachability Design

Status: staging candidate, not deployed.

## Goal

Allow an edge workstation behind NAT or changing public IP to remain reachable for the narrow `storage.summary` application RPC without exposing HB-Files admin surfaces, Kubo RPC, shell access, or a generic TCP/HTTP tunnel.

## Paths

1. Direct authenticated UDP TEP when the current NAT mapping is usable.
2. Trusted rendezvous relay when direct return reachability fails (including symmetric-NAT-like conditions).

The candidate rendezvous set is infrastructure-controlled and may later include `64.31.4.9`, `85.233.199.35`, and `77.90.188.155`. Production enablement is intentionally deferred.

## Stable identity vs dynamic coordinates

`peer_id + registered TEP public key + node_id` identify the node. Public IP and UDP source port are learned coordinates. A new coordinate replaces the old one only after successful cryptographic authentication and full identity validation.

## Storage invariant

Reachability does not change storage economics. An edge node reached through TEP remains `best-effort`; it never increases `capacity_committable_gb` or `free_sellable_gb`.

## Degradation

Blockchain peer discovery remains primary with static peers as bootstrap/fallback. If identity metadata is absent, ambiguous, stale, or lacks a TEP public key, APP/RELAY fails closed while legacy heartbeat compatibility remains available.
