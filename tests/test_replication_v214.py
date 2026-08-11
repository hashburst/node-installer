import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hbfiles"))

from replication.hb_replication_v214_db import LifecycleConflict, ReplicationDBV214
import replication.hb_replica_agent_v214 as agentmod

GiB = 1024 ** 3


def node(node_id="p1"):
    return {
        "node_id": node_id,
        "name": node_id,
        "role": "primary",
        "capacity_class": "committable",
        "enabled": True,
        "replication_token": "token",
        "failure_domain": "provider-a/region-a/rack-a",
    }


class LifecycleDBTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = ReplicationDBV214(str(Path(self.tmp.name) / "controller.sqlite3"))
        self.addCleanup(self.db._conn.close)
        self.db.upsert_node(node())

    def test_additive_schema_migration_fields_exist(self):
        self.assertIn("released_at", self.db._columns("objects"))
        self.assertIn("released_at", self.db._columns("object_refs"))
        self.assertIn("authorized_at", self.db._columns("jobs"))
        self.assertIn("failure_domain", self.db._columns("nodes"))

    def test_final_release_increments_generation_and_tombstones_reference(self):
        self.db.register_object("bafy-a", GiB, 3, 2, source_node="p1", reference_id="file-1")
        before = self.db.object("bafy-a")["generation"]
        out = self.db.release_object("bafy-a", "file-1", request_id="delete-1", actor="test")
        self.assertTrue(out["final_release"])
        self.assertEqual(0, out["refcount"])
        self.assertEqual(before + 1, out["generation"])
        ref = self.db._conn.execute(
            "SELECT released_at,release_request_id FROM object_refs WHERE reference_id='file-1'"
        ).fetchone()
        self.assertGreater(ref[0], 0)
        self.assertEqual("delete-1", ref[1])

    def test_release_retry_is_idempotent(self):
        self.db.register_object("bafy-a", GiB, 3, 2, source_node="p1", reference_id="file-1")
        first = self.db.release_object("bafy-a", "file-1", request_id="delete-1")
        replay = self.db.release_object("bafy-a", "file-1", request_id="delete-1")
        self.assertTrue(first["released"])
        self.assertFalse(replay["released"])
        self.assertEqual(0, replay["refcount"])

    def test_unpin_authorization_requires_zero_refcount_generation_and_lease(self):
        self.db.register_object("bafy-a", GiB, 3, 2, source_node="p1", reference_id="file-1")
        out = self.db.release_object("bafy-a", "file-1")
        jid = self.db.create_unpin_job("bafy-a", "p1", out["generation"], "final-release")
        leased = self.db.pending_jobs("p1", now=2_000_000_000, allowed_operations={"UNPIN"})
        self.assertEqual([jid], [j["job_id"] for j in leased])
        denied = self.db.authorize_unpin("p1", jid, leased[0]["lease_until"] + 1)
        self.assertFalse(denied["authorized"])

    def test_authorized_unpin_blocks_reregister_until_outcome_known(self):
        self.db.register_object("bafy-a", GiB, 3, 2, source_node="p1", reference_id="file-1")
        out = self.db.release_object("bafy-a", "file-1")
        jid = self.db.create_unpin_job("bafy-a", "p1", out["generation"], "final-release")
        leased = self.db.pending_jobs("p1", now=2_000_000_000, allowed_operations={"UNPIN"})[0]
        auth = self.db.authorize_unpin("p1", jid, leased["lease_until"])
        self.assertTrue(auth["authorized"])
        with self.assertRaises(LifecycleConflict):
            self.db.register_object("bafy-a", GiB, 3, 2, source_node="p1", reference_id="file-2")


class FakeIPFS:
    def __init__(self):
        self.unpin_calls = []
        self.pinned = True
    def unpin(self, cid):
        self.unpin_calls.append(cid)
        self.pinned = False
    def is_pinned(self, cid):
        return self.pinned
    def repo_stat(self):
        return {"RepoSize": 1, "StorageMax": 100}


class AgentUnpinTests(unittest.TestCase):
    def make_agent(self, fake):
        with mock.patch.object(agentmod.base.hb_ipfs, "IPFSClient", return_value=fake):
            return agentmod.AgentV214("http://controller", "p1", "token", "http://127.0.0.1:5011")

    def test_unpin_is_denied_by_local_gate_by_default(self):
        fake = FakeIPFS()
        agent = self.make_agent(fake)
        with mock.patch.object(agentmod, "ALLOW_UNPIN", False):
            report = agent.execute_job({"job_id": "j1", "cid": "bafy", "operation": "UNPIN", "lease_until": 10})
        self.assertEqual("failed", report["state"])
        self.assertEqual([], fake.unpin_calls)

    def test_unpin_requires_controller_authorization(self):
        fake = FakeIPFS()
        agent = self.make_agent(fake)
        with mock.patch.object(agentmod, "ALLOW_UNPIN", True), \
             mock.patch.object(agent, "_request", return_value={"authorized": False, "reason": "stale"}):
            report = agent.execute_job({"job_id": "j1", "cid": "bafy", "operation": "UNPIN", "lease_until": 10})
        self.assertEqual("failed", report["state"])
        self.assertEqual([], fake.unpin_calls)

    def test_authorized_unpin_confirms_pin_removed(self):
        fake = FakeIPFS()
        agent = self.make_agent(fake)
        with mock.patch.object(agentmod, "ALLOW_UNPIN", True), \
             mock.patch.object(agent, "_request", return_value={"authorized": True, "cid": "bafy", "generation": 2}):
            report = agent.execute_job({"job_id": "j1", "cid": "bafy", "operation": "UNPIN", "lease_until": 10})
        self.assertEqual("unpinned", report["state"])
        self.assertEqual(["bafy"], fake.unpin_calls)


if __name__ == "__main__":
    unittest.main()
