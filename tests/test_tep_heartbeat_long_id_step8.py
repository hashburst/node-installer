import importlib.util
import pathlib
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
DAEMON = ROOT / "tep" / "hb_tep.py"


def load_daemon():
    spec = importlib.util.spec_from_file_location("hb_tep_step8_longid", DAEMON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class LongNodeIdHeartbeatRegressionTests(unittest.TestCase):
    def test_resolved_peer_uses_canonical_identity_for_mark_seen(self):
        source = DAEMON.read_text()

        self.assertIn(
            "self.peers.mark_seen(peer.id, latency_ms=0.0,",
            source,
        )

        self.assertNotIn(
            "self.peers.mark_seen(peer_id, latency_ms=0.0,\n"
            "                                     pubkey=msg.get('pubkey', ''))",
            source,
        )

    def test_blockchainapi_one_is_longer_than_wire_identity(self):
        node_id = "blockchainapi.one"

        self.assertGreater(len(node_id.encode("ascii")), 16)
        self.assertEqual(
            node_id.encode("ascii")[:16].decode("ascii"),
            "blockchainapi.on",
        )


if __name__ == "__main__":
    unittest.main()
