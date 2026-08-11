import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from replication.hb_replication_policy import PlacementPolicy
from replication.hb_replication_v214_recovery import ReplicationDBV214Recovery
import replication.hb_replication_controller_v214 as ctrlmod

GiB = 1024 ** 3
NOW = 2_000_000_000


def node(node_id, cls="committable"):
    return {
        "node_id": node_id,
        "name": node_id,
        "role": "primary" if node_id == "p1" else "secondary",
        "capacity_class": cls,
        "enabled": True,
        "replication_token": "token",
        "failure_domain": "domain-" + node_id,
    }


class ControllerV214Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.registry = root / "storage-nodes.json"
        self.registry.write_text(json.dumps({"nodes": [node("p1"), node("p2"), node("e1", "best-effort"), node("e2", "best-effort")]}))
        self.db = ReplicationDBV214Recovery(str(root / "controller.sqlite3"))
        self.addCleanup(self.db._conn.close)
        self.ctl = ctrlmod.ControllerV214(
            self.db, str(self.registry), PlacementPolicy(3, 2, safety_margin_bytes=0),
            mode="full", unpin_enabled=True,
        )
        self.ctl.sync_registry()
        for nid in ("p1", "p2", "e1", "e2"):
            self.db.heartbeat(nid, 100 * GiB, 0, now=NOW)

    def test_release_reports_cleanup_pending_not_immediate_unpin(self):
        self.db.register_object("bafy-release", GiB, 3, 2, source_node="p1", reference_id="file-1")
        with mock.patch.object(ctrlmod.time, "time", return_value=NOW):
            out = self.ctl.release_object({"cid": "bafy-release", "reference_id": "file-1"})
        self.assertTrue(out["cleanup_pending"])
        self.assertFalse(out["unpin_scheduled"])
        self.assertGreater(out["unpin_not_before"], 0)
        jobs = self.db._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE cid='bafy-release' AND operation='UNPIN'"
        ).fetchone()[0]
        self.assertEqual(0, jobs)

    def test_repeated_trim_does_not_schedule_beyond_excess(self):
        cid = "bafy-trim"
        self.db.register_object(cid, GiB, 3, 2, source_node="p1", reference_id="file-1")
        generation = self.db.object(cid)["generation"]
        now = NOW
        # Simulate four confirmed desired replicas for an N=3 object.
        for nid, cls in (("p2", "committable"), ("e1", "best-effort"), ("e2", "best-effort")):
            self.db._conn.execute(
                """INSERT OR REPLACE INTO replicas(cid,node_id,desired,state,class_at_assignment,
                   assigned_at,confirmed_at,last_verified_at,generation,last_error)
                   VALUES(?,?,1,'pinned',?,?,?,?,?,'')""",
                (cid, nid, cls, now, now, now, generation),
            )
        obj = self.db.object(cid)
        self.ctl._plan_trim(obj)
        first = self.db._conn.execute(
            """SELECT COUNT(*) FROM jobs WHERE cid=? AND operation='UNPIN'
               AND reason='trim-extra-replica' AND state IN ('pending','retry','authorized')""",
            (cid,),
        ).fetchone()[0]
        self.assertEqual(1, first)
        self.ctl._plan_trim(self.db.object(cid))
        second = self.db._conn.execute(
            """SELECT COUNT(*) FROM jobs WHERE cid=? AND operation='UNPIN'
               AND reason='trim-extra-replica' AND state IN ('pending','retry','authorized')""",
            (cid,),
        ).fetchone()[0]
        self.assertEqual(1, second)

    def test_active_trim_preserves_committable_floor(self):
        cid = "bafy-floor"
        self.db.register_object(cid, GiB, 3, 2, source_node="p1", reference_id="file-1")
        generation = self.db.object(cid)["generation"]
        for nid, cls in (("p2", "committable"), ("e1", "best-effort"), ("e2", "best-effort")):
            self.db._conn.execute(
                """INSERT OR REPLACE INTO replicas(cid,node_id,desired,state,class_at_assignment,
                   assigned_at,confirmed_at,last_verified_at,generation,last_error)
                   VALUES(?,?,1,'pinned',?,?,?,?,?,'')""",
                (cid, nid, cls, NOW, NOW, NOW, generation),
            )
        self.ctl._plan_trim(self.db.object(cid))
        row = self.db._conn.execute(
            "SELECT node_id FROM jobs WHERE cid=? AND operation='UNPIN' AND reason='trim-extra-replica'",
            (cid,),
        ).fetchone()
        self.assertIn(row[0], {"e1", "e2"})


if __name__ == "__main__":
    unittest.main()
