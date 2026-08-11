#!/usr/bin/env python3
"""v2.1.4 lifecycle extension for the v2.1.3 replication database.

The base schema remains readable in-place. Migrations are additive and preserve
all v2.1.3 rows. Destructive jobs are generation fenced and require a second,
just-in-time authorization before an agent may unpin local Kubo content.
"""
from __future__ import annotations

import json
import time
import uuid

try:
    from .hb_replication_db import ReplicationDB
except ImportError:
    from hb_replication_db import ReplicationDB


class LifecycleConflict(RuntimeError):
    pass


class ReplicationDBV214(ReplicationDB):
    def __init__(self, path: str):
        super().__init__(path)
        self._migrate_v214()

    def _columns(self, table: str) -> set[str]:
        return {str(r[1]) for r in self._conn.execute(f"PRAGMA table_info({table})")}

    def _migrate_v214(self):
        additions = {
            "nodes": {
                "failure_domain": "TEXT NOT NULL DEFAULT ''",
                "provider": "TEXT NOT NULL DEFAULT ''",
                "region": "TEXT NOT NULL DEFAULT ''",
                "rack": "TEXT NOT NULL DEFAULT ''",
            },
            "objects": {"released_at": "INTEGER NOT NULL DEFAULT 0"},
            "object_refs": {
                "released_at": "INTEGER NOT NULL DEFAULT 0",
                "release_request_id": "TEXT NOT NULL DEFAULT ''",
            },
            "jobs": {
                "reason": "TEXT NOT NULL DEFAULT ''",
                "authorized_at": "INTEGER NOT NULL DEFAULT 0",
            },
        }
        with self._lock:
            for table, cols in additions.items():
                existing = self._columns(table)
                for name, ddl in cols.items():
                    if name not in existing:
                        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_cid_operation ON jobs(cid,operation,state)"
            )

    def upsert_node(self, node: dict):
        super().upsert_node(node)
        with self._lock:
            self._conn.execute(
                """UPDATE nodes SET failure_domain=?,provider=?,region=?,rack=? WHERE node_id=?""",
                (
                    str(node.get("failure_domain") or "").strip(),
                    str(node.get("provider") or "").strip(),
                    str(node.get("region") or "").strip(),
                    str(node.get("rack") or "").strip(),
                    node["node_id"],
                ),
            )

    def register_object(self, cid: str, size_bytes: int, n: int, m: int,
                        source_node: str | None = None, reference_id: str | None = None):
        # Never resurrect an object while a destructive operation has already
        # crossed the authorization boundary. The caller may retry afterwards.
        with self._lock:
            obj = self._conn.execute(
                "SELECT generation,refcount FROM objects WHERE cid=?", (cid,)
            ).fetchone()
            if obj and int(obj[1] or 0) == 0:
                pending = self._conn.execute(
                    """SELECT 1 FROM jobs WHERE cid=? AND operation='UNPIN'
                       AND state='authorized' LIMIT 1""", (cid,)
                ).fetchone()
                if pending:
                    raise LifecycleConflict("CID has an authorized UNPIN in progress")
        out = super().register_object(cid, size_bytes, n, m, source_node, reference_id)
        with self.tx() as c:
            row = c.execute(
                "SELECT refcount,released_at FROM objects WHERE cid=?", (cid,)
            ).fetchone()
            if row and int(row[0]) > 0 and int(row[1] or 0):
                c.execute(
                    "UPDATE objects SET generation=generation+1,released_at=0,state='pending',reason='' WHERE cid=?",
                    (cid,),
                )
                gen = int(c.execute("SELECT generation FROM objects WHERE cid=?", (cid,)).fetchone()[0])
                c.execute(
                    "UPDATE replicas SET desired=1,generation=? WHERE cid=? AND state<>'unpinned'",
                    (gen, cid),
                )
                c.execute(
                    "UPDATE jobs SET state='stale',updated_at=? WHERE cid=? AND operation='UNPIN' AND state IN ('pending','retry')",
                    (int(time.time()), cid),
                )
        return out

    def release_object(self, cid: str, reference_id: str | None = None,
                       request_id: str | None = None, actor: str | None = None) -> dict:
        now = int(time.time())
        reference_id = str(reference_id or "").strip() or None
        request_id = str(request_id or "").strip() or uuid.uuid4().hex
        actor = str(actor or "").strip()
        with self.tx() as c:
            obj = c.execute("SELECT refcount,generation FROM objects WHERE cid=?", (cid,)).fetchone()
            if not obj:
                return {"ok": False, "reason": "not-found"}
            old_ref = int(obj[0])
            if reference_id:
                ref = c.execute(
                    "SELECT cid,released_at FROM object_refs WHERE reference_id=?", (reference_id,)
                ).fetchone()
                if not ref:
                    return {"ok": True, "cid": cid, "reference_id": reference_id,
                            "refcount": old_ref, "released": False, "final_release": old_ref == 0}
                if ref[0] != cid:
                    raise ValueError("reference_id belongs to a different CID")
                if int(ref[1] or 0):
                    return {"ok": True, "cid": cid, "reference_id": reference_id,
                            "refcount": old_ref, "released": False, "final_release": old_ref == 0}
                c.execute(
                    "UPDATE object_refs SET released_at=?,release_request_id=? WHERE reference_id=?",
                    (now, request_id, reference_id),
                )
            elif old_ref <= 0:
                return {"ok": True, "cid": cid, "reference_id": None,
                        "refcount": 0, "released": False, "final_release": True}

            new_ref = max(0, old_ref - 1)
            final = new_ref == 0
            if final:
                c.execute(
                    """UPDATE objects SET refcount=0,released_at=?,state='release_pending',
                       reason='logical references released',generation=generation+1 WHERE cid=?""",
                    (now, cid),
                )
                generation = int(c.execute(
                    "SELECT generation FROM objects WHERE cid=?", (cid,)
                ).fetchone()[0])
                c.execute(
                    "UPDATE replicas SET desired=0,generation=? WHERE cid=?",
                    (generation, cid),
                )
                c.execute(
                    """UPDATE jobs SET state='stale',updated_at=? WHERE cid=?
                       AND operation IN ('PIN','VERIFY') AND state IN ('pending','retry')""",
                    (now, cid),
                )
            else:
                c.execute("UPDATE objects SET refcount=? WHERE cid=?", (new_ref, cid))
                generation = int(obj[1])
        self.audit(
            "OBJECT_RELEASE", cid=cid, reference_id=reference_id or "",
            request_id=request_id, actor=actor, old_refcount=old_ref,
            new_refcount=new_ref, final_release=final, generation=generation,
        )
        return {"ok": True, "cid": cid, "reference_id": reference_id,
                "request_id": request_id, "refcount": new_ref, "released": True,
                "final_release": final, "generation": generation}

    def create_unpin_job(self, cid: str, node_id: str, generation: int,
                         reason: str, not_before: int = 0) -> str | None:
        now = int(time.time())
        with self.tx() as c:
            obj = c.execute("SELECT refcount,generation FROM objects WHERE cid=?", (cid,)).fetchone()
            if not obj or int(obj[0]) != 0 or int(obj[1]) != int(generation):
                return None
            active = c.execute(
                """SELECT job_id FROM jobs WHERE cid=? AND node_id=? AND operation='UNPIN'
                   AND generation=? AND state IN ('pending','retry','authorized') LIMIT 1""",
                (cid, node_id, int(generation)),
            ).fetchone()
            if active:
                return None
            job_id = uuid.uuid4().hex
            c.execute(
                """INSERT INTO jobs(job_id,cid,node_id,operation,generation,state,next_attempt_at,
                   created_at,updated_at,reason) VALUES(?,?,?,?,?,'pending',?,?,?,?)""",
                (job_id, cid, node_id, "UNPIN", int(generation), max(now, int(not_before)), now, now, reason[:200]),
            )
        self.audit("UNPIN_PLANNED", cid=cid, node_id=node_id, job_id=job_id,
                   generation=int(generation), reason=reason[:200])
        return job_id

    def authorize_unpin(self, node_id: str, job_id: str, lease_until: int) -> dict:
        now = int(time.time())
        with self.tx() as c:
            job = c.execute(
                "SELECT * FROM jobs WHERE job_id=? AND node_id=? AND operation='UNPIN'",
                (job_id, node_id),
            ).fetchone()
            if not job:
                return {"authorized": False, "reason": "unknown-job"}
            obj = c.execute("SELECT refcount,generation FROM objects WHERE cid=?", (job["cid"],)).fetchone()
            valid = (
                obj and int(obj[0]) == 0 and int(obj[1]) == int(job["generation"])
                and int(job["lease_until"] or 0) == int(lease_until)
                and job["state"] in {"pending", "retry"}
            )
            if not valid:
                c.execute("UPDATE jobs SET state='stale',updated_at=? WHERE job_id=?", (now, job_id))
                return {"authorized": False, "reason": "stale-or-referenced"}
            c.execute(
                "UPDATE jobs SET state='authorized',authorized_at=?,updated_at=? WHERE job_id=?",
                (now, now, job_id),
            )
            return {"authorized": True, "cid": job["cid"], "generation": int(job["generation"])}

    def recover_v214(self):
        now = int(time.time())
        with self.tx() as c:
            # An authorized UNPIN has unknown physical outcome after restart.
            # Convert it into VERIFY: if still pinned, reconciliation can safely
            # schedule a fresh UNPIN; if absent, state converges to unpinned.
            rows = c.execute(
                "SELECT job_id,cid,node_id,generation FROM jobs WHERE operation='UNPIN' AND state='authorized'"
            ).fetchall()
            for row in rows:
                c.execute("UPDATE jobs SET state='stale',updated_at=? WHERE job_id=?", (now, row[0]))
                verify_id = uuid.uuid4().hex
                c.execute(
                    """INSERT INTO jobs(job_id,cid,node_id,operation,generation,state,next_attempt_at,
                       created_at,updated_at,reason) VALUES(?,?,?,?,?,'pending',0,?,?,?)""",
                    (verify_id, row[1], row[2], int(row[3]), now, now, "recover-authorized-unpin"),
                )
        if rows:
            self.audit("RECOVERY_UNPIN_VERIFY", count=len(rows))

    def pending_jobs(self, node_id: str, now: int | None = None, limit: int = 32,
                     allowed_operations: set[str] | None = None):
        jobs = super().pending_jobs(node_id, now=now, limit=limit)
        if allowed_operations is None:
            return jobs
        return [j for j in jobs if j.get("operation") in allowed_operations]

    def apply_job_report(self, node_id: str, report: dict):
        job_id = str(report.get("job_id") or "")
        with self._lock:
            job = self._conn.execute("SELECT * FROM jobs WHERE job_id=? AND node_id=?", (job_id, node_id)).fetchone()
        if not job or job["operation"] != "UNPIN":
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
            if int(job["lease_until"] or 0) != lease or job["state"] != "authorized":
                return
            if state == "unpinned":
                c.execute("UPDATE jobs SET state='done',lease_until=0,updated_at=?,last_error='' WHERE job_id=?", (now, job_id))
                c.execute(
                    "UPDATE replicas SET state='unpinned',desired=0,last_error='' WHERE cid=? AND node_id=?",
                    (job["cid"], node_id),
                )
            elif state == "failed":
                attempts = int(job["attempts"]) + 1
                delay = [30, 60, 120, 300, 900, 1800][min(attempts - 1, 5)]
                err = str(report.get("error") or "unpin failed")[:500]
                c.execute(
                    """UPDATE jobs SET state='retry',attempts=?,next_attempt_at=?,lease_until=0,
                       authorized_at=0,updated_at=?,last_error=? WHERE job_id=?""",
                    (attempts, now + delay, now, err, job_id),
                )
            else:
                raise ValueError(f"invalid UNPIN report state: {state}")
        self.audit("UNPIN_REPORT", cid=job["cid"], node_id=node_id,
                   job_id=job_id, state=state, generation=int(job["generation"]))
