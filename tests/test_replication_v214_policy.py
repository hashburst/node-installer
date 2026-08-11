import unittest

from replication.hb_replication_policy import PlacementPolicy, choose_placements, node_failure_domain

GiB = 1024 ** 3


def n(node_id, domain, cls="committable", free_gib=100):
    return {
        "node_id": node_id,
        "capacity_class": cls,
        "enabled": True,
        "online": True,
        "total_bytes": free_gib * GiB,
        "used_bytes": 0,
        "failure_domain": domain,
    }


class FailureDomainPlacementTests(unittest.TestCase):
    def test_explicit_domain_is_used(self):
        self.assertEqual("provider-a/region-a", node_failure_domain(n("p1", "provider-a/region-a")))

    def test_distinct_domain_is_preferred_when_available(self):
        nodes = [
            n("p1", "domain-a", free_gib=100),
            n("p2", "domain-a", free_gib=200),
            n("p3", "domain-b", free_gib=100),
        ]
        picks = choose_placements(
            "bafy-domain", GiB, nodes, {"p1"},
            current_committable_count=1, current_total_count=1,
            reserved_by_node={}, desired_count_by_node={}, assigned_bytes_by_node={},
            policy=PlacementPolicy(replication_n=2, committable_m=2, safety_margin_bytes=0),
        )
        self.assertEqual(["p3"], [p["node_id"] for p in picks])

    def test_same_domain_fallback_preserves_availability(self):
        nodes = [n("p1", "domain-a"), n("p2", "domain-a")]
        picks = choose_placements(
            "bafy-domain", GiB, nodes, {"p1"},
            current_committable_count=1, current_total_count=1,
            reserved_by_node={}, desired_count_by_node={}, assigned_bytes_by_node={},
            policy=PlacementPolicy(replication_n=2, committable_m=2, safety_margin_bytes=0),
        )
        self.assertEqual(["p2"], [p["node_id"] for p in picks])


if __name__ == "__main__":
    unittest.main()
