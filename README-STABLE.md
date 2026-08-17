# HashBurst Node Installer — Stable deployment profiles (v2.1.6)

This file keeps the current deployment profiles and operational notes for the
published v2.1.6 release. Historical release-specific regression files remain in
the repository and are intentionally not renamed.

Run the current CI/release checks before changing production. The canonical
installer version is `2.1.6`.

## Deployment profiles for the current network

### 85.233.199.35 - primary storage / master-node

The supplied diagnostic reports `no ZFS` while HB-Files reports `capacity_source=zfs` and the
private repo is under `/datapool/hashburst`. Do not let auto-detection guess during migration.
If `/datapool/hashburst` is the intended mounted storage but `zfs` is not usable:

```bash
sudo ./install.sh --role full --storage-role primary \
  --storage-backend filesystem --storage-path /datapool/hashburst \
  --capacity-gb 5120 --public-ipfs-mode auto
```

If the ZFS dataset is actually visible and healthy, use `--storage-backend zfs --zfs-dataset datapool/hashburst` instead.

### 77.90.188.155 - node-6 secondary storage

```bash
sudo cp swarm.key /tmp/swarm.key
sudo ./install.sh --role storage --storage-role secondary --capacity-gb 400 \
  --swarm-master-ip 85.233.199.35 --swarm-peer-id '<PRIMARY_PRIVATE_IPFS_PEER_ID>' \
  --aggregator-ip 64.31.4.9 --public-ipfs-mode auto
```

For direct aggregator polling on :8091, UFW must already be active; otherwise the installer keeps
HB-Files on localhost. This is intentional fail-closed behavior.

### 77.90.188.157 - n4 blockchain node with existing public IPFS

For the current blockchain-only role:

```bash
sudo ./install.sh --role blockchain --public-ipfs-mode reuse
```

If storage is added later, `reuse` leaves the existing :5001/:4001 daemon untouched and creates only
HashBurst private IPFS :5011/:4011.

### Workstation / physical full-node behind NAT

Use the v2.1.6 full/edge profile for a machine such as node-7:

```bash
sudo cp swarm.key /tmp/swarm.key
sudo ./install.sh \
  --role full \
  --storage-role edge \
  --storage-backend filesystem \
  --storage-path '<LOCAL_STORAGE_PATH>' \
  --capacity-gb '<CAPACITY_GB>' \
  --swarm-master-ip 85.233.199.35 \
  --swarm-peer-id '<PRIMARY_PRIVATE_IPFS_PEER_ID>' \
  --aggregator-ip 64.31.4.9 \
  --node-name '<UNIQUE_NODE_NAME>' \
  --public-ipfs-mode auto
```

`edge` is best-effort capacity: it can contribute replica/presence but does not increase sellable storage.
In v2.1.6, TEP reconciles registered NAT peers from `/api/nodes`, preserves authenticated NAT coordinates,
and fails closed when stable X25519 identity is unavailable.

## Storage aggregator publication contract

- `8093`: mining aggregator only.
- `8094`: storage network aggregator.
- `HB_AGGREGATOR_TIMEOUT=3`: recommended production timeout and supplied unit default.
- Static edge entries must explicitly use `role: "edge"` and `capacity_class: "best-effort"`.
- Offline entries without a valid configured class or role are reported as `unknown`, never implicitly `committable`.

For an existing public explorer, see `integrations/explorer/` for the CSP-safe schema patcher.

## Replication controller rollout

The replication controller and replica agent remain packaged but are not enabled automatically.
The initial safety mode is `observe`; the HB-Files registration hook and automatic UNPIN are disabled by default.

Default policy is N=3 total confirmed copies with M=2 confirmed copies on committable nodes. Edge replicas are best-effort only. See `docs/REPLICATION_CONTROLLER.md` and the historical release notes for the evolution of this contract.

## v2.1.6 release state

The five-node TEP rollout completed successfully across `blockchainapi.one`, `node-6`, `n4`,
`master-node` and `node-7`. See `docs/RELEASE-v2.1.6.md` for the final evidence.
