#!/usr/bin/env python3
"""HashBurst Replication Controller v0.1.

IMPLEMENTED:
- stdlib-only HTTP API and SQLite state.
- N total / M committable desired-state placement.
- agent-pull job delivery.
- edge grace and faster committable repair.
- pin-only safety mode; UNPIN is not scheduled by this version.
- accounting metrics separate committable vs best-effort replica bytes.

NOT IMPLEMENTED / NO CLAIM:
- Byzantine proof-of-storage.
- controller HA / multi-writer consensus.
- on-chain discovery.
- automatic UNPIN/trimming.
- geographic/failure-domain diversity.

The controller never talks to remote Kubo RPC. Node agents talk only to their
local Kubo API, normally http://127.0.0.1:5011.
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

try:
    # Package import: unittest / tooling from the node-installer repository.
    from .hb_replication_db import ReplicationDB
    from .hb_replication_policy import BEST_EFFORT, COMMITTABLE, PlacementPolicy, choose_placements
except ImportError:
    # Script import: systemd executes this file directly from /opt/hashburst/replication.
    from hb_replication_db import ReplicationDB
    from hb_replication_policy import BEST_EFFORT, COMMITTABLE, PlacementPolicy, choose_placements

LOG = logging.getLogger("hb-replication-controller")

DEFAULT_DB = os.environ.get("HB_REPL_DB", "/var/lib/hashburst/replication/controller.sqlite3")
DEFAULT_NODES = os.environ.get("HB_STORAGE_NODES", "/etc/hashburst/storage-nodes.json")
DEFAULT_BIND = os.environ.get("HB_REPL_BIND", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("HB_REPL_PORT", "8095"))
DEFAULT_N = int(os.environ.get("HB_REPL_N", "3"))
DEFAULT_M = int(os.environ.get("HB_REPL_M", "2"))
REPAIR_INTERVAL = int(os.environ.get("HB_REPL_REPAIR_INTERVAL", "30"))
EDGE_GRACE = int(os.environ.get("HB_REPL_EDGE_GRACE_SEC", str(6 * 3600)))
COMMITTABLE_GRACE = int(os.environ.get("HB_REPL_COMMITTABLE_GRACE_SEC", str(2 * REPAIR_INTERVAL)))
NODE_STALE = int(os.environ.get("HB_REPL_NODE_STALE_SEC", "90"))
VERIFY_INTERVAL = int(os.environ.get("HB_REPL_VERIFY_INTERVAL_SEC", "900"))
MODE = os.environ.get("HB_REPL_MODE", "observe").strip().lower()  # observe | pin-only
ADMIN_TOKEN = os.environ.get("HB_REPL_ADMIN_TOKEN", "")
SHARED_NODE_TOKEN = os.environ.get("HB_REPL_NODE_TOKEN", "")
MAX_BODY = int(os.environ.get("HB_REPL_MAX_BODY", str(1024 * 1024)))


class Controller:
    def __init__(self, db: ReplicationDB, nodes_file: str, policy: PlacementPolicy, mode: str = MODE):
        if mode not in {"observe", "pin-only"}:
            raise ValueError("HB_REPL_MODE must be observe or pin-only")
        self.db = db
        self.nodes_file = nodes_file
        self.default_policy = policy
        self.mode = mode
        self._stop = threading.Event()

    def sync_registry(self):
        try:
            raw = json.loads(Path(self.nodes_file).read_text())
        except Exception as e:
            LOG.error("cannot read node registry %s: %s", self.nodes_file, e)
            return
        for configured in raw.get("nodes", []):
            if not isinstance(configured, dict) or configured.get("enabled", True) is False:
                continue
            # v1 compatibility: existing registries may not yet contain node_id.
            # We use configured node_id when present, otherwise stable configured name.
            node_id = str(configured.get("node_id") or configured.get("name") or "").strip()
            if not node_id:
                LOG.warning("registry entry skipped: missing node_id/name")
                continue
            role = str(configured.get("role") or "").strip()
            cls = str(configured.get("capacity_class") or "").strip()
            if cls not in {COMMITTABLE, BEST_EFFORT}:
                if role in {"primary", "secondary"}:
                    cls = COMMITTABLE
                elif role == "edge":
                    cls = BEST_EFFORT
                else:
                    cls = "unknown"
            node = dict(configured)
            node.update({"node_id": node_id, "role": role, "capacity_class": cls})
            self.db.upsert_node(node)

    def authenticate_node(self, node_id: str, supplied_token: str) -> bool:
        node = self.db.node(node_id)
        if not node:
            return False
        expected = str(node.get("auth_token") or SHARED_NODE_TOKEN)
        return bool(expected) and hmac.compare_digest(expected, supplied_token or "")

    def register_object(self, payload: dict) -> dict:
        cid = str(payload.get("cid") or "").strip()
        size = int(payload.get("size_bytes") or 0)
        source = str(payload.get("source_node") or "").strip() or None
        reference_id = str(payload.get("reference_id") or "").strip() or None
        n = int(payload.get("replication_n") or self.default_policy.replication_n)
        m = int(payload.get("committable_m") if payload.get("committable_m") is not None else self.default_policy.committable_m)
        policy = PlacementPolicy(n, m, self.default_policy.prefer_edge_for_extra, self.default_policy.safety_margin_bytes)
        policy.validate()
        if not cid or len(cid) > 256:
            raise ValueError("invalid cid")
        if size <= 0:
            raise ValueError("size_bytes must be > 0")
        self.db.register_object(cid, size, n, m, source_node=source, reference_id=reference_id)
        self.reconcile_cid(cid)
        return self.object_status(cid)

    def heartbeat(self, node_id: str, payload: dict) -> dict:
        repo = payload.get("repo") or {}
        total = int(repo.get("total_bytes") or 0)
        used = int(repo.get("used_bytes") or 0)
        if total < 0 or used < 0 or (total and used > total * 1.10):
            raise ValueError("invalid repo capacity")
        self.db.heartbeat(node_id, total, used)
        for report in payload.get("jobs") or []:
            if isinstance(report, dict):
                self.db.apply_job_report(node_id, report)
        return {"ok": True, "mode": self.mode, "server_time": int(time.time())}

    def assignments(self, node_id: str) -> dict:
        jobs = self.db.pending_jobs(node_id) if self.mode == "pin-only" else []
        return {
            "node_id": node_id,
            "mode": self.mode,
            "jobs": [
                {
                    "job_id": j["job_id"], "cid": j["cid"], "operation": j["operation"],
                    "generation": j["generation"], "size_bytes": j["size_bytes"],
                    "lease_until": j["lease_until"],
                }
                for j in jobs
            ],
        }

    def _node_online(self, node: dict, now: int) -> bool:
        return bool(node.get("enabled")) and int(node.get("last_seen") or 0) > 0 and now - int(node["last_seen"]) <= NODE_STALE

    def _classify_observed_replicas(self, cid: str, now: int):
        rows = self.db.replica_rows(cid)
        nodes = {n["node_id"]: n for n in self.db.nodes()}
        for r in rows:
            node = nodes.get(r["node_id"])
            if not node:
                self.db.mark_replica_state(cid, r["node_id"], "missing", "node removed from registry")
                continue
            if self._node_online(node, now):
                # A previous confirmed pin remains confirmed while the node is online.
                # Periodic pin/ls verification is performed by the agent via VERIFY jobs
                # in a later reconciliation enhancement. We do not fabricate a pin here.
                if r["state"] == "grace":
                    self.db.mark_replica_state(cid, r["node_id"], "pinned")
                continue
            if r["state"] not in {"pinned", "grace"}:
                continue
            offline_for = now - int(node.get("last_seen") or r.get("confirmed_at") or r.get("assigned_at") or now)
            cls = r.get("class_at_assignment")
            grace = EDGE_GRACE if cls == BEST_EFFORT else COMMITTABLE_GRACE
            if offline_for < grace:
                self.db.mark_replica_state(cid, r["node_id"], "grace", "node offline within grace")
            else:
                self.db.mark_replica_state(cid, r["node_id"], "missing", "node offline beyond grace")

    @staticmethod
    def _confirmed_counts(replicas: list[dict]) -> tuple[int, int, int]:
        confirmed = [r for r in replicas if r.get("desired") and r.get("state") == "pinned"]
        comm = sum(1 for r in confirmed if r.get("class_at_assignment") == COMMITTABLE)
        edge = sum(1 for r in confirmed if r.get("class_at_assignment") == BEST_EFFORT)
        return len(confirmed), comm, edge

    def reconcile_cid(self, cid: str):
        now = int(time.time())
        obj = self.db.object(cid)
        if not obj or int(obj.get("refcount") or 0) <= 0:
            return
        self._classify_observed_replicas(cid, now)
        obj = self.db.object(cid)
        replicas = obj["replicas"]
        total, comm, _edge = self._confirmed_counts(replicas)

        # Grace replicas intentionally count neither as online confirmation nor as
        # replacement demand until their grace expires. This avoids edge churn.
        grace_total = sum(1 for r in replicas if r.get("desired") and r.get("state") == "grace")
        grace_comm = sum(1 for r in replicas if r.get("desired") and r.get("state") == "grace" and r.get("class_at_assignment") == COMMITTABLE)
        effective_total_for_placement = total + grace_total
        effective_comm_for_placement = comm + grace_comm

        nodes = self.db.nodes()
        online_nodes = []
        for n in nodes:
            n = dict(n)
            n["online"] = self._node_online(n, now)
            online_nodes.append(n)

        # Never immediately reuse a node that already has any desired assignment,
        # including a failed one. This lets repair choose a different target while
        # the old retry may still recover later (temporary over-replication is safe).
        current_node_ids = {r["node_id"] for r in replicas if r.get("desired")}
        reserved, desired_count, assigned_bytes = self.db.placement_load()
        policy = PlacementPolicy(
            int(obj["replication_n"]), int(obj["committable_m"]),
            self.default_policy.prefer_edge_for_extra, self.default_policy.safety_margin_bytes,
        )
        picks = choose_placements(
            cid, int(obj["size_bytes"]), online_nodes, current_node_ids,
            effective_comm_for_placement, effective_total_for_placement,
            reserved, desired_count, assigned_bytes, policy,
        )

        if self.mode == "pin-only":
            for node in picks:
                job = self.db.create_pin_assignment(cid, node["node_id"], node["capacity_class"])
                if job:
                    LOG.info("PIN assignment cid=%s node=%s job=%s", cid, node["node_id"], job)

            # Periodic local pin verification. A VERIFY failure marks the replica
            # failed; the next reconcile can place an additional copy elsewhere.
            nodes_by_id = {n["node_id"]: n for n in online_nodes}
            for r in replicas:
                if r.get("state") != "pinned" or not r.get("desired"):
                    continue
                node = nodes_by_id.get(r["node_id"])
                if not node or not node.get("online"):
                    continue
                if now - int(r.get("last_verified_at") or 0) >= VERIFY_INTERVAL:
                    self.db.create_verify_job(cid, r["node_id"], int(r.get("generation") or obj["generation"]))
        elif picks:
            self.db.audit("PLACEMENT_OBSERVED", cid=cid, candidates=[n["node_id"] for n in picks])

        # Health is based on CONFIRMED ONLINE pins only, not desired assignments.
        replicas = self.db.replica_rows(cid)
        total, comm, edge = self._confirmed_counts(replicas)
        if comm < policy.committable_m:
            state, reason = "degraded_committable", f"confirmed_committable={comm} required={policy.committable_m}"
        elif total < policy.replication_n:
            if any(r.get("state") == "grace" for r in replicas):
                state, reason = "degraded_total", "replica offline within grace"
            else:
                state, reason = "degraded_total", f"confirmed_total={total} target={policy.replication_n}"
        else:
            state, reason = "healthy", ""
        self.db.update_object_health(cid, state, reason)

    def reconcile_all(self):
        self.sync_registry()
        for obj in self.db.objects():
            try:
                self.reconcile_cid(obj["cid"])
            except Exception:
                LOG.exception("reconcile failed cid=%s", obj.get("cid"))

    def object_status(self, cid: str) -> dict:
        obj = self.db.object(cid)
        if not obj:
            raise KeyError(cid)
        total, comm, edge = self._confirmed_counts(obj["replicas"])
        return {
            "cid": cid,
            "size_bytes": obj["size_bytes"],
            "state": obj["state"],
            "reason": obj["reason"],
            "target_replicas": obj["replication_n"],
            "required_committable": obj["committable_m"],
            "confirmed_total": total,
            "confirmed_committable": comm,
            "confirmed_best_effort": edge,
            "sla_replicas": comm,
            "refcount": obj["refcount"],
            "replicas": obj["replicas"],
        }

    def loop(self):
        while not self._stop.wait(REPAIR_INTERVAL):
            self.reconcile_all()

    def stop(self):
        self._stop.set()


class APIHandler(BaseHTTPRequestHandler):
    server_version = "HashBurstReplication/0.1"

    def log_message(self, fmt, *args):
        LOG.info("http %s - %s", self.address_string(), fmt % args)

    @property
    def ctl(self) -> Controller:
        return self.server.controller

    def _json(self, code: int, obj: dict):
        raw = json.dumps(obj, sort_keys=True).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > MAX_BODY:
            raise ValueError("request body too large")
        raw = self.rfile.read(n)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _token(self) -> str:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return self.headers.get("X-HB-Node-Token", "")

    def _admin(self) -> bool:
        return bool(ADMIN_TOKEN) and hmac.compare_digest(ADMIN_TOKEN, self._token())

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            body = self._body()
            if path == "/v1/objects/register":
                if not self._admin():
                    return self._json(403, {"error": "forbidden"})
                return self._json(200, self.ctl.register_object(body))
            if path == "/v1/objects/release":
                if not self._admin():
                    return self._json(403, {"error": "forbidden"})
                return self._json(200, self.ctl.db.release_object(
                    str(body.get("cid") or ""), str(body.get("reference_id") or "").strip() or None))
            if path == "/v1/nodes/heartbeat":
                node_id = str(body.get("node_id") or "")
                if not self.ctl.authenticate_node(node_id, self._token()):
                    return self._json(403, {"error": "forbidden"})
                return self._json(200, self.ctl.heartbeat(node_id, body))
            return self._json(404, {"error": "not found"})
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            return self._json(400, {"error": str(e)})
        except Exception as e:
            LOG.exception("POST failed")
            return self._json(500, {"error": "internal error", "detail": str(e)[:160]})

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if path == "/v1/health":
                return self._json(200, {"ok": True, "mode": self.ctl.mode, "time": int(time.time())})
            if path == "/v1/stats":
                if not self._admin():
                    return self._json(403, {"error": "forbidden"})
                return self._json(200, self.ctl.db.stats())
            prefix = "/v1/nodes/"
            suffix = "/assignments"
            if path.startswith(prefix) and path.endswith(suffix):
                node_id = unquote(path[len(prefix):-len(suffix)]).strip("/")
                if not self.ctl.authenticate_node(node_id, self._token()):
                    return self._json(403, {"error": "forbidden"})
                return self._json(200, self.ctl.assignments(node_id))
            prefix = "/v1/objects/"
            if path.startswith(prefix):
                if not self._admin():
                    return self._json(403, {"error": "forbidden"})
                cid = unquote(path[len(prefix):])
                return self._json(200, self.ctl.object_status(cid))
            return self._json(404, {"error": "not found"})
        except KeyError as e:
            return self._json(404, {"error": str(e)})
        except Exception as e:
            LOG.exception("GET failed")
            return self._json(500, {"error": "internal error", "detail": str(e)[:160]})


class ControllerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, controller: Controller):
        super().__init__(addr, handler)
        self.controller = controller


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--nodes", default=DEFAULT_NODES)
    ap.add_argument("--bind", default=DEFAULT_BIND)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--mode", choices=["observe", "pin-only"], default=MODE)
    ap.add_argument("--log-level", default=os.environ.get("HB_REPL_LOG_LEVEL", "INFO"))
    args = ap.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not ADMIN_TOKEN:
        raise SystemExit("HB_REPL_ADMIN_TOKEN must be set (fail-closed)")
    if not SHARED_NODE_TOKEN:
        LOG.warning("HB_REPL_NODE_TOKEN is empty; each registry node must define replication_token")

    db = ReplicationDB(args.db)
    policy = PlacementPolicy(DEFAULT_N, DEFAULT_M)
    policy.validate()
    ctl = Controller(db, args.nodes, policy, args.mode)
    ctl.sync_registry()
    ctl.reconcile_all()

    thread = threading.Thread(target=ctl.loop, name="repair-loop", daemon=True)
    thread.start()
    server = ControllerServer((args.bind, args.port), APIHandler, ctl)

    def shutdown(_sig, _frame):
        ctl.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    LOG.info("controller listening %s:%s mode=%s N=%s M=%s", args.bind, args.port, args.mode, DEFAULT_N, DEFAULT_M)
    server.serve_forever(poll_interval=0.5)
    server.server_close()


if __name__ == "__main__":
    main()
