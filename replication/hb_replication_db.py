#!/usr/bin/env python3
"""SQLite state store for HashBurst Replication Controller.

Single-writer v1 design. SQLite transactions protect placement/job creation
against duplicate repairs inside one controller process.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

SCHEMA = r"""
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    capacity_class TEXT NOT NULL DEFAULT 'unknown',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_seen INTEGER NOT NULL DEFAULT 0,
    last_pin_report INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    used_bytes INTEGER NOT NULL DEFAULT 0,
    stability_score INTEGER NOT NULL DEFAULT 0,
    failure_penalty INTEGER NOT NULL DEFAULT 0,
    auth_token TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS objects (
    cid TEXT PRIMARY KEY,
    size_bytes INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    replication_n INTEGER NOT NULL,
    committable_m INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    reason TEXT NOT NULL DEFAULT '',
    generation INTEGER NOT NULL DEFAULT 1,
    refcount INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS object_refs (
    reference_id TEXT PRIMARY KEY,
    cid TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (cid) REFERENCES objects(cid) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_object_refs_cid ON object_refs(cid);

CREATE TABLE IF NOT EXISTS replicas (
    cid TEXT NOT NULL,
    node_id TEXT NOT NULL,
    desired INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'assigned',
    class_at_assignment TEXT NOT NULL DEFAULT 'unknown',
    assigned_at INTEGER NOT NULL,
    confirmed_at INTEGER NOT NULL DEFAULT 0,
    last_verified_at INTEGER NOT NULL DEFAULT 0,
    failure_since INTEGER NOT NULL DEFAULT 0,
    generation INTEGER NOT NULL,
    last_error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (cid, node_id),
    FOREIGN KEY (cid) REFERENCES objects(cid) ON DELETE CASCADE,
    FOREIGN KEY (node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    cid TEXT NOT NULL,
    node_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    generation INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    lease_until INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    last_error TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (cid) REFERENCES objects(cid) ON DELETE CASCADE,
    FOREIGN KEY (node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    cid TEXT NOT NULL DEFAULT '',
    node_id TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_jobs_node_state ON jobs(node_id, state, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_replicas_cid_state ON replicas(cid, state);
CREATE INDEX IF NOT EXISTS idx_replicas_node ON replicas(node_id);
"""


class ReplicationDB:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def audit(self, action: str, cid: str = "", node_id: str = "", **detail):
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit(ts,cid,node_id,action,detail_json) VALUES(?,?,?,?,?)",
                (int(time.time()), cid, node_id, action, json.dumps(detail, sort_keys=True)),
            )

    def upsert_node(self, node: dict):
        with self._lock:
            self._conn.execute(
                """INSERT INTO nodes(node_id,name,role,capacity_class,enabled,auth_token,metadata_json)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(node_id) DO UPDATE SET
                     name=excluded.name, role=excluded.role,
                     capacity_class=excluded.capacity_class,
                     enabled=excluded.enabled,
                     auth_token=CASE WHEN excluded.auth_token<>'' THEN excluded.auth_token ELSE nodes.auth_token END,
                     metadata_json=excluded.metadata_json""",
                (
                    node["node_id"], node.get("name", ""), node.get("role", ""),
                    node.get("capacity_class", "unknown"), 1 if node.get("enabled", True) else 0,
                    node.get("replication_token", ""), json.dumps(node, sort_keys=True),
                ),
            )

    def heartbeat(self, node_id: str, total_bytes: int, used_bytes: int, now: int | None = None):
        now = int(now or time.time())
        with self._lock:
            cur = self._conn.execute(
                "UPDATE nodes SET last_seen=?, total_bytes=?, used_bytes=? WHERE node_id=?",
                (now, max(0, int(total_bytes)), max(0, int(used_bytes)), node_id),
            )
            if cur.rowcount != 1:
                raise KeyError(f"unknown node_id: {node_id}")

    def node(self, node_id: str):
        with self._lock:
            row = self._conn.execute("SELECT * FROM nodes WHERE node_id=?", (node_id,)).fetchone()
            return dict(row) if row else None

    def nodes(self):
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM nodes ORDER BY node_id")]

    def register_object(self, cid: str, size_bytes: int, n: int, m: int,
                        source_node: str | None = None, reference_id: str | None = None):
        """Register one logical reference to a CID.

        reference_id makes registration idempotent across HTTP retries. Reusing
        the same reference_id for another CID is rejected. Without reference_id
        the legacy incrementing behavior is retained for compatibility.
        """
        now = int(time.time())
        reference_id = str(reference_id or "").strip() or None
        created_reference = False
        with self.tx() as c:
            if reference_id:
                ref = c.execute("SELECT cid FROM object_refs WHERE reference_id=?", (reference_id,)).fetchone()
                if ref:
                    if ref[0] != cid:
                        raise ValueError("reference_id already belongs to a different CID")
                    row = c.execute("SELECT * FROM objects WHERE cid=?", (cid,)).fetchone()
                    if not row:
                        raise RuntimeError("object reference exists without object")
                else:
                    row = c.execute("SELECT * FROM objects WHERE cid=?", (cid,)).fetchone()
                    if row:
                        if int(row["size_bytes"]) != int(size_bytes):
                            raise ValueError("CID size mismatch")
                        c.execute(
                            "UPDATE objects SET refcount=refcount+1, replication_n=MAX(replication_n,?), committable_m=MAX(committable_m,?) WHERE cid=?",
                            (int(n), int(m), cid),
                        )
                    else:
                        c.execute(
                            "INSERT INTO objects(cid,size_bytes,created_at,replication_n,committable_m) VALUES(?,?,?,?,?)",
                            (cid, int(size_bytes), now, int(n), int(m)),
                        )
                    c.execute("INSERT INTO object_refs(reference_id,cid,created_at) VALUES(?,?,?)",
                              (reference_id, cid, now))
                    created_reference = True
            else:
                row = c.execute("SELECT * FROM objects WHERE cid=?", (cid,)).fetchone()
                if row:
                    if int(row["size_bytes"]) != int(size_bytes):
                        raise ValueError("CID size mismatch")
                    c.execute(
                        "UPDATE objects SET refcount=refcount+1, replication_n=MAX(replication_n,?), committable_m=MAX(committable_m,?) WHERE cid=?",
                        (int(n), int(m), cid),
                    )
                else:
                    c.execute(
                        "INSERT INTO objects(cid,size_bytes,created_at,replication_n,committable_m) VALUES(?,?,?,?,?)",
                        (cid, int(size_bytes), now, int(n), int(m)),
                    )
                created_reference = True

            if source_node:
                node = c.execute("SELECT capacity_class FROM nodes WHERE node_id=?", (source_node,)).fetchone()
                if not node:
                    raise KeyError(f"unknown source node: {source_node}")
                gen = c.execute("SELECT generation FROM objects WHERE cid=?", (cid,)).fetchone()[0]
                c.execute(
                    """INSERT INTO replicas(cid,node_id,desired,state,class_at_assignment,assigned_at,
                                              confirmed_at,last_verified_at,generation)
                       VALUES(?,?,1,'pinned',?,?,?,?,?)
                       ON CONFLICT(cid,node_id) DO UPDATE SET
                         desired=1,state='pinned',confirmed_at=excluded.confirmed_at,
                         last_verified_at=excluded.last_verified_at,generation=excluded.generation,last_error=''""",
                    (cid, source_node, node[0], now, now, now, gen),
                )
        self.audit("OBJECT_REGISTER", cid=cid, node_id=source_node or "",
                   size_bytes=int(size_bytes), n=n, m=m, reference_id=reference_id or "",
                   new_reference=created_reference)
        return {"cid": cid, "reference_id": reference_id, "new_reference": created_reference}

    def release_object(self, cid: str, reference_id: str | None = None) -> dict:
        """Release a logical reference. No UNPIN jobs are created in v2.1.3."""
        reference_id = str(reference_id or "").strip() or None
        with self.tx() as c:
            row = c.execute("SELECT refcount FROM objects WHERE cid=?", (cid,)).fetchone()
            if not row:
                return {"ok": False, "reason": "not-found"}
            if reference_id:
                ref = c.execute("SELECT cid FROM object_refs WHERE reference_id=?", (reference_id,)).fetchone()
                if not ref:
                    return {"ok": True, "cid": cid, "reference_id": reference_id,
                            "refcount": int(row[0]), "released": False, "unpin_scheduled": False}
                if ref[0] != cid:
                    raise ValueError("reference_id belongs to a different CID")
                c.execute("DELETE FROM object_refs WHERE reference_id=?", (reference_id,))
            new_ref = max(0, int(row[0]) - 1)
            c.execute("UPDATE objects SET refcount=? WHERE cid=?", (new_ref, cid))
        self.audit("OBJECT_RELEASE", cid=cid, refcount=new_ref, reference_id=reference_id or "")
        return {"ok": True, "cid": cid, "reference_id": reference_id, "refcount": new_ref,
                "released": True, "unpin_scheduled": False}

    def object(self, cid: str):
        with self._lock:
            row = self._conn.execute("SELECT * FROM objects WHERE cid=?", (cid,)).fetchone()
            if not row:
                return None
            out = dict(row)
            out["replicas"] = [dict(r) for r in self._conn.execute(
                "SELECT * FROM replicas WHERE cid=? ORDER BY node_id", (cid,)
            )]
            return out

    def objects(self):
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM objects ORDER BY created_at,cid")]

    def replica_rows(self, cid: str):
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM replicas WHERE cid=?", (cid,))]

    def placement_load(self):
        with self._lock:
            reserved = {}
            count = {}
            assigned = {}
            q = """SELECT r.node_id, COUNT(*) AS cnt, COALESCE(SUM(o.size_bytes),0) AS bytes
                   FROM replicas r JOIN objects o ON o.cid=r.cid
                   WHERE r.desired=1 AND r.state IN ('assigned','pinning','pinned','grace')
                   GROUP BY r.node_id"""
            for r in self._conn.execute(q):
                count[r["node_id"]] = int(r["cnt"])
                assigned[r["node_id"]] = int(r["bytes"])
                reserved[r["node_id"]] = int(r["bytes"])
            return reserved, count, assigned

    def create_pin_assignment(self, cid: str, node_id: str, capacity_class: str) -> str | None:
        now = int(time.time())
        with self.tx() as c:
            obj = c.execute("SELECT generation FROM objects WHERE cid=?", (cid,)).fetchone()
            if not obj:
                raise KeyError(cid)
            generation = int(obj[0])
            existing = c.execute("SELECT desired,state FROM replicas WHERE cid=? AND node_id=?", (cid, node_id)).fetchone()
            if existing and int(existing[0]) == 1 and existing[1] in {"assigned", "pinning", "pinned", "grace"}:
                return None
            c.execute(
                """INSERT INTO replicas(cid,node_id,desired,state,class_at_assignment,assigned_at,generation)
                   VALUES(?,?,1,'assigned',?,?,?)
                   ON CONFLICT(cid,node_id) DO UPDATE SET desired=1,state='assigned',
                     class_at_assignment=excluded.class_at_assignment,assigned_at=excluded.assigned_at,
                     generation=excluded.generation,last_error=''""",
                (cid, node_id, capacity_class, now, generation),
            )
            job_id = uuid.uuid4().hex
            active = c.execute(
                "SELECT job_id FROM jobs WHERE cid=? AND node_id=? AND operation='PIN' AND generation=? AND state IN ('pending','retry') LIMIT 1",
                (cid, node_id, generation),
            ).fetchone()
            if active:
                return None
            c.execute(
                """INSERT INTO jobs(job_id,cid,node_id,operation,generation,state,next_attempt_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,'pending',?,?,?)""",
                (job_id, cid, node_id, "PIN", generation, now, now, now),
            )
        self.audit("PIN_ASSIGNED", cid=cid, node_id=node_id, generation=generation)
        return job_id


    def create_verify_job(self, cid: str, node_id: str, generation: int) -> str | None:
        now = int(time.time())
        with self.tx() as c:
            active = c.execute(
                "SELECT job_id FROM jobs WHERE cid=? AND node_id=? AND operation='VERIFY' AND state IN ('pending','retry') LIMIT 1",
                (cid, node_id),
            ).fetchone()
            if active:
                return None
            job_id = uuid.uuid4().hex
            c.execute(
                """INSERT INTO jobs(job_id,cid,node_id,operation,generation,state,next_attempt_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,'pending',?,?,?)""",
                (job_id, cid, node_id, "VERIFY", int(generation), now, now, now),
            )
        return job_id

    def pending_jobs(self, node_id: str, now: int | None = None, limit: int = 32):
        """Lease due jobs to one agent poll.

        ``lease_until`` doubles as a delivery-attempt token. The agent echoes it
        in the result report, allowing the controller to reject stale/replayed
        reports from a previous delivery without adding a new schema column.
        """
        now = int(now or time.time())
        lease_until = now + 120
        with self.tx() as c:
            rows = c.execute(
                """SELECT j.*, o.size_bytes FROM jobs j JOIN objects o ON o.cid=j.cid
                   WHERE j.node_id=? AND j.state IN ('pending','retry')
                     AND j.next_attempt_at<=? AND (j.lease_until=0 OR j.lease_until<=?)
                   ORDER BY j.created_at LIMIT ?""",
                (node_id, now, now, int(limit)),
            ).fetchall()
            out = []
            for row in rows:
                c.execute(
                    "UPDATE jobs SET lease_until=?,updated_at=? WHERE job_id=?",
                    (lease_until, now, row["job_id"]),
                )
                item = dict(row)
                item["lease_until"] = lease_until
                out.append(item)
            return out

    def apply_job_report(self, node_id: str, report: dict):
        job_id = str(report.get("job_id") or "")
        state = str(report.get("state") or "")
        now = int(time.time())
        with self.tx() as c:
            job = c.execute("SELECT * FROM jobs WHERE job_id=? AND node_id=?", (job_id, node_id)).fetchone()
            if not job:
                raise KeyError(f"unknown job {job_id}")
            cid = job["cid"]
            generation = int(job["generation"])
            obj = c.execute("SELECT generation FROM objects WHERE cid=?", (cid,)).fetchone()
            if not obj or generation != int(obj[0]):
                c.execute("UPDATE jobs SET state='stale',updated_at=? WHERE job_id=?", (now, job_id))
                return
            reported_lease = int(report.get("lease_until") or 0)
            current_lease = int(job["lease_until"] or 0)

            # Production agents always echo the lease assigned by pending_jobs().
            # Once a report is applied we clear the lease, so a replay carrying
            # the old lease is rejected even if it arrives after the retry delay.
            if reported_lease:
                if not current_lease or reported_lease != current_lease:
                    return
            elif current_lease:
                # A leased job must not accept an unleased/legacy report.
                return

            # Legacy/direct callers used by unit tests may report without a lease.
            # Preserve compatibility while making an immediate duplicate failure
            # idempotent until the scheduled retry becomes due.
            if state == "failed" and not reported_lease and job["state"] == "retry" and int(job["next_attempt_at"] or 0) > now:
                return

            if job["state"] == "done" and state in {"pinned", "verified"}:
                return

            if state == "pinned":
                c.execute("UPDATE jobs SET state='done',lease_until=0,updated_at=?,last_error='' WHERE job_id=?", (now, job_id))
                c.execute(
                    """UPDATE replicas SET state='pinned',confirmed_at=CASE WHEN confirmed_at=0 THEN ? ELSE confirmed_at END,
                       last_verified_at=?,failure_since=0,last_error='' WHERE cid=? AND node_id=?""",
                    (now, now, cid, node_id),
                )
            elif state == "verified":
                c.execute("UPDATE jobs SET state='done',lease_until=0,updated_at=?,last_error='' WHERE job_id=?", (now, job_id))
                c.execute("UPDATE replicas SET state='pinned',last_verified_at=?,failure_since=0,last_error='' WHERE cid=? AND node_id=?",
                          (now, cid, node_id))
            elif state == "failed":
                attempts = int(job["attempts"]) + 1
                delays = [30, 60, 120, 300, 900, 1800]
                delay = delays[min(attempts - 1, len(delays) - 1)]
                err = str(report.get("error") or "pin failed")[:500]
                c.execute(
                    "UPDATE jobs SET state='retry',attempts=?,next_attempt_at=?,lease_until=0,updated_at=?,last_error=? WHERE job_id=?",
                    (attempts, now + delay, now, err, job_id),
                )
                c.execute(
                    "UPDATE replicas SET state='failed',failure_since=CASE WHEN failure_since=0 THEN ? ELSE failure_since END,last_error=? WHERE cid=? AND node_id=?",
                    (now, err, cid, node_id),
                )
            else:
                raise ValueError(f"invalid job state: {state}")
        self.audit("JOB_REPORT", cid=cid, node_id=node_id, job_id=job_id, state=state)

    def mark_replica_state(self, cid: str, node_id: str, state: str, error: str = ""):
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                """UPDATE replicas SET state=?,last_error=?,failure_since=CASE
                   WHEN ? IN ('missing','failed','grace') AND failure_since=0 THEN ?
                   WHEN ?='pinned' THEN 0 ELSE failure_since END
                   WHERE cid=? AND node_id=?""",
                (state, error[:500], state, now, state, cid, node_id),
            )

    def update_object_health(self, cid: str, state: str, reason: str):
        with self._lock:
            self._conn.execute("UPDATE objects SET state=?,reason=? WHERE cid=?", (state, reason[:200], cid))

    def stats(self):
        with self._lock:
            obj = dict(self._conn.execute(
                """SELECT COUNT(*) total,
                   SUM(CASE WHEN state='healthy' THEN 1 ELSE 0 END) healthy,
                   SUM(CASE WHEN state<>'healthy' THEN 1 ELSE 0 END) degraded,
                   COALESCE(SUM(size_bytes),0) logical_bytes
                   FROM objects WHERE refcount>0"""
            ).fetchone())
            reps = dict(self._conn.execute(
                """SELECT COALESCE(SUM(CASE WHEN r.state='pinned' AND r.class_at_assignment='committable' THEN o.size_bytes ELSE 0 END),0) committable_bytes,
                   COALESCE(SUM(CASE WHEN r.state='pinned' AND r.class_at_assignment='best-effort' THEN o.size_bytes ELSE 0 END),0) best_effort_bytes
                   FROM replicas r JOIN objects o ON o.cid=r.cid"""
            ).fetchone())
            jobs = dict(self._conn.execute(
                """SELECT COUNT(*) total,
                   SUM(CASE WHEN state IN ('pending','retry') THEN 1 ELSE 0 END) pending,
                   SUM(CASE WHEN state='done' THEN 1 ELSE 0 END) done
                   FROM jobs"""
            ).fetchone())
            return {"objects": obj, "replicas": reps, "jobs": jobs}
