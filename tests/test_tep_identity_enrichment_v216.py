from __future__ import annotations

import io
import json
import unittest
from unittest import mock

from tep.hb_tep import Peer
from tep.hb_tep_app import ProtocolError
from tep.hb_tep_runtime import TepEngine


class _Response:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._raw


class _Peers:
    _rpc_port = 8009


class _Crypto:
    def derive_shared_key(self, pubkey):
        return b"k" * 32


class V216IdentityEnrichmentTests(unittest.TestCase):
    def make_engine(self):
        engine = object.__new__(TepEngine)
        engine.peers = _Peers()
        engine.crypto = _Crypto()
        engine._identity_refresh_at = {}
        return engine

    def test_authoritative_identity_uses_api_nodes_stable_fields(self):
        engine = self.make_engine()
        payload = [
            {
                "node_id": "node-7",
                "peer_id": "peer-node-7",
                "tep_pubkey": "ab" * 32,
                "external_ip": "",
            }
        ]
        with mock.patch("tep.hb_tep_runtime.urllib.request.urlopen", return_value=_Response(payload)) as call:
            self.assertEqual(engine._authoritative_identity("node-7"), ("peer-node-7", "ab" * 32))
        self.assertIn("/api/nodes", call.call_args.args[0])

    def test_missing_tep_peer_identity_is_enriched_without_overwriting_nat_ip(self):
        engine = self.make_engine()
        peer = Peer(id="node-7", ip="79.12.5.136", port=47777, pubkey="", peer_id=None)
        with mock.patch.object(engine, "_authoritative_identity", return_value=("peer-node-7", "cd" * 32)):
            self.assertTrue(engine._ensure_peer_identity(peer))
        self.assertEqual(peer.ip, "79.12.5.136")
        self.assertEqual(peer.port, 47777)
        self.assertEqual(peer.peer_id, "peer-node-7")
        self.assertEqual(peer.pubkey, "cd" * 32)

    def test_heartbeat_shared_key_fails_closed_when_identity_is_missing(self):
        engine = self.make_engine()
        peer = Peer(id="node-7", ip="79.12.5.136", port=47777, pubkey="", peer_id=None)
        with mock.patch.object(engine, "_ensure_peer_identity", return_value=False):
            with self.assertRaises(ProtocolError):
                engine._get_shared_key(peer)

    def test_heartbeat_shared_key_uses_registered_x25519_pubkey(self):
        engine = self.make_engine()
        peer = Peer(id="node-7", ip="79.12.5.136", port=47777,
                    pubkey="ef" * 32, peer_id="peer-node-7")
        with mock.patch.object(engine, "_ensure_peer_identity", return_value=True):
            self.assertEqual(engine._get_shared_key(peer), b"k" * 32)


if __name__ == "__main__":
    unittest.main()
