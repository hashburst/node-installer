#!/usr/bin/env python3
"""HashBurst Replication Controller v2.1.4 entrypoint.

This module layers lifecycle safety on the proven v2.1.3 controller. Default
mode remains observe and destructive UNPIN additionally requires
HB_REPL_UNPIN_ENABLED=1.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.parse import unquote, urlparse

try:
    from . import hb_replication_controller as base
    from .hb_replication_v214_db import LifecycleConflict, ReplicationDBV214
    from .hb_replication_policy import BEST_EFFORT, COMMITTABLE, PlacementPolicy
except ImportError:
    import hb_replication_controller as base
    from hb_replication_v214_db import LifecycleConflict, ReplicationDBV214
    from hb_replication_policy import BEST_EFFORT, COMMITTABLE, PlacementPolicy

LOG = logging.getLogger("hb-replication-controller-v214")
RECONCILE_INTERVAL = int(os.environ.get("HB_REPL_RECONCILE_INTERVAL_SEC", "900"))
DELETE_GRACE = int(os.environ.get("HB_REPL_DELETE_GRACE_SEC", "900"))
UNPIN_ENABLED = os.environ.get("HB_REPL_UNPIN_ENABLED", "0") == "1"
MODE = os.environ.get("HB_REPL_MODE", "observe").strip().lower()


class ControllerV214(base.Controller):
    def __init__(self, db, nodes_file, policy, mode=MODE, unpin_enabled=UNPIN_ENABLED):
        if mode not in {"observe", "pin-only", "full"}:
            raise ValueError("HB_REPL_MODE must be observe, pin-only or full")
        # Initialize the proven controller in its active non-destructive mode,
        # then expose the explicit v2.1.4 mode.
        super().__init__(db, nodes_file, policy, "pin-only" if mode == "full" else mode)
        self.mode = mode
        self.unpin_enabled = bool(unpin_enabled)
        self._last_full_reconcile = 0

    def release_object(self, payload: dict) -> dict:
        cid = str(payload.get("cid") or "").strip()
        if not cid:
            raise ValueError("cid required")
        result = self.db.release_object(
            cid,
            str(payload.get("reference_id") or "").strip() or None,
            str(payload.get("request_id") or "").strip() or None,
            str(payload.get("actor") or "").strip() or None,
        )
        if result.get("ok"):
            self.reconcile_cid(cid)
        result["unpin_scheduled"] = bool(
            result.get("final_release") and self.mode == "full" and self.unpin_enabled
        )
        return result

    def assignments(self, node_id: str) -> dict:
        if self.mode == "observe":
            jobs = []
        else:
            allowed = {"PIN", "VERIFY"}
            if self.mode == "full" and self.unpin_enabled:
                allowed.add("UNPIN")
            jobs = self.db.pending_jobs(node_id, allowed_operations=allowed)
        return {
            "node_id": node_id,
            "mode": self.mode,
            "unpin_enabled": self.unpin_enabled,
            "jobs": [{
                "job_id": j["job_id"], "cid": j["cid"], "operation": j["operation"],
                "generation": j["generation"], "size_bytes": j["size_bytes"],
                "lease_until": j["lease_until"],
            } for j in jobs],
        }

    def authorize_unpin(self, node_id: str, payload: dict) -> dict:
        if self.mode != "full" or not self.unpin_enabled:
            return {"authorized": False, "reason": "unpin-disabled"}
        return self.db.authorize_unpin(
            node_id,
            str(payload.get("job_id") or ""),
            int(payload.get("lease_until") or 0),
        )

    def _plan_final_release(self, obj: dict, now: int):
        if self.mode != "full" or not self.unpin_enabled:
            return
        released_at = int(obj.get("released_at") or 0)
        if not released_at or now < released_at + DELETE_GRACE:
            return
        generation = int(obj["generation"])
        for replica in obj.get("replicas") or []:
            if replica.get("state") == "unpinned":
                continue
            self.db.create_unpin_job(
                obj["cid"], replica["node_id"], generation,
                reason="final-release", not_before=released_at + DELETE_GRACE,
            )

    def _plan_trim(self, obj: dict):
        if self.mode != "full" or not self.unpin_enabled or int(obj.get("refcount") or 0) <= 0:
            return
        pinned = [r for r in obj.get("replicas") or [] if r.get("desired") and r.get("state") == "pinned"]
        target = int(obj["replication_n"])
        required_comm = int(obj["committable_m"])
        if len(pinned) <= target:
            return
        comm = sum(1 for r in pinned if r.get("class_at_assignment") == COMMITTABLE)
        # Prefer removing best-effort replicas; remove committable copies only
        # when M remains satisfied after the trim.
        candidates = sorted(
            pinned,
            key=lambda r: (0 if r.get("class_at_assignment") == BEST_EFFORT else 1,
                           int(r.get("last_verified_at") or 0), r["node_id"]),
        )
        remove = len(pinned) - target
        for replica in candidates:
            if remove <= 0:
                break
            is_comm = replica.get("class_at_assignment") == COMMITTABLE
            if is_comm and comm - 1 < required_comm:
                continue
            job = self.db.create_unpin_job(
                obj["cid"], replica["node_id"], int(obj["generation"]), reason="trim-extra-replica"
            )
            if job:
                remove -= 1
                if is_comm:
                    comm -= 1

    def reconcile_cid(self, cid: str):
        obj = self.db.object(cid)
        if not obj:
            return
        if int(obj.get("refcount") or 0) <= 0:
            self._plan_final_release(obj, int(time.time()))
            return
        super().reconcile_cid(cid)
        self._plan_trim(self.db.object(cid))

    def reconcile_all(self):
        self.sync_registry()
        for obj in self.db.objects():
            try:
                self.reconcile_cid(obj["cid"])
            except Exception:
                LOG.exception("reconcile failed cid=%s", obj.get("cid"))

    def loop(self):
        while not self._stop.wait(base.REPAIR_INTERVAL):
            self.reconcile_all()


class APIHandlerV214(base.APIHandler):
    @property
    def ctl(self) -> ControllerV214:
        return self.server.controller

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
                return self._json(200, self.ctl.release_object(body))
            if path == "/v1/nodes/heartbeat":
                node_id = str(body.get("node_id") or "")
                if not self.ctl.authenticate_node(node_id, self._token()):
                    return self._json(403, {"error": "forbidden"})
                return self._json(200, self.ctl.heartbeat(node_id, body))
            prefix = "/v1/nodes/"
            suffix = "/authorize-unpin"
            if path.startswith(prefix) and path.endswith(suffix):
                node_id = unquote(path[len(prefix):-len(suffix)]).strip("/")
                if not self.ctl.authenticate_node(node_id, self._token()):
                    return self._json(403, {"error": "forbidden"})
                return self._json(200, self.ctl.authorize_unpin(node_id, body))
            return self._json(404, {"error": "not found"})
        except (LifecycleConflict, ValueError, KeyError, json.JSONDecodeError) as exc:
            return self._json(409 if isinstance(exc, LifecycleConflict) else 400, {"error": str(exc)})
        except Exception as exc:
            LOG.exception("POST failed")
            return self._json(500, {"error": "internal error", "detail": str(exc)[:160]})

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if path == "/v1/health":
                return self._json(200, {
                    "ok": True, "mode": self.ctl.mode,
                    "unpin_enabled": self.ctl.unpin_enabled,
                    "version": "2.1.4", "time": int(time.time()),
                })
            return super().do_GET()
        except Exception as exc:
            LOG.exception("GET failed")
            return self._json(500, {"error": "internal error", "detail": str(exc)[:160]})


class ControllerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    def __init__(self, addr, handler, controller):
        super().__init__(addr, handler)
        self.controller = controller


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=base.DEFAULT_DB)
    ap.add_argument("--nodes", default=base.DEFAULT_NODES)
    ap.add_argument("--bind", default=base.DEFAULT_BIND)
    ap.add_argument("--port", type=int, default=base.DEFAULT_PORT)
    ap.add_argument("--mode", choices=["observe", "pin-only", "full"], default=MODE)
    ap.add_argument("--log-level", default=os.environ.get("HB_REPL_LOG_LEVEL", "INFO"))
    args = ap.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not base.ADMIN_TOKEN:
        raise SystemExit("HB_REPL_ADMIN_TOKEN must be set (fail-closed)")
    db = ReplicationDBV214(args.db)
    db.recover_v214()
    policy = PlacementPolicy(base.DEFAULT_N, base.DEFAULT_M)
    policy.validate()
    ctl = ControllerV214(db, args.nodes, policy, args.mode, UNPIN_ENABLED)
    ctl.sync_registry()
    ctl.reconcile_all()
    thread = threading.Thread(target=ctl.loop, name="repair-loop-v214", daemon=True)
    thread.start()
    server = ControllerServer((args.bind, args.port), APIHandlerV214, ctl)
    def shutdown(_sig, _frame):
        ctl.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    LOG.info("controller v2.1.4 listening %s:%s mode=%s unpin=%s", args.bind, args.port, args.mode, UNPIN_ENABLED)
    server.serve_forever(poll_interval=0.5)
    server.server_close()


if __name__ == "__main__":
    main()
