import json
import os
import tempfile
import unittest
from pathlib import Path

from tep.hb_tep_runtime_v220 import k325t_enabled_for_node

ROOT = Path(__file__).resolve().parents[1]


class V220ServiceRoleTests(unittest.TestCase):
    def setUp(self):
        self._old_override = os.environ.pop("HB_TEP_K325T_ENABLED", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name) / "ha.json"
        self.config.write_text(
            json.dumps(
                {
                    "node_id": "hashburst-witness-1",
                    "roles": ["voter", "observer"],
                    "candidates": [
                        {"node_id": "master-node", "priority": 10},
                        {"node_id": "hashburst-dr1", "priority": 20},
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        os.environ.pop("HB_TEP_K325T_ENABLED", None)
        if self._old_override is not None:
            os.environ["HB_TEP_K325T_ENABLED"] = self._old_override
        self.tmp.cleanup()

    def test_witness_does_not_advertise_k325t(self):
        self.assertFalse(k325t_enabled_for_node("hashburst-witness-1", self.config))

    def test_candidates_advertise_k325t(self):
        self.assertTrue(k325t_enabled_for_node("master-node", self.config))
        self.assertTrue(k325t_enabled_for_node("hashburst-dr1", self.config))

    def test_explicit_override_is_supported(self):
        os.environ["HB_TEP_K325T_ENABLED"] = "1"
        self.assertTrue(k325t_enabled_for_node("hashburst-witness-1", self.config))
        os.environ["HB_TEP_K325T_ENABLED"] = "0"
        self.assertFalse(k325t_enabled_for_node("master-node", self.config))

    def test_ha_installer_packages_atomic_base_runtime(self):
        installer = (ROOT / "ha" / "install-ha.sh").read_text(encoding="utf-8")
        self.assertIn(
            'install -m 0644 "$ROOT_DIR/tep/hb_tep_runtime.py" /opt/hashburst-tep/tep/hb_tep_runtime.py',
            installer,
        )
        self.assertIn('/opt/hashburst-tep/tep/hb_tep_runtime.py', installer)


if __name__ == "__main__":
    unittest.main()
