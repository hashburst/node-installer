#!/usr/bin/env python3
"""HashBurst replica agent v2.1.4.

PIN and VERIFY behavior is inherited from v2.1.3. UNPIN is fail-closed and
requires both the local HB_REPL_ALLOW_UNPIN=1 gate and a just-in-time
controller authorization for the exact job lease immediately before pin/rm.
"""
from __future__ import annotations

import argparse
import logging
import os

try:
    from . import hb_replica_agent as base
except ImportError:
    import hb_replica_agent as base

LOG = logging.getLogger("hb-replica-agent-v214")
ALLOW_UNPIN = os.environ.get("HB_REPL_ALLOW_UNPIN", "0") == "1"


class AgentV214(base.Agent):
    def execute_job(self, job: dict) -> dict:
        operation = str(job.get("operation") or "")
        job_id = str(job.get("job_id") or "")
        cid = str(job.get("cid") or "")
        lease_until = int(job.get("lease_until") or 0)

        if operation == "UNPIN_VERIFY":
            try:
                state = "verified" if self.ipfs.is_pinned(cid) else "unpinned"
                return {"job_id": job_id, "state": state, "lease_until": lease_until}
            except Exception as exc:
                return {"job_id": job_id, "state": "failed", "lease_until": lease_until,
                        "error": str(exc)[:500]}

        if operation != "UNPIN":
            return super().execute_job(job)

        try:
            if not ALLOW_UNPIN:
                raise RuntimeError("UNPIN disabled by HB_REPL_ALLOW_UNPIN=0")
            auth = self._request(
                "POST",
                f"/v1/nodes/{self.node_id}/authorize-unpin",
                {"job_id": job_id, "lease_until": lease_until},
            )
            if not auth.get("authorized"):
                raise RuntimeError("controller denied UNPIN: " + str(auth.get("reason") or "denied"))
            if str(auth.get("cid") or "") != cid:
                raise RuntimeError("controller UNPIN authorization CID mismatch")
            self.ipfs.unpin(cid)
            if self.ipfs.is_pinned(cid):
                raise RuntimeError("pin/rm returned but recursive pin still exists")
            return {"job_id": job_id, "state": "unpinned", "lease_until": lease_until}
        except Exception as exc:
            return {"job_id": job_id, "state": "failed", "lease_until": lease_until,
                    "error": str(exc)[:500]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--log-level", default=os.environ.get("HB_REPL_LOG_LEVEL", "INFO"))
    args = ap.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    agent = AgentV214(base.CONTROLLER, base.NODE_ID, base.NODE_TOKEN, base.IPFS_API)
    if args.once:
        agent.run_once()
    else:
        agent.loop()


if __name__ == "__main__":
    main()
