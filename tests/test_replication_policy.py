import unittest

from replication.hb_replication_policy import (
    BEST_EFFORT,
    COMMITTABLE,
    PlacementPolicy,
    choose_placements,
)

GiB = 1024 ** 3


def node(node_id, cls, free_gib=10, used_gib=0, online=True, enabled=True, stability=0):
    total = free_gib * GiB
    used = used_gib * GiB
    return {
        "node_id": node_id,
        "capacity_class": cls,
        "total_bytes": total,
        "used_bytes": used,
        "online": online,
        "enabled": enabled,
        "stability_score": stability,
        "failure_penalty": 0,
    }


class PlacementPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = PlacementPolicy(
            replication_n=3,
            committable_m=2,
            prefer_edge_for_extra=True,
            safety_margin_bytes=0,
        )

    def choose(self, nodes, *, size=GiB, current=None, comm=0, total=0):
        return choose_placements(
            "bafy-test-cid",
            size,
            nodes,
            set(current or []),
            comm,
            total,
            {},
            {},
            {},
            self.policy,
        )

    def test_n3_m2_requires_two_committable_and_prefers_edge_for_extra(self):
        picks = self.choose([
            node("primary", COMMITTABLE, 100),
            node("secondary", COMMITTABLE, 90),
            node("edge-1", BEST_EFFORT, 80),
            node("edge-2", BEST_EFFORT, 70),
        ])
        self.assertEqual(3, len(picks))
        classes = [p["capacity_class"] for p in picks]
        self.assertEqual(2, classes.count(COMMITTABLE))
        self.assertEqual(1, classes.count(BEST_EFFORT))

    def test_existing_committable_replica_counts_toward_m(self):
        picks = self.choose([
            node("secondary", COMMITTABLE, 90),
            node("edge-1", BEST_EFFORT, 80),
            node("edge-2", BEST_EFFORT, 70),
        ], current={"primary"}, comm=1, total=1)
        self.assertEqual(2, len(picks))
        self.assertEqual(COMMITTABLE, picks[0]["capacity_class"])
        self.assertEqual(BEST_EFFORT, picks[1]["capacity_class"])

    def test_edge_never_satisfies_committable_requirement(self):
        picks = self.choose([
            node("edge-1", BEST_EFFORT, 80),
            node("edge-2", BEST_EFFORT, 70),
            node("edge-3", BEST_EFFORT, 60),
        ])
        # N can be approached with edge capacity, but M=2 cannot be satisfied.
        self.assertEqual(3, len(picks))
        self.assertTrue(all(p["capacity_class"] == BEST_EFFORT for p in picks))

    def test_full_or_too_small_nodes_are_excluded(self):
        picks = self.choose([
            node("primary", COMMITTABLE, 100),
            node("secondary-small", COMMITTABLE, 0),
            node("secondary-ok", COMMITTABLE, 2),
            node("edge-small", BEST_EFFORT, 0),
            node("edge-ok", BEST_EFFORT, 2),
        ], size=GiB)
        ids = {p["node_id"] for p in picks}
        self.assertIn("primary", ids)
        self.assertIn("secondary-ok", ids)
        self.assertIn("edge-ok", ids)
        self.assertNotIn("secondary-small", ids)
        self.assertNotIn("edge-small", ids)

    def test_offline_disabled_and_unknown_nodes_are_excluded(self):
        picks = self.choose([
            node("primary", COMMITTABLE, 100),
            node("secondary-offline", COMMITTABLE, 100, online=False),
            node("edge-disabled", BEST_EFFORT, 100, enabled=False),
            node("unknown", "unknown", 100),
            node("secondary-ok", COMMITTABLE, 100),
            node("edge-ok", BEST_EFFORT, 100),
        ])
        ids = {p["node_id"] for p in picks}
        self.assertEqual({"primary", "secondary-ok", "edge-ok"}, ids)

    def test_no_duplicate_assignment_to_existing_node(self):
        picks = self.choose([
            node("primary", COMMITTABLE, 100),
            node("secondary", COMMITTABLE, 100),
            node("edge", BEST_EFFORT, 100),
        ], current={"primary"}, comm=1, total=1)
        self.assertNotIn("primary", {p["node_id"] for p in picks})

    def test_deterministic_placement(self):
        nodes = [
            node("p1", COMMITTABLE, 100),
            node("p2", COMMITTABLE, 100),
            node("e1", BEST_EFFORT, 100),
            node("e2", BEST_EFFORT, 100),
        ]
        first = [p["node_id"] for p in self.choose(nodes)]
        second = [p["node_id"] for p in self.choose(list(reversed(nodes)))]
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
