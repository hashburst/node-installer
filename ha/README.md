# HashBurst TEP-HA v1

TEP-HA adds a quorum-based, exclusive primary lease to the existing HB-TEP network. It reuses the authenticated HB-TEP-APP/1 path, the stable node identity, the X25519/AES-256-GCM transport and the TEP peer registry. It does not introduce a second public control protocol.

## Components

- `tep/hb_tep_ha_service.py`: authenticated `ha.lease` application service. The TEP envelope source is preserved and forwarded to the local HA agent.
- `tep/hb_tep_runtime_ha.py`: v2.1.6-compatible TEP runtime extension. UDP/47777 and the existing wire format remain unchanged. A loopback-only local HA RPC endpoint is added on TCP/47781.
- `ha/hashburst_ha_agent.py`: voter/candidate/observer process. It maintains term/vote state, performs elections and renewals, computes the cluster view and controls only the configured primary-only services.
- `hashburst-ha-watchdog.service`: independent fencing watchdog. An armed candidate without a valid local lease guard is fenced even if the HA agent crashes or stalls.

## Initial production topology

Voters:

- `blockchainapi.one` (`64.31.4.9`)
- `mlmultiservices.com` (`77.90.188.180`)
- `hashburst-dr1` (new VPS)

Candidates:

- `master-node` / XD675 (`85.233.199.35`), priority 10
- `hashburst-dr1`, priority 20

Quorum is 2 of 3. The primary role is a logical resource; it is not tied to `85.233.199.35`.

## Safety model

A voter persists `term` and `voted_for`. It grants only one candidate in a term. A candidate becomes primary only after receiving a quorum. A primary renews the lease continuously and self-fences if quorum is lost. A node returning with an older term cannot displace the active primary. Automatic failback is intentionally disabled: a recovered XD675 returns as standby while another valid primary exists.

`armed=false` runs the complete consensus and observability path without starting or stopping primary-only services. Production service control must be enabled only after the four-node observation test has passed.

## TEP integration

HA traffic is carried as service `ha.lease` over the existing TEP APP request/response family. The HA runtime preserves the authenticated source identity before forwarding a request to the loopback HA agent. The local agent cannot be reached remotely except through the TEP daemon.

The legacy heartbeat wire format and packet numbers are unchanged.

## Install files only

```bash
sudo ./ha/install-ha.sh --config /path/to/node-ha.json
```

This installs the agent and systemd units but does not start them.

To enable later, after the TEP HA runtime and node-specific configuration have been validated:

```bash
sudo systemctl enable --now hashburst-ha-watchdog.service hashburst-ha-agent.service
```

## Local status

```bash
curl -fsS http://127.0.0.1:47780/v1/status | python3 -m json.tool
```

The status contains the local role, term, lease holder, quorum reachability, eligibility, armed state and last control-loop error. This endpoint is intentionally loopback-only and is the source that the dashboards should proxy.

## Production activation sequence

1. Install the HA-enabled TEP runtime on all voters and candidates, one TEP node at a time.
2. Install TEP-HA with `armed=false` on `blockchainapi.one`, `mlmultiservices.com`, XD675 and DR1.
3. Confirm TEP `app_ready=true`, `ha.lease` advertised, peer identity and public key stability, and 2-of-3 voter quorum.
4. Confirm DR1 replication eligibility and Monero synchronization.
5. Run an observation-mode election/failover test without changing production service state.
6. Configure the two public ingress nodes to route to the lease holder.
7. Set `armed=true` on both candidates and enable the independent watchdog.
8. Perform a controlled production failover test and measure RTO/RPO.

Do not clone TEP private identity files between candidates. Every HA node keeps its own TEP identity; only the logical `HASHBURST_PRIMARY` role moves.
