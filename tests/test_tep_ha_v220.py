import json
import sys
import tempfile
import unittest
from pathlib import Path

HA_DIR = Path(__file__).resolve().parents[1] / "ha"
if str(HA_DIR) not in sys.path:
    sys.path.insert(0, str(HA_DIR))

import hashburst_ha_agent as base  # noqa: E402
import hashburst_ha_agent_v220 as v220  # noqa: E402
import hashburst_ha_readiness as readiness  # noqa: E402


class FakeController:
    def __init__(self):
        self.desired = "standby"

    def set_primary(self):
        self.desired = "primary"

    def set_standby(self):
        self.desired = "standby"


class AlwaysEligible:
    def check(self):
        return True, []


class AdvancingTransport:
    def __init__(self, clock, step=2.0):
        self.clock = clock
        self.step = step

    def rpc(self, target, payload):
        self.clock[0] += self.step
        return {
            "granted": True,
            "term": int(payload.get("term", 0)),
            "holder": payload.get("candidate") or payload.get("holder") or "",
            "lease_ms": int(payload.get("lease_ms", 12000)),
        }


def write_config(root: Path, node_id: str, roles, voters, candidates):
    data = {
        "cluster_id": "hashburst-production",
        "node_id": node_id,
        "roles": list(roles),
        "voters": list(voters),
        "candidates": candidates,
        "lease_seconds": 12,
        "loop_seconds": 2,
        "rpc_timeout_seconds": 1,
        "armed": False,
        "primary_services": [],
        "required_services": [],
        "health_urls": [],
        "replication_state_file": "",
        "state_file": str(root / f"{node_id}.state.json"),
        "guard_file": str(root / f"{node_id}.guard.json"),
        "bind_port": 47780,
    }
    path = root / f"{node_id}.json"
    path.write_text(json.dumps(data))
    return path, data


class V220HardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.original_boot_seconds = base.boot_seconds

    def tearDown(self):
        base.boot_seconds = self.original_boot_seconds
        self.tmp.cleanup()

    def test_voter_restart_blocks_different_candidate_for_one_lease(self):
        clock = [100.0]
        base.boot_seconds = lambda: clock[0]
        voters = ["voter-1"]
        candidates = [
            {"node_id": "master-node", "priority": 10},
            {"node_id": "hashburst-dr1", "priority": 20},
        ]
        path, raw = write_config(self.root, "voter-1", {"voter", "observer"}, voters, candidates)
        config = base.Config.load(path)
        first = v220.LeaseEngine(config, raw_config=raw, controller=FakeController())
        granted = first.handle_tep(
            {"node_id": "master-node", "peer_id": "peer-master"},
            {
                "op": "vote_request",
                "cluster_id": "hashburst-production",
                "candidate": "master-node",
                "priority": 10,
                "term": 1,
                "lease_ms": 12000,
            },
        )
        self.assertTrue(granted["granted"])

        restarted = v220.LeaseEngine(config, raw_config=raw, controller=FakeController())
        denied = restarted.handle_tep(
            {"node_id": "hashburst-dr1", "peer_id": "peer-dr1"},
            {
                "op": "vote_request",
                "cluster_id": "hashburst-production",
                "candidate": "hashburst-dr1",
                "priority": 20,
                "term": 2,
                "lease_ms": 12000,
            },
        )
        self.assertFalse(denied["granted"])
        self.assertEqual(denied["reason"], "restart_lease_guard")
        clock[0] += 12.1
        after_guard = restarted.handle_tep(
            {"node_id": "hashburst-dr1", "peer_id": "peer-dr1"},
            {
                "op": "vote_request",
                "cluster_id": "hashburst-production",
                "candidate": "hashburst-dr1",
                "priority": 20,
                "term": 2,
                "lease_ms": 12000,
            },
        )
        self.assertTrue(after_guard["granted"])

    def test_campaign_deadline_is_anchored_before_sequential_rpc(self):
        clock = [100.0]
        base.boot_seconds = lambda: clock[0]
        voters = ["v1", "v2", "v3"]
        candidates = [{"node_id": "candidate", "priority": 10}]
        path, raw = write_config(self.root, "candidate", {"candidate"}, voters, candidates)
        config = base.Config.load(path)
        controller = FakeController()
        engine = v220.LeaseEngine(
            config,
            raw_config=raw,
            transport=AdvancingTransport(clock, step=2.0),
            controller=controller,
            eligibility=AlwaysEligible(),
        )
        self.assertTrue(engine._campaign(1))
        self.assertEqual(engine._leader_deadline, 112.0)
        self.assertEqual(clock[0], 106.0)
        self.assertEqual(controller.desired, "primary")

    def test_monero_sync_is_an_eligibility_gate(self):
        candidates = [{"node_id": "candidate", "priority": 10}]
        path, raw = write_config(self.root, "candidate", {"candidate"}, ["v1"], candidates)
        raw["monero_checks"] = [
            {"name": "mainnet", "url": "http://127.0.0.1:18081/json_rpc", "nettype": "mainnet"}
        ]
        config = base.Config.load(path)
        checker = v220.EligibilityChecker(config, raw)
        checker._json_request = lambda *args, **kwargs: {
            "result": {
                "nettype": "mainnet",
                "height": 100,
                "target_height": 200,
                "synchronized": False,
                "busy_syncing": True,
                "offline": False,
            }
        }
        ok, reasons = checker.check()
        self.assertFalse(ok)
        self.assertIn("monero_not_synchronized:mainnet", reasons)
        self.assertIn("monero_busy_syncing:mainnet", reasons)

    def test_reconstructable_readiness_does_not_invent_replication_lag(self):
        required = self.root / "master_node.py"
        required.write_text("print('ok')\n")
        state = readiness.build_state({"readiness_files": [{"path": str(required)}]})
        self.assertTrue(state["ready"])
        self.assertEqual(state["mode"], "reconstructable")
        self.assertNotIn("lag_seconds", state)


if __name__ == "__main__":
    unittest.main()
