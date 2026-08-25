import importlib.util
import json
import tempfile
import unittest
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "ha" / "hashburst_ha_agent.py"
spec = importlib.util.spec_from_file_location("hashburst_ha_agent", MODULE_PATH)
ha = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ha
assert spec.loader is not None
spec.loader.exec_module(ha)


class FakeController:
    def __init__(self):
        self.desired = "standby"
        self.primary_calls = 0
        self.standby_calls = 0

    def set_primary(self):
        self.desired = "primary"
        self.primary_calls += 1

    def set_standby(self):
        self.desired = "standby"
        self.standby_calls += 1


class FakeEligibility:
    def __init__(self, ready=True):
        self.ready = ready

    def check(self):
        return (self.ready, [] if self.ready else ["not_ready"])


class NetworkTransport:
    def __init__(self, source_node, network):
        self.source_node = source_node
        self.network = network

    def rpc(self, target, payload):
        engine = self.network[target]
        source = {"node_id": self.source_node, "peer_id": "peer-" + self.source_node}
        return engine.handle_tep(source, payload)


def make_config(root: Path, node_id: str, roles, *, armed=False):
    data = {
        "cluster_id": "hashburst-production",
        "node_id": node_id,
        "roles": list(roles),
        "voters": ["blockchainapi.one", "mlmultiservices.com", "hashburst-dr1"],
        "candidates": [
            {"node_id": "master-node", "priority": 10},
            {"node_id": "hashburst-dr1", "priority": 20},
        ],
        "lease_seconds": 12,
        "loop_seconds": 2,
        "rpc_timeout_seconds": 1,
        "armed": armed,
        "primary_services": [],
        "required_services": [],
        "health_urls": [],
        "state_file": str(root / f"{node_id}.state.json"),
        "guard_file": str(root / f"{node_id}.guard.json"),
        "bind_port": 47780,
    }
    path = root / f"{node_id}.json"
    path.write_text(json.dumps(data))
    return ha.Config.load(path)


class TepHaAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.network = {}
        self.controllers = {}

        for node_id, roles in [
            ("blockchainapi.one", {"voter", "observer"}),
            ("mlmultiservices.com", {"voter", "observer"}),
            ("hashburst-dr1", {"voter", "candidate"}),
            ("master-node", {"candidate"}),
        ]:
            config = make_config(self.root, node_id, roles)
            controller = FakeController()
            self.controllers[node_id] = controller
            engine = ha.LeaseEngine(
                config,
                transport=NetworkTransport(node_id, self.network),
                controller=controller,
                eligibility=FakeEligibility(True),
            )
            self.network[node_id] = engine

    def tearDown(self):
        self.tmp.cleanup()

    def test_master_wins_initial_quorum(self):
        master = self.network["master-node"]
        master.run_once()
        status = master.local_status()
        self.assertEqual(status["local_role"], "primary")
        self.assertEqual(status["leader_term"], 1)
        voter_statuses = [
            self.network[v]._voter_status()
            for v in ("blockchainapi.one", "mlmultiservices.com", "hashburst-dr1")
        ]
        self.assertGreaterEqual(sum(s["holder"] == "master-node" for s in voter_statuses), 2)

    def test_dr1_takes_over_after_master_disappears(self):
        master = self.network["master-node"]
        dr1 = self.network["hashburst-dr1"]
        master.run_once()
        dr1.run_once()
        self.assertEqual(dr1.local_status()["local_role"], "standby")

        for voter in ("blockchainapi.one", "mlmultiservices.com", "hashburst-dr1"):
            self.network[voter]._voter_deadline = 0.0
            self.network[voter]._voter_holder = ""
        master._leader_deadline = 0.0
        self.network.pop("master-node")

        dr1.run_once()
        status = dr1.local_status()
        self.assertEqual(status["local_role"], "primary")
        self.assertEqual(status["leader_term"], 2)
        self.assertEqual(status["cluster_view"]["quorum"], 2)

    def test_same_term_cannot_be_granted_to_two_candidates(self):
        voter = self.network["blockchainapi.one"]
        grant = voter.handle_tep(
            {"node_id": "master-node", "peer_id": "peer-master"},
            {
                "op": "vote_request",
                "cluster_id": "hashburst-production",
                "candidate": "master-node",
                "priority": 10,
                "term": 4,
                "lease_ms": 12000,
            },
        )
        self.assertTrue(grant["granted"])
        voter._voter_deadline = 0.0
        voter._voter_holder = ""
        denied = voter.handle_tep(
            {"node_id": "hashburst-dr1", "peer_id": "peer-dr1"},
            {
                "op": "vote_request",
                "cluster_id": "hashburst-production",
                "candidate": "hashburst-dr1",
                "priority": 20,
                "term": 4,
                "lease_ms": 12000,
            },
        )
        self.assertFalse(denied["granted"])
        self.assertEqual(denied["reason"], "already_voted")

    def test_active_lease_blocks_higher_term_competitor(self):
        voter = self.network["blockchainapi.one"]
        first = voter.handle_tep(
            {"node_id": "master-node", "peer_id": "peer-master"},
            {
                "op": "vote_request",
                "cluster_id": "hashburst-production",
                "candidate": "master-node",
                "priority": 10,
                "term": 3,
                "lease_ms": 12000,
            },
        )
        self.assertTrue(first["granted"])
        denied = voter.handle_tep(
            {"node_id": "hashburst-dr1", "peer_id": "peer-dr1"},
            {
                "op": "vote_request",
                "cluster_id": "hashburst-production",
                "candidate": "hashburst-dr1",
                "priority": 20,
                "term": 4,
                "lease_ms": 12000,
            },
        )
        self.assertFalse(denied["granted"])
        self.assertEqual(denied["reason"], "active_lease")
        self.assertEqual(denied["term"], 3)

    def test_candidate_cannot_impersonate_another_source(self):
        voter = self.network["blockchainapi.one"]
        with self.assertRaises(ha.HaError) as ctx:
            voter.handle_tep(
                {"node_id": "hashburst-dr1", "peer_id": "peer-dr1"},
                {
                    "op": "vote_request",
                    "cluster_id": "hashburst-production",
                    "candidate": "master-node",
                    "priority": 10,
                    "term": 1,
                    "lease_ms": 12000,
                },
            )
        self.assertEqual(ctx.exception.code, "identity_mismatch")

    def test_term_is_persisted(self):
        voter = self.network["mlmultiservices.com"]
        voter.handle_tep(
            {"node_id": "master-node", "peer_id": "peer-master"},
            {
                "op": "vote_request",
                "cluster_id": "hashburst-production",
                "candidate": "master-node",
                "priority": 10,
                "term": 7,
                "lease_ms": 12000,
            },
        )
        state = json.loads(voter.config.state_file.read_text())
        self.assertEqual(state, {"term": 7, "voted_for": "master-node"})


if __name__ == "__main__":
    unittest.main()
