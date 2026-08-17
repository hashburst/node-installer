from __future__ import annotations

import json
import threading
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


class _ReconPeers:
    def __init__(self, initial, replacement):
        self._rpc_port = 8009
        self._lock = threading.Lock()
        self._peers = {p.id: p for p in initial}
        self._replacement = {p.id: p for p in replacement}

    def get_all(self):
        with self._lock:
            return list(self._peers.values())

    def sync_from_blockchain(self):
        with self._lock:
            self._peers = dict(self._replacement)
        return True


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

    def make_reconciliation_engine(self, initial, replacement):
        engine = object.__new__(TepEngine)
        engine.node_id = "blockchainapi.one"
        engine.peers = _ReconPeers(initial, replacement)
        engine._identity_refresh_at = {}
        return engine

    def node7_record(self):
        return {
            "node_id": "node-7",
            "peer_id": "peer-node-7",
            "tep_pubkey": "ab" * 32,
            "tep_port": 47777,
            "external_ip": "",
            "multiaddrs": ["/ip4/192.168.1.29/tcp/30307/p2p/peer-node-7"],
        }

    def test_sync_restores_registered_peer_omitted_by_tep_peers(self):
        node6 = Peer(id="node-6", ip="77.90.188.155", pubkey="cd" * 32, peer_id="peer-node-6")
        engine = self.make_reconciliation_engine([], [node6])
        with mock.patch.object(engine, "_authoritative_nodes", return_value=[self.node7_record()]):
            engine._install_registry_reconciliation()
            self.assertTrue(engine.peers.sync_from_blockchain())
        peer = engine.peers._peers["node-7"]
        self.assertEqual(peer.peer_id, "peer-node-7")
        self.assertEqual(peer.pubkey, "ab" * 32)
        self.assertEqual(peer.ip, "192.168.1.29")
        self.assertFalse(peer.online)

    def test_sync_preserves_authenticated_nat_endpoint_when_tep_peers_omits_peer(self):
        previous = Peer(
            id="node-7", ip="79.12.5.136", port=47777,
            pubkey="ab" * 32, peer_id="peer-node-7",
            last_seen=123.0, latency_ms=0.0, online=True,
        )
        node6 = Peer(id="node-6", ip="77.90.188.155", pubkey="cd" * 32, peer_id="peer-node-6")
        engine = self.make_reconciliation_engine([previous], [node6])
        with mock.patch.object(engine, "_authoritative_nodes", return_value=[self.node7_record()]):
            engine._install_registry_reconciliation()
            self.assertTrue(engine.peers.sync_from_blockchain())
        peer = engine.peers._peers["node-7"]
        self.assertEqual(peer.ip, "79.12.5.136")
        self.assertEqual(peer.port, 47777)
        self.assertEqual(peer.peer_id, "peer-node-7")
        self.assertEqual(peer.pubkey, "ab" * 32)
        self.assertTrue(peer.online)
        self.assertEqual(peer.last_seen, 123.0)

    def test_reconciliation_never_adds_local_node_as_peer(self):
        engine = self.make_reconciliation_engine([], [])
        local = {
            "node_id": "blockchainapi.one",
            "peer_id": "peer-local",
            "tep_pubkey": "ef" * 32,
            "tep_port": 47777,
            "external_ip": "64.31.4.9",
        }
        with mock.patch.object(engine, "_authoritative_nodes", return_value=[local]):
            engine._install_registry_reconciliation()
            self.assertTrue(engine.peers.sync_from_blockchain())
        self.assertNotIn("blockchainapi.one", engine.peers._peers)


if __name__ == "__main__":
    unittest.main()
