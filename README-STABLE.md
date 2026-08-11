# HashBurst Node Installer v2.1.3

Run tests first:

```bash
python3 tests/test_release.py
python3 tests/test_multipart_bytes.py
python3 -W error::ResourceWarning -m unittest -v \
  tests.test_replication_policy tests.test_replication_repair \
  tests.test_replica_agent tests.test_replication_hook
python3 tests/v2.1.3_release_regression.py
```

## Deployment profiles for the current network

### 85.233.199.35 - primary storage

The supplied diagnostic reports `no ZFS` while HB-Files reports `capacity_source=zfs` and the
private repo is under `/datapool/hashburst`. Do not let auto-detection guess during migration.
If `/datapool/hashburst` is the intended mounted storage but `zfs` is not usable:

```bash
sudo ./install.sh --role full --storage-role primary \
  --storage-backend filesystem --storage-path /datapool/hashburst \
  --capacity-gb 5120 --public-ipfs-mode auto
```

If the ZFS dataset is actually visible and healthy, use `--storage-backend zfs --zfs-dataset datapool/hashburst` instead.

### 77.90.188.155 - secondary storage

```bash
sudo cp swarm.key /tmp/swarm.key
sudo ./install.sh --role storage --storage-role secondary --capacity-gb 400 \
  --swarm-master-ip 85.233.199.35 --swarm-peer-id '<PRIMARY_PRIVATE_IPFS_PEER_ID>' \
  --aggregator-ip 64.31.4.9 --public-ipfs-mode auto
```

For direct aggregator polling on :8091, UFW must already be active; otherwise the installer keeps
HB-Files on localhost. This is intentional fail-closed behavior.

### 77.90.188.157 - blockchain node with existing public IPFS

For the current blockchain-only role:

```bash
sudo ./install.sh --role blockchain --public-ipfs-mode reuse
```

If storage is added later, `reuse` leaves the existing :5001/:4001 daemon untouched and creates only
HashBurst private IPFS :5011/:4011.

### Workstation / Raspberry / NAT node

```bash
sudo cp swarm.key /tmp/swarm.key
sudo ./install.sh --role edge --storage-role edge --capacity-gb 100 \
  --swarm-master-ip 85.233.199.35 --swarm-peer-id '<PRIMARY_PRIVATE_IPFS_PEER_ID>' \
  --public-ipfs-mode disabled
```

`edge` is best-effort capacity: it can contribute replica/presence but does not increase sellable storage.
A future TEP-aware aggregator/discovery path can remove the need for inbound HTTP reachability.

## Storage aggregator publication contract

- `8093`: mining aggregator only.
- `8094`: storage network aggregator.
- `HB_AGGREGATOR_TIMEOUT=3`: recommended production timeout and supplied unit default.
- Static edge entries must explicitly use `role: "edge"` and `capacity_class: "best-effort"`.
- Offline entries without a valid configured class or role are reported as `unknown`, never implicitly `committable`.

For an existing public explorer, see `integrations/explorer/` for the CSP-safe schema patcher.
## Replication controller rollout

v2.1.3 ships the replication controller and replica agent but does not enable either service automatically. The initial safety mode is `observe`; the HB-Files registration hook and automatic UNPIN are disabled by default.

Default policy is N=3 total confirmed copies with M=2 confirmed copies on committable nodes. Edge replicas are best-effort only. See `docs/REPLICATION_CONTROLLER.md` and `docs/RELEASE_NOTES_v2.1.3.md`.
