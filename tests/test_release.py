#!/usr/bin/env python3
import importlib.util, json, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_agg():
    p = ROOT / "aggregator" / "hb_aggregator.py"
    spec = importlib.util.spec_from_file_location("hb_aggregator_test", p)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


class PackageTests(unittest.TestCase):
    def test_direct_package_layout(self):
        for rel in ["install.sh", "bin/hashburst-node", "hbfiles/hb_files.py",
                    "ipfs-scripts/01-install-ipfs-dual-noZFS.sh",
                    "systemd/hashburst-files.service", "aggregator/hb_aggregator.py"]:
            self.assertTrue((ROOT / rel).is_file(), rel)

    def test_swarm_key_is_fail_closed(self):
        s=(ROOT/"install.sh").read_text()
        self.assertIn("swarm.key missing", s)
        self.assertIn("refusing to create a split private network", s)

    def test_no_ciphertext_rstrip_corruption(self):
        s=(ROOT/"hbfiles/hb_files.py").read_text()
        self.assertNotIn("content.rstrip(b'\\r\\n--')", s)
        self.assertIn("content.endswith(b'\\r\\n')", s)

    def test_panel_has_no_inline_filename_download_handler(self):
        s=(ROOT/"hbfiles/panel.html").read_text()
        self.assertNotIn("onclick=\"downloadFile('${f.id}'", s)
        self.assertIn("btn.addEventListener('click'", s)
        self.assertIn("name.textContent", s)

    def test_blockchain_role_does_not_run_ipfs_setup(self):
        s=(ROOT/"install.sh").read_text()
        self.assertIn('if [ "$ROLE" != blockchain ]; then', s)

    def test_existing_public_ipfs_can_be_reused(self):
        s=(ROOT/"install.sh").read_text()
        self.assertIn('PUBLIC_IPFS_MODE="reuse"', s)
        ip=(ROOT/"ipfs-scripts/01-install-ipfs-dual-noZFS.sh").read_text()
        self.assertIn("reusing pre-existing daemon", ip)

    def test_no_implicit_kubo_replacement(self):
        ip=(ROOT/"ipfs-scripts/01-install-ipfs-dual-noZFS.sh").read_text()
        self.assertIn("Never downgrade or replace an existing Kubo binary", ip)
        self.assertIn("sha512sum -c", ip)

    def test_storage_aggregator_port_contract(self):
        unit=(ROOT/"aggregator/hashburst-aggregator.service").read_text()
        server=(ROOT/"aggregator/hb_aggregator_server.py").read_text()
        self.assertIn("Environment=HB_AGGREGATOR_PORT=8094", unit)
        self.assertIn("Environment=HB_AGGREGATOR_TIMEOUT=3", unit)
        self.assertNotIn("Environment=HB_AGGREGATOR_PORT=8093", unit)
        self.assertIn('os.environ.get("HB_AGGREGATOR_PORT", "8094")', server)

    def test_example_storage_classes_are_explicit(self):
        data=json.loads((ROOT/"aggregator/storage-nodes.example.json").read_text())
        for node in data["nodes"]:
            self.assertIn(node.get("role"), {"primary", "secondary", "edge"})
            self.assertIn(node.get("capacity_class"), {"committable", "best-effort"})
            if node["role"] == "edge":
                self.assertEqual(node["capacity_class"], "best-effort")

    def test_explorer_patcher_is_csp_safe(self):
        patcher_path=ROOT/"integrations/explorer/patch_hashburst_explorer.py"
        patcher=patcher_path.read_text()
        self.assertNotIn(".style.", patcher)
        self.assertNotIn("style=", patcher)
        spec=importlib.util.spec_from_file_location("explorer_patch_test", patcher_path)
        m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        fixture="""<div class="stat-card"><div class="stat-label">Private IPFS</div><div class="stat-value" id="st-ipfs">—</div><div class="stat-sub">Private swarm, connected</div></div>
<div class="stat-card"><div class="stat-label">Total Capacity</div><div class="stat-value" id="st-total">—</div><div class="stat-sub">Sovereign storage pool</div></div>
<script nonce="x">$('st-ipfs').textContent = s.ipfs_private_peers ?? '—'; $('st-total').textContent = fmtGB(s.capacity_total_gb);</script>"""
        out=m.patch_text(fixture)
        self.assertIn("Best-effort Capacity", out)
        self.assertIn("Committable Capacity", out)
        self.assertIn("capacity_committable_gb", out)
        self.assertIn("capacity_best_effort_gb", out)
        self.assertEqual(m.patch_text(out), out)

    def test_admin_default_secret_rejected(self):
        s=(ROOT/"hbfiles/hb_files.py").read_text()
        self.assertIn("HB_ADMIN_SECRET missing/refuses default secret", s)

    def test_cid_unpin_reference_count(self):
        s=(ROOT/"hbfiles/hb_files.py").read_text()
        self.assertIn("final reference", s)
        self.assertIn("refs == 0", s)


