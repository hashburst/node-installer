# HashBurst v2.2 Architecture Scope Baseline

Status: architecture baseline frozen on 2026-09-01.

## HashBurst scope

HashBurst v2.2 contains only HashBurst DePIN infrastructure and technologies belonging to the HashBurst branch:

- HB-TEP transport and authenticated application protocol;
- stable node identity based on node_id, libp2p Peer ID and TEP X25519 identity;
- blockchain-backed TEP peer discovery and reconciliation;
- TEP-native HA lease, quorum, election and fencing;
- HashBurst blockchain node services;
- HB-Files / IPFS storage and replication components;
- HashBurst master workload only where it belongs to the TEP/DePIN PoC;
- HashBurst HVM/blockchain integration after independent validation against the current node runtime.

Physical IP addresses are mutable network coordinates and must not be used as the logical identity of the HA primary.

## Explicit exclusions

The following items belong to the separate Neurallity BI3339M project and MUST NOT be included in HashBurst v2.2 runtime, HA readiness, failover payloads, public ingress, TEP application services, tests, release documentation or deployment requirements:

- K325T FPGA PCB and K325T firmware;
- k325t.exchange or any K325T-specific TEP service;
- K325T HTTP/JSON ingress;
- K325T API tokens or K325T secrets;
- K325T Monero mainnet/testnet services and datasets;
- BI3339M AI-1 decision/orchestration logic;
- BI3339M AI-2 mining segmentation logic;
- BI3339M or prospective HashStrike patent claims, implementations or documentation.

Conversely, HB-TEP inventions, protocol claims and TEP-specific HA/routing mechanisms are not part of the BI3339M/HashStrike patent scope unless separately and explicitly decided through a formal patent review.

## HA topology baseline

Voters:

1. blockchainapi.one
2. hashburst-witness-1
3. hashburst-dr1

Quorum: 2 of 3.

Candidates:

- master-node / XD675, priority 10;
- hashburst-dr1, priority 20.

The HA system remains unarmed until candidate parity, readiness, 3/3 voter visibility, election observation, fencing tests, quorum-loss tests and controlled failover validation have all passed.

## Disaster-recovery principle

A full image/ZFS backup of XD675 is an archival/bare-metal recovery artifact, not the payload required to prepare HashBurst DR candidates.

HashBurst DR nodes should receive only the minimum reproducible HashBurst application state required for candidate parity. Large datasets that are unrelated to HashBurst or reconstructable from authoritative peers must not become mandatory candidate-readiness dependencies.

The v2.2 DR seed and readiness checks must therefore be reviewed to remove all K325T, BI3339M and K325T-specific Monero dependencies before field failover testing.

## Release rule

The existing released stable line remains v2.1.6 until this HashBurst-only v2.2 branch passes code review, CI, controlled field deployment and armed failover validation. No v2.2 release or production-ready claim is permitted before those gates pass.
