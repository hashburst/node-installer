# HB-TEP-APP/1 Production Rollback Plan

Status: plan only. Do not execute during staging.

## Pre-deploy evidence required

- SHA256 of current production `/opt/hashburst-tep/hb_tep.py`.
- Byte-for-byte backup of that file outside the deployment path.
- Existing service unit captured with `systemctl cat hashburst-tep`.
- Existing `/api/tep/` status JSON captured.
- Existing peer online count and packet counters captured.

Known production baseline source SHA256 at Step 5B:

`1a1e0554c001020c80261b638e57bf5fe072da574967264dd2e0dbef91b61e24`

## Rollout principle

One node at a time. APP remains fail-closed without local `peer_id`; relay remains disabled unless explicitly enabled. Do not change firewall, public UDP port, Kubo RPC exposure, mining aggregator 8093, storage aggregator 8094, or replication controller 8095 as part of the initial TEP code rollout.

## Rollback trigger examples

Rollback immediately if legacy heartbeat peer count regresses materially, packet drop/auth rejection rises unexpectedly, status API fails, daemon crash-loops, APP traffic changes storage accounting, or any unapproved listener appears.

## Rollback procedure (production phase only)

1. Stop only `hashburst-tep.service`.
2. Restore the exact pre-deploy `hb_tep.py` backup.
3. Verify its SHA256 matches the recorded pre-deploy value.
4. Start `hashburst-tep.service`.
5. Verify UDP 47777 listener and local status 47778.
6. Verify legacy peers recover and packet counters advance.
7. Verify `/api/tep/` through nginx.
8. Record rollback evidence and stop further rollout.

No database migration is part of HB-TEP-APP/1, so rollback does not require database reversal.
