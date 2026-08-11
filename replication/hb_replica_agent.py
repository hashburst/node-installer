#!/usr/bin/env python3
"""HashBurst Replica Agent v0.1.

Runs on each storage node and uses outbound HTTP to poll the controller.
Kubo RPC stays localhost-only. Default behavior cannot UNPIN.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

# In the packaged repository this module lives under hbfiles/ and is imported
# through PYTHONPATH set by the systemd unit.
import hb_ipfs

LOG = logging.getLogger("hb-replica-agent")
CONTROLLER = os.environ.get("HB_REPL_CONTROLLER", "http://127.0.0.1:8095").rstrip("/")
NODE_ID = os.environ.get("HB_REPL_NODE_ID", "").strip()
NODE_TOKEN = os.environ.get("HB_REPL_NODE_TOKEN", "")
IPFS_API = os.environ.get("HB_IPFS_PRIVATE_API", "http://127.0.0.1:5011")
POLL_SEC = int(os.environ.get("HB_REPL_AGENT_POLL_SEC", "30"))
HTTP_TIMEOUT = int(os.environ.get("HB_REPL_HTTP_TIMEOUT", "10"))
ALLOW_UNPIN = os.environ.get("HB_REPL_ALLOW_UNPIN", "0") == "1"


class Agent:
    def __init__(self, controller: str, node_id: str, token: str, ipfs_api: str):
        if not node_id:
            raise ValueError("HB_REPL_NODE_ID must be set")
        if not token:
            raise ValueError("HB_REPL_NODE_TOKEN must be set")
        self.controller = controller.rstrip("/")
        self.node_id = node_id
        self.token = token
        self.ipfs = hb_ipfs.IPFSClient(ipfs_api)
        self.pending_reports: list[dict] = []

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            self.controller + path, data=data, method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-HB-Node-Token": self.token,
                "User-Agent": "HashBurst-Replica-Agent/0.1",
            },
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))

    def repo_capacity(self) -> tuple[int, int]:
        stat = self.ipfs.repo_stat()
        used = int(stat.get("RepoSize") or 0)
        total = int(stat.get("StorageMax") or 0)
        return total, used

    def heartbeat(self):
        total, used = self.repo_capacity()
        payload = {
            "node_id": self.node_id,
            "timestamp": int(time.time()),
            "repo": {"total_bytes": total, "used_bytes": used},
            "jobs": self.pending_reports,
        }
        result = self._request("POST", "/v1/nodes/heartbeat", payload)
        self.pending_reports = []
        return result

    def assignments(self) -> list[dict]:
        node = urllib.parse.quote(self.node_id, safe="")
        data = self._request("GET", f"/v1/nodes/{node}/assignments")
        return list(data.get("jobs") or [])

    def execute_job(self, job: dict) -> dict:
        job_id = str(job.get("job_id") or "")
        cid = str(job.get("cid") or "")
        operation = str(job.get("operation") or "")
        lease_until = int(job.get("lease_until") or 0)
        try:
            if operation == "PIN":
                self.ipfs.pin(cid)
                if not self.ipfs.is_pinned(cid):
                    raise hb_ipfs.IPFSError("pin/add returned but pin/ls did not confirm recursive pin")
                return {"job_id": job_id, "state": "pinned", "lease_until": lease_until}
            if operation == "VERIFY":
                if not self.ipfs.is_pinned(cid):
                    raise hb_ipfs.IPFSError("CID is not recursively pinned")
                return {"job_id": job_id, "state": "verified", "lease_until": lease_until}
            if operation == "UNPIN":
                if not ALLOW_UNPIN:
                    raise RuntimeError("UNPIN disabled by HB_REPL_ALLOW_UNPIN=0")
                self.ipfs.unpin(cid)
                return {"job_id": job_id, "state": "verified", "lease_until": lease_until}
            raise ValueError(f"unsupported operation: {operation}")
        except Exception as e:
            return {"job_id": job_id, "state": "failed", "lease_until": lease_until, "error": str(e)[:500]}

    def run_once(self):
        self.heartbeat()
        for job in self.assignments():
            report = self.execute_job(job)
            self.pending_reports.append(report)
        # Report results immediately; if this fails the idempotent job will be
        # delivered again and local pin verification will safely repeat.
        if self.pending_reports:
            self.heartbeat()

    def loop(self):
        while True:
            try:
                self.run_once()
            except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
                LOG.warning("poll failed: %s", e)
            except Exception:
                LOG.exception("agent iteration failed")
            time.sleep(POLL_SEC)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--log-level", default=os.environ.get("HB_REPL_LOG_LEVEL", "INFO"))
    args = ap.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    agent = Agent(CONTROLLER, NODE_ID, NODE_TOKEN, IPFS_API)
    if args.once:
        agent.run_once()
    else:
        agent.loop()


if __name__ == "__main__":
    main()
