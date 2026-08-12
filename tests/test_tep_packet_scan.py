import tempfile
import unittest
from pathlib import Path

from tools.hb_tep_packet_scan import scan_packet_constants
from tep.hb_tep_wire import allocate_packet_types


class PacketScanTests(unittest.TestCase):
    def test_ast_scan_does_not_execute_daemon(self):
        src = '''\nPKT_HEARTBEAT = 0x01\nPKT_OTHER: int = 0x20\nraise RuntimeError("must not execute")\n'''
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "hb_tep.py"
            p.write_text(src, encoding="utf-8")
            found = scan_packet_constants(p)
        self.assertEqual({"PKT_HEARTBEAT": 1, "PKT_OTHER": 0x20}, found)

    def test_scanned_values_feed_collision_safe_allocator(self):
        src = 'PKT_HEARTBEAT=1\nPKT_FOO=0x20\nPKT_BAR=0x21\n'
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "hb_tep.py"
            p.write_text(src, encoding="utf-8")
            found = scan_packet_constants(p)
        proposed = allocate_packet_types(found.values())
        self.assertEqual([0x22, 0x23, 0x24, 0x25, 0x26], list(proposed.as_dict().values()))


if __name__ == "__main__":
    unittest.main()
