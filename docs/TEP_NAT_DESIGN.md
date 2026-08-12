# TEP NAT Reachability Design

Status: v2.1.5 release candidate.

## Goal

Allow an edge workstation behind NAT or changing public IP to remain reachable for the narrow `storage.summary` application RPC without exposing HB-Files admin surfaces, Kubo RPC, shell access, or a generic TCP/HTTP tunnel.

## Paths

1. Direct authenticated UDP TEP when the current NAT mapping is usable.
2. Trusted rendezvous relay when direct return reachability fails, including symmetric-NAT-like conditions.

The canonical v2.1.5 rendezvous is the infrastructure node `blockchainapi.one` at `64.31.4.9`. Ordinary nodes do not become relays by default.

## Stable identity vs dynamic coordinates

`peer_id + registered TEP public key + node_id` identify the node. Public IP and UDP source port are learned coordinates.

For registered peers, v2.1.5 resolves the stable wire identity before source-IP fallback. After a heartbeat has been cryptographically authenticated, the heartbeat node identity must match the registered node and its advertised TEP public key must match the registered key. Only then may the observed IP and UDP source port replace the previous coordinates and enter the relay table.

Unauthenticated traffic, ambiguous identities, unknown peers and public-key mismatches cannot update an authenticated relay route.

## Local rendezvous path

The storage aggregator and canonical TEP rendezvous run on the same infrastructure host. The loopback-only storage-summary IPC can therefore select the local TEP peer itself as rendezvous after direct target reachability fails.

This local path remains fail-closed and accepts only the existing `storage.summary` application service. It does not create a general proxy or administrative channel.

## Storage invariant

Reachability does not change storage economics. An edge node reached through direct TEP or rendezvous remains `best-effort`; it never increases `capacity_committable_gb` or `free_sellable_gb`.

## Encryption boundary

Direct and relayed hops use authenticated TEP transport. Relay is hop-by-hop encryption through trusted infrastructure; v2.1.5 does not claim inner end-to-end confidentiality from the rendezvous.

## Degradation

Blockchain peer discovery remains primary with static peers as bootstrap/fallback. If identity metadata is absent, ambiguous, stale, lacks a TEP public key, or the authenticated observed route has expired, APP/RELAY fails closed while legacy heartbeat compatibility remains available.
