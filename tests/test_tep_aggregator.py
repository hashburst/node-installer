import json
import os
import tempfile
import time
import unittest
from unittest import mock

from aggregator import hb_aggregator


def summary(node_id, role, total, used, **extra):
    out = {
        "available": True,
        "node_id": node_id,
        "role": role,
        "capacity_total_gb": total,
        "used_gb": used,
        "timestamp": int(time.time()),
        "capacity_source": "test",
    }
    out.update(extra)
    return out


class AggregatorTransportTests(unittest.TestCase):
    def setUp(self):
        self.old_nodes = hb_aggregator.NODES_FILE

    def tearDown(self):
        hb_aggregator.NODES_FILE = self.old_nodes

    def _config(self, nodes):
        f = tempfile.NamedTemporaryFile("w", delete=False)
        json.dump({"nodes": nodes}, f); f.close()
        self.addCleanup(lambda: os.path.exists(f.name) and os.unlink(f.name))
        hb_aggregator.NODES_FILE = f.name
        return f.name

    def test_legacy_url_defaults_to_direct(self):
        self._config([{"name":"p", "url":"http://127.0.0.1:1", "role":"primary"}])
        nodes = hb_aggregator.discover_nodes()
        self.assertEqual(len(nodes), 1)
        self.assertEqual(hb_aggregator._transport(nodes[0]), "direct")

    def test_tep_node_discovered_without_url(self):
        self._config([{"name":"node-7", "transport":"tep", "tep_peer_id":"peer-node7",
                       "role":"edge", "capacity_class":"best-effort"}])
        nodes = hb_aggregator.discover_nodes()
        self.assertEqual(len(nodes), 1)
        self.assertNotIn("url", nodes[0])

    def test_unknown_transport_fail_closed(self):
        self._config([{"name":"x", "transport":"magic", "url":"http://x", "tep_peer_id":"p"}])
        self.assertEqual(hb_aggregator.discover_nodes(), [])

    def test_tep_success_marks_online(self):
        node = {"name":"node-7", "transport":"tep", "tep_peer_id":"peer-node7",
                "role":"edge", "capacity_class":"best-effort"}
        with mock.patch.object(hb_aggregator.hb_tep_adapter, "fetch_summary",
                               return_value=summary("node-7", "edge", 200, 10)):
            result = hb_aggregator._fetch_summary(node)
        self.assertTrue(result["online"])
        self.assertEqual(result["transport"], "tep")
        self.assertEqual(result["role"], "edge")
        self.assertEqual(hb_aggregator._capacity_class(result), "best-effort")

    def test_tep_failure_marks_offline_but_configured_best_effort(self):
        node = {"name":"node-7", "transport":"tep", "tep_peer_id":"peer-node7",
                "role":"edge", "capacity_class":"best-effort"}
        with mock.patch.object(hb_aggregator.hb_tep_adapter, "fetch_summary", side_effect=RuntimeError("offline")):
            result = hb_aggregator._fetch_summary(node)
        self.assertFalse(result["online"])
        self.assertEqual(hb_aggregator._capacity_class(result), "best-effort")

    def test_tep_summary_uses_same_role_validation(self):
        node = {"name":"node-7", "transport":"tep", "tep_peer_id":"peer-node7",
                "role":"edge", "capacity_class":"best-effort"}
        bad = summary("node-7", "administrator", 200, 10)
        with mock.patch.object(hb_aggregator.hb_tep_adapter, "fetch_summary", return_value=bad):
            result = hb_aggregator._fetch_summary(node)
        self.assertFalse(result["online"])
        self.assertIn("invalid node_id/role", result["error"])

    def test_tep_summary_stale_rejected(self):
        node = {"name":"node-7", "transport":"tep", "tep_peer_id":"peer-node7",
                "role":"edge", "capacity_class":"best-effort"}
        bad = summary("node-7", "edge", 200, 10, timestamp=int(time.time()) - 10000)
        with mock.patch.object(hb_aggregator.hb_tep_adapter, "fetch_summary", return_value=bad):
            result = hb_aggregator._fetch_summary(node)
        self.assertFalse(result["online"])
        self.assertIn("stale", result["error"])

    def test_tep_summary_node_identity_mismatch_rejected(self):
        node = {"name":"node-7", "tep_node_id":"node-7", "transport":"tep",
                "tep_peer_id":"peer-node7", "role":"edge", "capacity_class":"best-effort"}
        bad = summary("other-node", "edge", 200, 10)
        with mock.patch.object(hb_aggregator.hb_tep_adapter, "fetch_summary", return_value=bad):
            result = hb_aggregator._fetch_summary(node)
        self.assertFalse(result["online"])
        self.assertIn("node_id does not match", result["error"])


if __name__ == "__main__": unittest.main()
