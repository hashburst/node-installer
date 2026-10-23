# Mining distribution and full-node integration

Inspection date: 23 October 2026. No production workload or service was changed.
This document separates inspected functionality from proposed integration.

## Sources and inspection boundary

- https://hashburst.io/nodes/repository/ and linked subdirectories
- https://blockchainapi.one/hashburst/mining.php
- https://blockchainapi.one/api/hashburst/mining/summary
- Owner-supplied RainbowMiner workers API, without retaining its raw records
- hashburst/blockchain, hashburst/node-installer and
  hashburst/HashBurst-Blockchain-WebApp-Core

The directory crawl visited 259 directory URLs and discovered 299 file URLs.
Of 221 text candidates, 206 were fetched; 15 file requests and 7 directory
requests failed. Unlinked files and every bundled binary were not audited.
The fbc 3.2.5 ZIP central directory was inspected separately. These counts are an
inspection snapshot, not a complete security or license certification.

## Existing orchestration

There is an orchestration foundation. Machine-family configurations cover fbc,
mcm, mcmsummus, smcm, tow and tow2, including AVX2/SSE2 miner layouts.
JSON configuration includes miner_commands, gpu_miners and miner_layout.
Documented routines refresh configuration every three hours, remove logs every
four days and restart miners every seven days. The distribution includes
RainbowMiner setup, CPU miners and process management scripts.

This is centrally distributed configuration and scheduled process control.
It does not establish authenticated, acknowledged, reversible per-node policy
execution or an independently validated AI optimizer. The HA master also contains
worker registration and Stratum job handling; those paths are distinct from the
pool-profile optimizer added here. Do not enable nonce segmentation as part of
this integration.

Inspected installation wrappers download mutable latest pointers and execute
installers without artifact signature/checksum verification. Family installers
can overwrite shell setup, configure autologin, select multi-user.target and
install system packages. They must not be reused unchanged on personal laptops,
shared desktops or HPC hosts. The free-installer latest pointer was 2.0.1 while a
2.0.2 directory existed. Several current download paths failed during inspection.

Public configuration responses contained populated NiceHash API-key and secret
fields. Their validity and scope were not tested. No values are reproduced here.
The operator must identify dependent clients, replace central public credentials
with scoped local credentials, rotate the exposed credentials and verify that
new public configurations contain no secrets. Removing a public file alone does
not revoke a credential already downloaded.

## Repository and release comparison

| Component | Inspected state | Consequence |
| --- | --- | --- |
| blockchain | RC2 prerelease with platform assets; Windows full-node work still draft | Asset presence does not prove production runtime acceptance |
| node-installer | v2.1.6 had no attached packages; v2.1.5 had tarball/checksum | A reproducible release job and installation acceptance are needed |
| node-installer --miner | Writes MINER_ENABLED but does not install this pool orchestrator | Do not advertise complete mining enrollment |
| WebApp Core | Wallet UI and federation modules; removed Supabase imports break typecheck | Useful base, not a fully accepted local dashboard |
| WebApp wallet import | Used SHA-256 of private-key text instead of secp256k1/Keccak | Corrected separately with known-vector and encryption tests |

The existing installer bundles a Linux x86_64 executable and assumes apt/systemd.
A platform guard now rejects incompatible hosts before modifications. This guard
prevents a wrong installation; it does not add support for another platform.

## Accelerator delivered in this change

`tools/accelerator` is a Python standard-library collector and empirical policy
model. It collects measured workload profiles, excludes unhealthy/stale samples,
pseudonymizes worker identity and deduplicates source timestamps. It compares
estimated BTC/day after electricity cost for the same worker and hardware.
No raw worker names, IPs, credentials or console output are retained.
Pool summary totals are not allocated to individual workers.

A live sample contained 163 workers: 140 usable records and 23 excluded records
(8 health alerts, 10 missing power readings, 5 offline/paused/stale). All 140
proposals remained on hold because actual energy cost and a fresh BTC conversion
price were absent. This is an input-validation result, not evidence of increased
profitability. No worker was switched.

The model is an interpretable statistical baseline, not an LLM or a trained
production inference model. Configurators can become its downstream output via
a schema-validated adapter. The current output is an unsigned recommendation;
`apply_supported` is false. There is no command dispatcher.

Before actuation: bind telemetry to authenticated node IDs; preserve payout and
pool credentials locally; sign target-bound, expiring, monotonic configurations;
require local resource consent; use atomic replacement, acknowledgement, canary
execution and rollback. Evaluate net settled revenue, rejected/stale shares,
energy, temperature and interruption time against a fixed-profile baseline.
Do not mix different algorithms' hashrates or attribute HBT rewards to pool work.

Patent scope and conformity are not established by this audit. Keep the pool
accelerator separate from blockchain consensus and unrelated inference research.

## Unified node architecture and acceptance

| Target | Required implementation and acceptance |
| --- | --- |
| Debian/Ubuntu x86_64 | Separate HVM/TEP/storage services; signed manifests; install, upgrade and rollback tests |
| Raspberry Pi OS/Linux ARM64 | Architecture-specific binaries, resource limits, disk/replay tests; no implicit ARMv7 support |
| macOS Intel/Apple Silicon | Native runtime/service integration, keychain/custody decisions, restart and storage tests |
| Windows x86_64 | Finish native full-node portability, ACLs, durable ledger and service tests; wallet/auditor alone is insufficient |
| GPU rigs | Driver and miner capability inventory, operator-approved pools and power/resource limits |
| FPGA/ASIC/PCB/HPC | Vendor-specific adapters and firmware/job interfaces; no generic bitstream or universal installer claim |

Use one versioned installation manifest with optional components, rather than
one binary assumed compatible with every device. Enrollment should create local
keys, export public registrations and obtain revocable API credentials through
an authenticated service. A wallet address or API key is not proof of TEP
membership or validator eligibility. Full nodes need not be validators.

Stakeholder enrollment requires evidence of the project's full-node, TEP and
CPU/storage contribution rules. IPFS/storage enrollment needs explicit path,
quota, retention and bandwidth consent. Merely starting IPFS does not establish
federated cloud accounting or availability. Asset, utility and carbon-credit
token allocations need approved contracts, allocation and eligibility rules;
no entitlement is minted by this package.

The faucet remains contingent on accepted mainnet and funded custody: approved
pilot budget 10,000 HBT, first funding 1,000 HBT, 1 HBT/address per seven days,
100 HBT global/day. Enforce these limits server-side with replay protection and
atomic accounting. Do not distribute the founder key through an installer.

## Release gates

1. Fix dependency/typecheck gaps and connect the local UI to a versioned local
   agent API with authentication, origin checks and explicit user consent.
2. Implement the signed configuration adapter and test on disposable workers,
   then one consenting canary; preserve existing pool accounts and workloads.
3. Build each supported platform from a fixed accepted commit; attach checksums,
   provenance, license notices/SBOM and installation/upgrade/recovery evidence.
4. Keep legacy archive, HVM testnet and mainnet identities and ledgers separate.
5. Mainnet activation and founder-signed funding remain pending. A new source
   package or a successful optimizer test does not remove that dependency.

The current deliverable is a tested observation package and installer guard,
not the definitive full-node release. No mainnet, faucet or token was activated.

