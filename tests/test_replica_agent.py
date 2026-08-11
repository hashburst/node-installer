import sys
import unittest
from pathlib import Path
from unittest import mock

# Agent imports hb_ipfs as a top-level module, matching the systemd PYTHONPATH.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hbfiles"))

import replication.hb_replica_agent as agentmod
import hb_ipfs


class FakeIPFS:
    def __init__(self, *, confirm=True, pinned=True, pin_error=None):
        self.confirm = confirm
        self.pinned = pinned
        self.pin_error = pin_error
        self.pin_calls = []
        self.unpin_calls = []

    def pin(self, cid):
        self.pin_calls.append(cid)
        if self.pin_error:
            raise self.pin_error
        return True

    def is_pinned(self, cid):
        return self.confirm if cid in self.pin_calls else self.pinned

    def unpin(self, cid):
        self.unpin_calls.append(cid)
        return True

    def repo_stat(self):
        return {"RepoSize": 10, "StorageMax": 100}


class ReplicaAgentTests(unittest.TestCase):
    def make_agent(self, fake):
        with mock.patch.object(agentmod.hb_ipfs, "IPFSClient", return_value=fake):
            return agentmod.Agent("http://controller", "node-1", "token", "http://127.0.0.1:5011")

    def test_pin_job_requires_pin_ls_confirmation(self):
        fake = FakeIPFS(confirm=True)
        agent = self.make_agent(fake)
        report = agent.execute_job({"job_id": "j1", "cid": "bafy", "operation": "PIN", "lease_until": 12345})
        self.assertEqual({"job_id": "j1", "state": "pinned", "lease_until": 12345}, report)
        self.assertEqual(["bafy"], fake.pin_calls)

    def test_pin_job_failure_is_reported_and_not_claimed_pinned(self):
        fake = FakeIPFS(confirm=False)
        agent = self.make_agent(fake)
        report = agent.execute_job({"job_id": "j2", "cid": "bafy", "operation": "PIN"})
        self.assertEqual("failed", report["state"])
        self.assertIn("pin/ls", report["error"])

    def test_kubo_pin_exception_is_reported(self):
        fake = FakeIPFS(pin_error=hb_ipfs.IPFSError("mock unavailable"))
        agent = self.make_agent(fake)
        report = agent.execute_job({"job_id": "j3", "cid": "bafy", "operation": "PIN"})
        self.assertEqual("failed", report["state"])
        self.assertIn("mock unavailable", report["error"])

    def test_verify_job_succeeds_only_when_recursive_pin_exists(self):
        fake = FakeIPFS(pinned=True)
        agent = self.make_agent(fake)
        report = agent.execute_job({"job_id": "j4", "cid": "bafy", "operation": "VERIFY"})
        self.assertEqual("verified", report["state"])

        fake2 = FakeIPFS(pinned=False)
        agent2 = self.make_agent(fake2)
        report2 = agent2.execute_job({"job_id": "j5", "cid": "bafy", "operation": "VERIFY"})
        self.assertEqual("failed", report2["state"])

    def test_unpin_is_disabled_by_default(self):
        fake = FakeIPFS()
        agent = self.make_agent(fake)
        with mock.patch.object(agentmod, "ALLOW_UNPIN", False):
            report = agent.execute_job({"job_id": "j6", "cid": "bafy", "operation": "UNPIN"})
        self.assertEqual("failed", report["state"])
        self.assertEqual([], fake.unpin_calls)


if __name__ == "__main__":
    unittest.main()
