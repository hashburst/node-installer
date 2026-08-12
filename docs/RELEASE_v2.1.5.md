# HashBurst Node Installer v2.1.5

v2.1.5 promotes the production-validated HB-TEP transport into the canonical
installer and closes the manual wiring gap between a fresh/full HashBurst node,
its blockchain identity and encrypted storage-summary transport.

## Main changes

- canonical HB-TEP-APP/1 package and hardened systemd service
- automatic TEP onboarding helper invoked by `install.sh`
- X25519 + AES-256-GCM required for installer success
- existing TEP key material preserved across updates
- stable `HB_TEP_PEER_ID` replacement refused unless explicitly repaired by an operator
- `TEP_PUBKEY` written before the blockchain node starts and queues registration
- TEP rebound to the stable blockchain Peer ID obtained from local `/api/health`
- public rendezvous bootstrap for initial/NAT contact; no private network secret embedded
- trusted one-hop relay delivery configured while relay capability remains off by default
- edge port 8091 remains unexposed
- storage aggregator supports TEP direct/relay path metadata and separate routing/summary identities
- production flat-layout aggregator import fixed and regression-tested

## Production validation completed before release cut

The storage aggregator path was validated on `64.31.4.9` with node-6:

- real HB-TEP-APP `storage.summary` direct request succeeded
- node-6 receiver counters confirmed authenticated APP request/response
- storage aggregator staging on a temporary localhost port succeeded
- flat `/opt/hashburst-files` import issue was reproduced, fixed and regression-tested
- production `8094` cutover succeeded with rollback protection
- five-request post-cutover soak remained `accounting_status=ok`
- node-6 remained `secondary`, `committable`, `transport=tep`, `transport_path=direct`
- relay remained disabled
- `8093`, `8095` and the TEP daemon PIDs remained unchanged during the storage cutover
- aggregator and TEP journals remained clean

## Safety defaults

- edge copies are best-effort and never increase sellable capacity
- replication controller and agent are still not auto-enabled
- automatic UNPIN is still disabled and double-gated
- private Kubo RPC remains localhost-only
- TEP local APP IPC remains localhost-only
- no admin tunnel or arbitrary HTTP proxy is introduced by TEP
- no `swarm.key`, admin secret or replication token is embedded in the public repository

## Fresh/non-primary node prerequisite

A node may download all software from GitHub, but a non-primary node cannot join
the private IPFS federation without the existing network `swarm.key`. That file
must be supplied out-of-band or already exist under `/etc/hashburst/swarm.key`.
The installer intentionally refuses to generate a different key on a joining node.

## Release gate

Before tagging `v2.1.5`:

1. branch CI must be green at the exact release head;
2. `install.sh --dry-run` contract must report v2.1.5;
3. the release candidate must be exercised on a controlled Ubuntu node/full-edge profile;
4. TEP must report `AES-256-GCM` and `app_ready=true` for a full/blockchain node;
5. no local patching may be needed after installation;
6. main CI must be green after merge.
