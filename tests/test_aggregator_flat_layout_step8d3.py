from __future__ import annotations

import importlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


class FlatAggregatorLayoutTests(unittest.TestCase):
    def test_flat_sibling_import_matches_production_layout(self):
        repo_root = Path(__file__).resolve().parent.parent
        src_dir = repo_root / "aggregator"

        with tempfile.TemporaryDirectory() as td:
            flat = Path(td)
            shutil.copy2(src_dir / "hb_aggregator.py", flat / "hb_aggregator.py")
            shutil.copy2(src_dir / "hb_tep_adapter.py", flat / "hb_tep_adapter.py")

            old_path = list(sys.path)
            old_modules = {
                name: sys.modules.get(name)
                for name in ("hb_aggregator", "hb_tep_adapter")
            }

            try:
                sys.path[:] = [str(flat)] + [p for p in sys.path if p != str(repo_root)]
                sys.modules.pop("hb_aggregator", None)
                sys.modules.pop("hb_tep_adapter", None)

                module = importlib.import_module("hb_aggregator")

                self.assertEqual(
                    Path(module.hb_tep_adapter.__file__).resolve(),
                    (flat / "hb_tep_adapter.py").resolve(),
                )
            finally:
                sys.path[:] = old_path
                for name, module in old_modules.items():
                    if module is None:
                        sys.modules.pop(name, None)
                    else:
                        sys.modules[name] = module


if __name__ == "__main__":
    unittest.main()
