from __future__ import annotations

import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

from tep.hb_tep_app import Identity, ProtocolError, encode_message, new_request
from tep.hb_tep_relay import RelayTable
from tep.hb_tep_runtime import TepEngine


class FakePeers:
    def __init__(self, peer):
        self.peer = peer
        self.updated = None
        self.seen = None

    def update_authenticated_endpoint(self, node_id, ip, port):
        self.updated = (node_id, ip, port)
        return True

    def mark_seen(self, node_id, latency_ms=0.0, pubkey=''):
        self.seen = (node_id, latency_ms, pubkey)

    def find_by_peer_id(self, peer_id):
        return self.peer if self.peer.peer_id == peer_id else None


class TepRuntimeV215Tests(unittest.TestCase):
    def test_authenticated_heartbeat_refreshes_dynamic_nat_route(self):
        peer = SimpleNamespace(id='node-7', peer_id='peer-node7', pubkey='ab' * 32)
        engine = object.__new__(TepEngine)
        engine.peers = FakePeers(peer)
        engine._relay_table = RelayTable()

        engine._record_authenticated_heartbeat(
            peer,
            {'type': 'heartbeat', 'node': 'node-7', 'pubkey': 'ab' * 32},
            ('203.0.113.44', 53123),
        )

        self.assertEqual(engine.peers.updated, ('node-7', '203.0.113.44', 53123))
        route = engine._relay_table.get('peer-node7')
        self.assertEqual((route.ip, route.port), ('203.0.113.44', 53123))

    def test_heartbeat_cannot_replace_registered_tep_key(self):
        peer = SimpleNamespace(id='node-7', peer_id='peer-node7', pubkey='ab' * 32)
        engine = object.__new__(TepEngine)
        engine.peers = FakePeers(peer)
        engine._relay_table = RelayTable()
        with self.assertRaises(ProtocolError):
            engine._record_authenticated_heartbeat(
                peer,
                {'type': 'heartbeat', 'node': 'node-7', 'pubkey': 'cd' * 32},
                ('203.0.113.45', 53124),
            )
        self.assertIsNone(engine.peers.updated)

    def test_local_rendezvous_forwards_storage_summary(self):
        local = Identity('blockchainapi.one', 'peer-rendezvous')
        target = Identity('node-7', 'peer-node7')
        peer = SimpleNamespace(id=target.node_id, peer_id=target.peer_id, pubkey='ab' * 32)
        engine = object.__new__(TepEngine)
        engine.node_id = local.node_id
        engine.peer_id = local.peer_id
        engine._relay_enabled = True
        engine.peers = FakePeers(peer)
        engine._relay_table = RelayTable()
        engine._relay_table.observe(
            peer_id=target.peer_id, ip='203.0.113.44', port=53123,
            pubkey=peer.pubkey, authenticated=True,
        )
        expected = b'inner-response'
        engine._relay_forward_to_target = types.MethodType(
            lambda self, target_peer_id, raw_request, timeout_sec, route: expected,
            engine,
        )
        raw = encode_message(new_request(
            source=local, destination=target, service='storage.summary', payload={}
        ))
        with patch.object(TepEngine, 'app_ready', new_callable=PropertyMock, return_value=True):
            self.assertEqual(
                engine.relay_transport(local.peer_id, target.peer_id, raw, 2.0), expected
            )

    def test_local_rendezvous_has_explicit_service_guard(self):
        runtime = (Path(__file__).resolve().parents[1] / 'tep' / 'hb_tep_runtime.py').read_text(encoding='utf-8')
        self.assertIn('env.service != "storage.summary"', runtime)
        self.assertIn('unsupported_service', runtime)

    def test_runtime_prefers_wire_identity_before_source_ip(self):
        runtime = (Path(__file__).resolve().parents[1] / 'tep' / 'hb_tep_runtime.py').read_text(encoding='utf-8')
        self.assertLess(runtime.index('find_by_wire_node_id'), runtime.index('p.ip == addr[0]'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
