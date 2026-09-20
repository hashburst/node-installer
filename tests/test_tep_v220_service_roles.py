import unittest
from pathlib import Path

from tep.hb_tep_runtime_ha import TepEngine as HaTepEngine
from tep.hb_tep_runtime_v220 import TepEngine as V220TepEngine

ROOT = Path(__file__).resolve().parents[1]


class V220RuntimeTests(unittest.TestCase):
    def test_v220_runtime_is_the_ha_runtime(self):
        self.assertIs(V220TepEngine, HaTepEngine)

    def test_ha_installer_packages_atomic_base_runtime(self):
        installer = (ROOT / "ha" / "install-ha.sh").read_text(encoding="utf-8")
        self.assertIn(
            'install -m 0644 "$ROOT_DIR/tep/hb_tep_runtime.py" /opt/hashburst-tep/tep/hb_tep_runtime.py',
            installer,
        )
        self.assertIn('/opt/hashburst-tep/tep/hb_tep_runtime_v220.py', installer)

    def test_observation_requires_ha_lease(self):
        installer = (ROOT / "ha" / "install-ha.sh").read_text(encoding="utf-8")
        self.assertIn('if "ha.lease" not in services:', installer)


if __name__ == "__main__":
    unittest.main()
