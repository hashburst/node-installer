from __future__ import annotations
import hashlib, importlib.util, json, tempfile, threading, time, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import unittest
import urllib.request

from tep.hb_tep_app import Identity, encode_message, new_request
from tep.hb_tep_client import TepRpcClient
from tep.hb_tep_services import StorageSummaryConfig, build_default_registry

ROOT = Path(__file__).resolve().parents[1]
MOD_PATH = ROOT / 'staging' / 'hb_tep_step5b.py'
spec = importlib.util.spec_from_file_location('hb_tep_step5b', MOD_PATH)
hb = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hb
spec.loader.exec_module(hb)

class SummaryHandler(BaseHTTPRequestHandler):
    payload = {
        'available': True, 'node_id': 'node-b', 'role': 'edge',
        'capacity_total_gb': 200, 'used_gb': 10, 'timestamp': 0,
    }
    seen = []
    def log_message(self, *a): pass
    def do_GET(self):
        type(self).seen.append((self.command, self.path))
        raw = json.dumps(type(self).payload).encode()
        self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(raw)

class Step5BDaemonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.http = HTTPServer(('127.0.0.1',0), SummaryHandler)
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True); self.http_thread.start()
        SummaryHandler.seen.clear()
        self.engines=[]

    def tearDown(self):
        for e in self.engines:
            e._stop.set()
            try: e.sock.close()
            except Exception: pass
        self.http.shutdown(); self.http.server_close(); self.tmp.cleanup()

    def _paths(self, name):
        d=self.root/name; d.mkdir();
        hb.STATE_DIR=d; hb.PEERS_FILE=d/'peers.json'; hb.KEY_FILE=d/'node.key'; hb.X25519_KEY=d/'node_x25519.key'; hb.LOG_FILE=d/'tep.log'
        hb.PEERS_FILE.write_text('{"peers":[]}')

    def _engine(self, node, peer, *, relay=False, relay_clients=None, trusted_rendezvous=None, service=False):
        self._paths(node)
        registry=None
        if service:
            registry=build_default_registry(storage_summary_config=StorageSummaryConfig(url=f'http://127.0.0.1:{self.http.server_port}/api/public/storage-summary'))
        e=hb.TepEngine(node_id=node, peer_id=peer, listen_host='127.0.0.1', listen_port=0, status_port=0,
                       relay_enabled=relay, relay_clients=relay_clients or [], trusted_rendezvous=trusted_rendezvous or [], service_registry=registry)
        self.engines.append(e); return e

    def _register(self, a, b, *, ip='127.0.0.1', port=None):
        with a.peers._lock:
            a.peers._peers[b.node_id]=hb.Peer(id=b.node_id, ip=ip, port=port or b.listen_port,
                                              pubkey=b.crypto.pubkey_hex, peer_id=b.peer_id)

    def _start_rx(self, *engines):
        for e in engines:
            threading.Thread(target=e._recv_loop, daemon=True).start()

    def test_original_sha256_matches_production(self):
        raw=Path('baseline/hb_tep-production-current.py').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),'1a1e0554c001020c80261b638e57bf5fe072da574967264dd2e0dbef91b61e24')

    def test_packet_types_frozen_and_collision_free(self):
        self.assertEqual(hb.PKT_HEARTBEAT,0x01)
        self.assertEqual([hb.PKT_APP_REQUEST,hb.PKT_APP_RESPONSE,hb.PKT_APP_ERROR,hb.PKT_RELAY_REQUEST,hb.PKT_RELAY_RESPONSE],[0x20,0x21,0x22,0x23,0x24])
        self.assertEqual(len(hb.APP_PACKET_TYPES),5)
        self.assertNotIn(hb.PKT_HEARTBEAT,hb.APP_PACKET_TYPES)

    def test_long_node_id_resolves_by_unique_wire_prefix_then_full_identity(self):
        a=self._engine('blockchainapi.one','peer-a'); b=self._engine('node-b','peer-b',service=True)
        self._register(a,b); self._register(b,a); self._start_rx(a,b)
        client=TepRpcClient(local_identity=Identity('blockchainapi.one','peer-a'), transport=a.app_transport)
        out=client.request(destination=Identity('node-b','peer-b'), service='storage.summary', payload={}, timeout_sec=2)
        self.assertEqual(out['node_id'],'node-b')
        self.assertEqual('blockchainapi.one'[:16], 'blockchainapi.on')

    def test_ambiguous_16_byte_wire_prefix_is_rejected(self):
        b=self._engine('node-b','peer-b')
        fake1=hb.Peer(id='1234567890123456A',ip='127.0.0.1',pubkey='aa',peer_id='p1')
        fake2=hb.Peer(id='1234567890123456B',ip='127.0.0.1',pubkey='bb',peer_id='p2')
        with b.peers._lock:
            b.peers._peers[fake1.id]=fake1; b.peers._peers[fake2.id]=fake2
        self.assertIsNone(b.peers.find_by_wire_node_id('1234567890123456'))

    def test_real_x25519_shared_key_symmetric(self):
        a=self._engine('node-a','peer-a'); b=self._engine('node-b','peer-b')
        ka=a.crypto.derive_shared_key(b.crypto.pubkey_hex); kb=b.crypto.derive_shared_key(a.crypto.pubkey_hex)
        self.assertEqual(ka,kb); self.assertEqual(len(ka),32)

    def test_legacy_wire_build_parse_decrypt_unchanged(self):
        a=self._engine('node-a','peer-a'); b=self._engine('node-b','peer-b')
        self._register(a,b); self._register(b,a)
        payload=json.dumps({'type':'heartbeat','ts':int(time.time()),'node':'node-a','pubkey':a.crypto.pubkey_hex}).encode()
        key=a.crypto.derive_shared_key(b.crypto.pubkey_hex)
        pkt=a._build_packet(hb.PKT_HEARTBEAT,payload,key)
        parsed=b._parse_packet(pkt)
        self.assertEqual(parsed['version'],hb.TEP_VERSION); self.assertEqual(parsed['type'],hb.PKT_HEARTBEAT); self.assertEqual(parsed['node_id'],'node-a')
        plain=b.crypto.decrypt(parsed['enc_payload'],b.crypto.derive_shared_key(a.crypto.pubkey_hex),parsed['nonce'])
        self.assertEqual(plain,payload)

    def test_direct_app_rpc_over_real_udp_and_crypto(self):
        a=self._engine('node-a','peer-a'); b=self._engine('node-b','peer-b',service=True)
        self._register(a,b); self._register(b,a); self._start_rx(a,b)
        client=TepRpcClient(local_identity=Identity('node-a','peer-a'), transport=a.app_transport)
        out=client.request(destination=Identity('node-b','peer-b'), service='storage.summary', payload={}, timeout_sec=2)
        self.assertEqual(out['node_id'],'node-b'); self.assertEqual(SummaryHandler.seen,[('GET','/api/public/storage-summary')])
        self.assertGreaterEqual(a.stats.pkts_recv,1); self.assertGreaterEqual(b.stats.app_requests,1)

    def test_ciphertext_does_not_expose_service_or_peer(self):
        a=self._engine('node-a','peer-a'); b=self._engine('node-b','peer-b')
        self._register(a,b)
        req=new_request(source=Identity('node-a','peer-a'), destination=Identity('node-b','peer-b'), service='storage.summary', payload={})
        raw=encode_message(req); key=a.crypto.derive_shared_key(b.crypto.pubkey_hex); pkt=a._build_packet(hb.PKT_APP_REQUEST,raw,key)
        self.assertNotIn(b'storage.summary',pkt); self.assertNotIn(b'peer-a',pkt); self.assertNotIn(b'peer-b',pkt)

    def test_spoofed_inner_identity_is_rejected_and_endpoint_not_changed(self):
        a=self._engine('node-a','peer-a'); b=self._engine('node-b','peer-b',service=True)
        self._register(a,b); self._register(b,a,ip='203.0.113.9',port=49999); self._start_rx(b)
        req=new_request(source=Identity('node-a','spoof-peer'), destination=Identity('node-b','peer-b'), service='storage.summary', payload={})
        key=a.crypto.derive_shared_key(b.crypto.pubkey_hex); pkt=a._build_packet(hb.PKT_APP_REQUEST,encode_message(req),key)
        a.sock.sendto(pkt,('127.0.0.1',b.listen_port)); time.sleep(.25)
        peer=b.peers.find_by_node_id('node-a')
        self.assertEqual((peer.ip,peer.port),('203.0.113.9',49999)); self.assertGreaterEqual(b.stats.app_auth_rejected,1)

    def test_authenticated_packet_updates_dynamic_endpoint(self):
        a=self._engine('node-a','peer-a'); b=self._engine('node-b','peer-b',service=True)
        self._register(a,b); self._register(b,a,ip='203.0.113.9',port=49999); self._start_rx(b)
        req=new_request(source=Identity('node-a','peer-a'), destination=Identity('node-b','peer-b'), service='storage.summary', payload={})
        key=a.crypto.derive_shared_key(b.crypto.pubkey_hex); pkt=a._build_packet(hb.PKT_APP_REQUEST,encode_message(req),key)
        a.sock.sendto(pkt,('127.0.0.1',b.listen_port)); time.sleep(.25)
        peer=b.peers.find_by_node_id('node-a')
        self.assertEqual(peer.ip,'127.0.0.1'); self.assertEqual(peer.port,a.listen_port)

    def test_status_http_api_on_loopback(self):
        a=self._engine('node-a','peer-a')
        server=a.start_status_server()
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{a.status_port}/', timeout=2) as r:
                body=json.loads(r.read())
            self.assertEqual(body['node_id'],'node-a'); self.assertTrue(body['app_ready'])
            self.assertEqual(body['app_protocols'],['HB-TEP-APP/1']); self.assertFalse(body['relay'])
        finally:
            server.shutdown(); server.server_close()

    def test_legacy_heartbeat_received_over_real_udp(self):
        a=self._engine('node-a','peer-a'); b=self._engine('node-b','peer-b')
        self._register(a,b); self._register(b,a); self._start_rx(b)
        payload=json.dumps({'type':'heartbeat','ts':int(time.time()),'node':'node-a','pubkey':a.crypto.pubkey_hex}).encode()
        key=a.crypto.derive_shared_key(b.crypto.pubkey_hex); pkt=a._build_packet(hb.PKT_HEARTBEAT,payload,key)
        a.sock.sendto(pkt,('127.0.0.1',b.listen_port)); time.sleep(.25)
        self.assertTrue(b.peers.find_by_node_id('node-a').online)

    def test_replayed_app_request_is_rejected(self):
        a=self._engine('node-a','peer-a'); b=self._engine('node-b','peer-b',service=True)
        self._register(a,b); self._register(b,a); self._start_rx(b)
        req=new_request(source=Identity('node-a','peer-a'), destination=Identity('node-b','peer-b'), service='storage.summary', payload={})
        raw=encode_message(req); key=a.crypto.derive_shared_key(b.crypto.pubkey_hex)
        a.sock.sendto(a._build_packet(hb.PKT_APP_REQUEST,raw,key),('127.0.0.1',b.listen_port)); time.sleep(.2)
        a.sock.sendto(a._build_packet(hb.PKT_APP_REQUEST,raw,key),('127.0.0.1',b.listen_port)); time.sleep(.3)
        self.assertGreaterEqual(b.stats.app_replay_rejected,1)

    def test_status_capabilities_additive_and_relay_default_off(self):
        a=self._engine('node-a','peer-a')
        st=a.status_payload(); self.assertTrue(st['app_ready']); self.assertEqual(st['app_protocols'],['HB-TEP-APP/1']); self.assertEqual(st['services'],['storage.summary']); self.assertFalse(st['relay'])
        self.assertEqual(st['app_packet_types']['PKT_APP_REQUEST'],32)

    def test_missing_peer_id_keeps_legacy_but_disables_app(self):
        a=self._engine('node-a','')
        self.assertFalse(a.app_ready); self.assertEqual(a.status_payload()['app_protocols'],[])
        payload=b'{}'; pkt=a._build_packet(hb.PKT_HEARTBEAT,payload,a.crypto.hmac_key); self.assertEqual(a._parse_packet(pkt)['type'],hb.PKT_HEARTBEAT)

    def test_two_hop_trusted_relay_over_real_udp(self):
        a=self._engine('node-a','peer-a')
        r=self._engine('node-r','peer-r',relay=True,relay_clients=['peer-a'])
        b=self._engine('node-b','peer-b',trusted_rendezvous=['peer-r'],service=True)
        for x,y in [(a,r),(r,a),(r,b),(b,r),(b,a),(a,b)]: self._register(x,y)
        r._relay_table.observe(peer_id='peer-b',ip='127.0.0.1',port=b.listen_port,pubkey=b.crypto.pubkey_hex,authenticated=True)
        self._start_rx(a,r,b)
        client=TepRpcClient(local_identity=Identity('node-a','peer-a'), transport=lambda pid, raw, t: a.relay_transport('peer-r',pid,raw,t))
        out=client.request(destination=Identity('node-b','peer-b'),service='storage.summary',payload={},timeout_sec=3)
        self.assertEqual(out['node_id'],'node-b'); self.assertGreaterEqual(r.stats.relay_requests,1); self.assertGreaterEqual(b.stats.relay_requests,1)

if __name__=='__main__': unittest.main()
