import json
import os
import tempfile
import time
import unittest
from unittest import mock

from aggregator import hb_aggregator


def sm(node_id, role, total, used, **extra):
    d={"available":True,"node_id":node_id,"role":role,"capacity_total_gb":total,
       "used_gb":used,"timestamp":int(time.time()),"capacity_source":"test"}
    d.update(extra); return d


class AccountingInvariantTests(unittest.TestCase):
    def setUp(self): self.old_nodes = hb_aggregator.NODES_FILE
    def tearDown(self): hb_aggregator.NODES_FILE = self.old_nodes

    def _set_config(self, nodes):
        f=tempfile.NamedTemporaryFile("w",delete=False); json.dump({"nodes":nodes},f); f.close()
        self.addCleanup(lambda: os.path.exists(f.name) and os.unlink(f.name)); hb_aggregator.NODES_FILE=f.name

    def _nodes(self, edge=True):
        nodes=[
          {"name":"primary","url":"http://primary:8091","role":"primary","capacity_class":"committable"},
          {"name":"secondary","url":"http://secondary:8091","role":"secondary","capacity_class":"committable"},
        ]
        if edge:
            nodes.append({"name":"node-7","transport":"tep","tep_peer_id":"peer-node7","role":"edge","capacity_class":"best-effort"})
        return nodes

    def _direct(self, node):
        if node["name"]=="primary":
            return dict(sm("primary","primary",1000,100), online=True, configured_class="committable",
                        configured_role="primary", name="primary", transport="direct",
                        reserved_stakeholders_gb=100, sold_active_gb=100, stakeholders=2)
        return dict(sm("secondary","secondary",500,50), online=True, configured_class="committable",
                    configured_role="secondary", name="secondary", transport="direct")

    def test_online_tep_edge_increases_best_effort_only(self):
        self._set_config(self._nodes(edge=True))
        edge = sm("node-7","edge",200,10)
        with mock.patch.object(hb_aggregator, "_fetch_summary_direct", side_effect=self._direct), \
             mock.patch.object(hb_aggregator.hb_tep_adapter, "fetch_summary", return_value=edge):
            result=hb_aggregator.aggregate()
        net=result["network"]
        self.assertEqual(net["capacity_committable_gb"],1500.0)
        self.assertEqual(net["capacity_best_effort_gb"],200.0)
        self.assertEqual(net["committable_nodes_online"],2)
        self.assertEqual(net["edge_nodes_online"],1)
        edge_out=next(n for n in result["nodes"] if n["node_id"]=="node-7")
        self.assertTrue(edge_out["online"])
        self.assertEqual(edge_out["capacity_class"],"best-effort")
        self.assertEqual(edge_out["transport"],"tep")

    def test_free_sellable_unchanged_when_edge_becomes_online(self):
        self._set_config(self._nodes(edge=False))
        with mock.patch.object(hb_aggregator, "_fetch_summary_direct", side_effect=self._direct):
            baseline=hb_aggregator.aggregate()
        baseline_sellable=baseline["network"]["free_sellable_gb"]

        self._set_config(self._nodes(edge=True))
        edge=sm("node-7","edge",9000,0)
        with mock.patch.object(hb_aggregator, "_fetch_summary_direct", side_effect=self._direct), \
             mock.patch.object(hb_aggregator.hb_tep_adapter, "fetch_summary", return_value=edge):
            with_edge=hb_aggregator.aggregate()
        self.assertEqual(with_edge["network"]["free_sellable_gb"], baseline_sellable)
        self.assertEqual(with_edge["network"]["capacity_committable_gb"],1500.0)
        self.assertEqual(with_edge["network"]["capacity_best_effort_gb"],9000.0)

    def test_primary_offline_remains_fail_closed_even_with_tep_edge(self):
        self._set_config(self._nodes(edge=True))
        def direct(node):
            if node["name"]=="primary":
                return {"name":"primary","online":False,"configured_role":"primary",
                        "configured_class":"committable","transport":"direct","error":"offline"}
            return self._direct(node)
        with mock.patch.object(hb_aggregator, "_fetch_summary_direct", side_effect=direct), \
             mock.patch.object(hb_aggregator.hb_tep_adapter, "fetch_summary", return_value=sm("node-7","edge",200,10)):
            result=hb_aggregator.aggregate()
        self.assertFalse(result["available"])
        self.assertEqual(result["accounting_status"],"primary-unavailable")
        self.assertIsNone(result["network"]["free_sellable_gb"])

    def test_tep_edge_self_reporting_primary_is_rejected_fail_closed(self):
        self._set_config(self._nodes(edge=True))
        fake=sm("node-7","primary",200,10)
        with mock.patch.object(hb_aggregator, "_fetch_summary_direct", side_effect=self._direct), \
             mock.patch.object(hb_aggregator.hb_tep_adapter, "fetch_summary", return_value=fake):
            result=hb_aggregator.aggregate()
        node=next(n for n in result["nodes"] if n["name"]=="node-7")
        self.assertFalse(node["online"])
        self.assertEqual(node["capacity_class"],"best-effort")
        self.assertEqual(result["network"]["capacity_committable_gb"],1500.0)
        self.assertEqual(result["network"]["capacity_best_effort_gb"],0.0)
        self.assertIsNotNone(result["network"]["free_sellable_gb"])


if __name__ == "__main__": unittest.main()