class AggregatorTests(unittest.TestCase):
    def _aggregate(self, summaries):
        m=load_agg()
        nodes=[{"name":f"n{i}","url":f"http://n{i}"} for i in range(len(summaries))]
        with patch.object(m, "discover_nodes", return_value=nodes), \
             patch.object(m, "_fetch_summary", side_effect=summaries):
            return m.aggregate()

    def test_current_four_node_snapshot(self):
        # User-provided live snapshot: only two nodes currently provide HB-Files.
        summaries=[
          {"name":"primary","online":True,"available":True,"role":"primary","node_id":"hb-node",
           "capacity_total_gb":5120.0,"used_gb":2.13,"capacity_source":"zfs",
           "reserved_stakeholders_gb":3736.0,"sold_active_gb":0,"stakeholders":1868},
          {"name":"secondary","online":True,"available":True,"role":"secondary","node_id":"hb-storage-node6",
           "capacity_total_gb":400.0,"used_gb":1.37,"capacity_source":"logical"},
        ]
        out=self._aggregate(summaries)
        self.assertTrue(out["available"])
        self.assertEqual(out["network"]["capacity_committable_gb"], 5520.0)
        self.assertEqual(out["network"]["used_committable_gb"], 3.5)
        self.assertEqual(out["network"]["free_sellable_gb"], 1784.0)
        self.assertEqual(out["network"]["stakeholders"], 1868)

    def test_edge_capacity_never_becomes_sellable(self):
        summaries=[
          {"name":"p","online":True,"role":"primary","node_id":"p","capacity_total_gb":5120,"used_gb":100,
           "reserved_stakeholders_gb":3736,"sold_active_gb":0,"stakeholders":1868},
          {"name":"edge","online":True,"role":"edge","node_id":"e","capacity_total_gb":1000,"used_gb":10},
        ]
        out=self._aggregate(summaries)
        self.assertEqual(out["network"]["capacity_committable_gb"], 5120.0)
        self.assertEqual(out["network"]["capacity_best_effort_gb"], 1000.0)
        self.assertEqual(out["network"]["free_sellable_gb"], 1384.0)

    def test_offline_edge_preserves_best_effort_class(self):
        m=load_agg()
        nodes=[
          {"name":"master-node","url":"http://85.233.199.35:8091","capacity_class":"committable"},
          {"name":"node-6","url":"http://77.90.188.155:8091","capacity_class":"committable"},
          {"name":"node-7","url":"http://87.9.166.34:8091","capacity_class":"best-effort"},
        ]
        summaries=[
          {"name":"master-node","online":True,"role":"primary","node_id":"hb-node",
           "capacity_total_gb":5120,"used_gb":2.2,"reserved_stakeholders_gb":3736,
           "sold_active_gb":0,"stakeholders":1868},
          {"name":"node-6","online":True,"role":"secondary","node_id":"hb-storage-node6",
           "capacity_total_gb":400,"used_gb":1.57},
          {"name":"node-7","url":"http://87.9.166.34:8091","online":False,
           "configured_class":"best-effort","error":"timed out"},
        ]
        with patch.object(m, "discover_nodes", return_value=nodes), \
             patch.object(m, "_fetch_summary", side_effect=summaries):
            out=m.aggregate()
        edge=next(n for n in out["nodes"] if n["name"] == "node-7")
        self.assertFalse(edge["online"])
        self.assertEqual(edge["role"], "?")
        self.assertEqual(edge["capacity_class"], "best-effort")
        self.assertEqual(out["network"]["capacity_committable_gb"], 5520.0)
        self.assertEqual(out["network"]["capacity_best_effort_gb"], 0)
        self.assertEqual(out["network"]["free_sellable_gb"], 1784.0)

    def test_offline_unclassified_is_unknown_never_committable(self):
        m=load_agg()
        nodes=[
          {"name":"master","url":"http://p","role":"primary","capacity_class":"committable"},
          {"name":"mystery","url":"http://m"},
        ]
        summaries=[
          {"name":"master","online":True,"role":"primary","node_id":"p","capacity_total_gb":100,
           "used_gb":1,"reserved_stakeholders_gb":10,"sold_active_gb":0,"stakeholders":5},
          {"name":"mystery","url":"http://m","online":False,"configured_class":None,
           "configured_role":None,"error":"timed out"},
        ]
        with patch.object(m, "discover_nodes", return_value=nodes), \
             patch.object(m, "_fetch_summary", side_effect=summaries):
            out=m.aggregate()
        mystery=next(n for n in out["nodes"] if n["name"] == "mystery")
        self.assertEqual(mystery["capacity_class"], "unknown")
        self.assertNotEqual(mystery["capacity_class"], "committable")
        self.assertEqual(out["network"]["capacity_committable_gb"], 100.0)

    def test_offline_configured_edge_role_infers_best_effort(self):
        m=load_agg()
        r={"name":"edge","online":False,"configured_class":None,"configured_role":"edge"}
        self.assertEqual(m._capacity_class(r), "best-effort")

    def test_online_validated_role_overrides_bad_config_class(self):
        m=load_agg()
        self.assertEqual(m._capacity_class({"online":True,"role":"edge","configured_class":"committable"}), "best-effort")
        self.assertEqual(m._capacity_class({"online":True,"role":"primary","configured_class":"best-effort"}), "committable")

    def test_invalid_offline_class_is_unknown(self):
        m=load_agg()
        self.assertEqual(m._capacity_class({"online":False,"configured_class":"bogus"}), "unknown")

    def test_primary_offline_is_fail_closed(self):
        summaries=[{"name":"s","online":True,"role":"secondary","node_id":"s","capacity_total_gb":400,"used_gb":1}]
        out=self._aggregate(summaries)
        self.assertFalse(out["available"])
        self.assertEqual(out["accounting_status"], "primary-unavailable")
        self.assertIsNone(out["network"]["free_sellable_gb"])

    def test_physical_usage_caps_sellable(self):
        summaries=[{"name":"p","online":True,"role":"primary","node_id":"p","capacity_total_gb":100,"used_gb":95,
                    "reserved_stakeholders_gb":10,"sold_active_gb":0,"stakeholders":5}]
        out=self._aggregate(summaries)
        self.assertEqual(out["network"]["free_sellable_gb"], 5.0)

    def test_duplicate_node_id_not_double_counted(self):
        summaries=[
          {"name":"p","online":True,"role":"primary","node_id":"same","capacity_total_gb":100,"used_gb":1,
           "reserved_stakeholders_gb":10,"sold_active_gb":0,"stakeholders":5},
          {"name":"dup","online":True,"role":"secondary","node_id":"same","capacity_total_gb":999,"used_gb":1},
        ]
        out=self._aggregate(summaries)
        self.assertEqual(out["network"]["capacity_committable_gb"], 100.0)
        self.assertEqual(out["network"]["storage_nodes_online"], 1)


if __name__ == '__main__': unittest.main(verbosity=2)
