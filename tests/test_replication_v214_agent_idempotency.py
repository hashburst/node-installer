import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hbfiles"))

import replication.hb_replica_agent_v214 as agentmod


class FakeIPFS:
    def __init__(self):
        self.unpin_calls = []
    def is_pinned(self, cid):
        return False
    def unpin(self, cid):
        self.unpin_calls.append(cid)
    def repo_stat(self):
        return {"RepoSize": 1, "StorageMax": 100}


class AgentIdempotencyTests(unittest.TestCase):
    def test_authorized_unpin_is_success_when_pin_already_absent(self):
        fake = FakeIPFS()
        with mock.patch.object(agentmod.base.hb_ipfs, "IPFSClient", return_value=fake):
            agent = agentmod.AgentV214("http://controller", "p1", "token", "http://127.0.0.1:5011")
        with mock.patch.object(agentmod, "ALLOW_UNPIN", True), \
             mock.patch.object(agent, "_request", return_value={"authorized": True, "cid": "bafy", "generation": 2}):
            report = agent.execute_job({"job_id": "j1", "cid": "bafy", "operation": "UNPIN", "lease_until": 10})
        self.assertEqual("unpinned", report["state"])
        self.assertEqual([], fake.unpin_calls)


if __name__ == "__main__":
    unittest.main()
