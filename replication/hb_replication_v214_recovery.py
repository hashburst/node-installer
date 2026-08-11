#!/usr/bin/env python3
"""Crash-recovery refinement for v2.1.4 destructive lifecycle."""
from __future__ import annotations

import time
import uuid

try:
    from .hb_replication_v214_db import ReplicationDBV214
except ImportError:
    from hb_replication_v214_db import ReplicationDBV214


class ReplicationDBV214Recovery(ReplicationDBV214):
    def recover_v214(self):
        now = int(time.time())
        with self.tx() as c:
            rows = c.execute(
                "SELECT job_id,cid,node_id,generation FROM jobs WHERE operation='UNPIN' AND state='authorized'"
            ).fetchall()
            for row in rows:
                c.execute("UPDATE jobs SET state='stale',updated_at=? WHERE job_id=?", (now, row[0]))
                active = c.execute(
                    """SELECT 1 FROM jobs WHERE cid=? AND node_id=? AND operation='UNPIN_VERIFY'
                       AND generation=? AND state IN ('pending','retry') LIMIT 1""",
                    (row[1], row[2], int(row[3])),
                ).fetchone()
                if active:
                    continue
                verify_id = uuid.uuid4().hex
                c.execute(
                    """INSERT INTO jobs(job_id,cid,node_id,operation,generation,state,next_attempt_at,
                       created_at,updated_at,reason) VALUES(?,?,?,?,?,'pending',0,?,?,?)""",
                    (verify_id, row[1], row[2], "UNPIN_VERIFY", int(row[3]), now, now,
                     "recover-authorized-unpin"),
                )
        if rows:
            self.audit("RECOVERY_UNPIN_VERIFY", count=len(rows))

    def apply_job_report(self, node_id: str, report: dict):
        job_id = str(report.get("job_id") or "")
        with self._lock:
            job = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id=? AND node_id=?", (job_id, node_id)
            ).fetchone()
        if not job or job["operation"] != "UNPIN_VERIFY":
            return super().apply_job_report(node_id, report)

        now = int(time.time())
        state = str(report.get("state") or "")
        with self.tx() as c:
            job = c.execute("SELECT * FROM jobs WHERE job_id=? AND node_id=?", (job_id, node_id)).fetchone()
            obj = c.execute("SELECT refcount,generation FROM objects WHERE cid=?", (job["cid"],)).fetchone()
            lease = int(report.get("lease_until") or 0)
            if not obj or int(obj[0]) != 0 or int(obj[1]) != int(job["generation"]):
                c.execute("UPDATE jobs SET state='stale',updated_at=? WHERE job_id=?", (now, job_id))
                return
            if int(job["lease_until"] or 0) != lease:
                return
            if state == "unpinned":
                c.execute("UPDATE jobs SET state='done',lease_until=0,updated_at=?,last_error='' WHERE job_id=?", (now, job_id))
                c.execute(
                    "UPDATE replicas SET state='unpinned',desired=0,last_error='' WHERE cid=? AND node_id=?",
                    (job["cid"], node_id),
                )
            elif state == "verified":
                # Content is still pinned. Verification itself succeeded; mark
                # this recovery check done and let reconciliation schedule a new,
                # freshly fenced UNPIN only if destructive mode remains enabled.
                c.execute("UPDATE jobs SET state='done',lease_until=0,updated_at=?,last_error='' WHERE job_id=?", (now, job_id))
                c.execute(
                    "UPDATE replicas SET state='pinned',desired=0,last_verified_at=?,last_error='' WHERE cid=? AND node_id=?",
                    (now, job["cid"], node_id),
                )
            elif state == "failed":
                attempts = int(job["attempts"]) + 1
                delay = [30, 60, 120, 300, 900, 1800][min(attempts - 1, 5)]
                err = str(report.get("error") or "unpin verification failed")[:500]
                c.execute(
                    """UPDATE jobs SET state='retry',attempts=?,next_attempt_at=?,lease_until=0,
                       updated_at=?,last_error=? WHERE job_id=?""",
                    (attempts, now + delay, now, err, job_id),
                )
            else:
                raise ValueError(f"invalid UNPIN_VERIFY report state: {state}")
        self.audit("UNPIN_VERIFY_REPORT", cid=job["cid"], node_id=node_id,
                   job_id=job_id, state=state, generation=int(job["generation"]))
