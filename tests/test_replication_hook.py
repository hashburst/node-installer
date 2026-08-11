import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_TEST_ROOT = tempfile.TemporaryDirectory()
os.environ["HB_FILES_STORAGE"] = str(Path(_TEST_ROOT.name) / "files")
os.environ["HB_FILES_META"] = str(Path(_TEST_ROOT.name) / "meta")
sys.path.insert(0, str(ROOT / "hbfiles"))
import hb_files

def tearDownModule():
    _TEST_ROOT.cleanup()


class ReplicationHookTests(unittest.TestCase):
    def test_hook_is_disabled_by_default(self):
        with mock.patch.object(hb_files, "REPL_HOOK_ENABLED", False):
            out = hb_files._register_replication("bafy", 123, "file-1")
        self.assertEqual("disabled", out["state"])
        self.assertFalse(out["registered"])

    def test_enabled_hook_uses_file_id_as_idempotent_reference(self):
        calls = []

        class FakeClient:
            def __init__(self, base, token):
                self.base = base
                self.token = token
            def register(self, cid, size_bytes, source_node, reference_id=None, **kwargs):
                calls.append((cid, size_bytes, source_node, reference_id))
                return {
                    "state": "pending",
                    "target_replicas": 3,
                    "required_committable": 2,
                    "confirmed_total": 1,
                    "confirmed_committable": 1,
                }

        fake_module = types.SimpleNamespace(ReplicationClient=FakeClient)
        with mock.patch.dict(sys.modules, {"hb_replication_client": fake_module}), \
             mock.patch.object(hb_files, "REPL_HOOK_ENABLED", True), \
             mock.patch.object(hb_files, "REPL_HOOK_TOKEN", "secret"), \
             mock.patch.object(hb_files, "REPL_NODE_ID", "p1"):
            out = hb_files._register_replication("bafy", 123, "file-uuid")
        self.assertEqual([("bafy", 123, "p1", "file-uuid")], calls)
        self.assertTrue(out["registered"])
        self.assertEqual("pending", out["state"])
        self.assertEqual(3, out["target_replicas"])
        self.assertEqual(2, out["required_committable"])

    def test_controller_failure_does_not_claim_replication(self):
        class FailingClient:
            def __init__(self, base, token):
                pass
            def register(self, *args, **kwargs):
                raise RuntimeError("controller unavailable")

        fake_module = types.SimpleNamespace(ReplicationClient=FailingClient)
        with mock.patch.dict(sys.modules, {"hb_replication_client": fake_module}), \
             mock.patch.object(hb_files, "REPL_HOOK_ENABLED", True), \
             mock.patch.object(hb_files, "REPL_HOOK_TOKEN", "secret"), \
             mock.patch.object(hb_files, "REPL_NODE_ID", "p1"):
            out = hb_files._register_replication("bafy", 123, "file-uuid")
        self.assertFalse(out["registered"])
        self.assertEqual("registration-failed", out["state"])


if __name__ == "__main__":
    unittest.main()
