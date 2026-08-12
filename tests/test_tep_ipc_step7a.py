from __future__ import annotations
import importlib.util, json, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import unittest
import urllib.error
import urllib.request

from tep.hb_tep_services import StorageSummaryConfig, build_default_registry

ROOT = Path(__file__).resolve().parents[1]
MOD_PATH = ROOT / 'patched' / 'hb_tep.py'
spec = importlib.util.spec_from_file_location('hb_tep_step7a', MOD_PATH)
hb = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hb
spec.loader.exec_module(hb)

class SummaryHandler(BaseHTTPRequestHandler):
    payload = {'available': True, 'node_id': 'node-b', 'role': 'edge', 'capacity_total_gb': 200, 'used_gb': 10, 'timestamp': 0}
    def log_message(self, *a): pass
    def do_GET(self):
        raw = json.dumps(type(self).payload).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)

class Step7AIpcTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.http = HTTPServer(('127.0.0.1', 0), SummaryHandler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.engines = []; self.status_servers = []

    def tearDown(self):
        for server in self.status_servers:
            server.shutdown(); server.server_close()
        for e in self.engines:
            e._stop.set()
            try: e.sock.close()
            except Exception: pass
        self.http.shutdown(); self.http.server_close(); self.tmp.cleanup()

    def _paths(self, name):
        d = self.root / name; d.mkdir()
        hb.STATE_DIR=d; hb.PEERS_FILE=d/'peers.json'; hb.KEY_FILE=d/'node.key'; hb.X25519_KEY=d/'node_x25519.key'; hb.LOG_FILE=d/'tep.log'
        hb.PEERS_FILE.write_text('{"peers":[]}')

    def _engine(self, node, peer, *, relay=False, relay_clients=None, trusted_rendezvous=None, rendezvous=None, service=False):
        self._paths(node); registry=None
        if service:
            registry=build_default_registry(storage_summary_config=StorageSummaryConfig(url=f'http://127.0.0.1:{self.http.server_port}/api/public/storage-summary'))
        e=hb.TepEngine(node_id=node, peer_id=peer, listen_host='127.0.0.1', listen_port=0, status_port=0,
                       relay_enabled=relay, relay_clients=relay_clients or [], trusted_rendezvous=trusted_rendezvous or [],
                       rendezvous_peer_ids=rendezvous or [], service_registry=registry)
        self.engines.append(e); return e

    def _register(self, a, b, *, port=None):
        with a.peers._lock:
            a.peers._peers[b.node_id]=hb.Peer(id=b.node_id, ip='127.0.0.1', port=port or b.listen_port, pubkey=b.crypto.pubkey_hex, peer_id=b.peer_id)

    def _start_rx(self, *engines):
        for e in engines: threading.Thread(target=e._recv_loop, daemon=True).start()

    def _status(self, engine):
        server=engine.start_status_server(); self.status_servers.append(server); return server.server_port

    def _post(self, port, payload, *, content_type='application/json'):
        raw=json.dumps(payload).encode()
        req=urllib.request.Request(f'http://127.0.0.1:{port}/app/storage-summary', data=raw, headers={'Content-Type': content_type}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=5) as r: return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_direct_storage_summary_ipc(self):
        a=self._engine('node-a','peer-a'); b=self._engine('node-b','peer-b',service=True)
        self._register(a,b); self._register(b,a); self._start_rx(a,b)
        status,body=self._post(self._status(a),{'node_id':'node-b','peer_id':'peer-b'})
        self.assertEqual(status,200); self.assertTrue(body['ok']); self.assertEqual(body['summary']['node_id'],'node-b'); self.assertEqual(body['path'],'direct'); self.assertIsNone(body['relay_peer_id'])

    def test_ipc_rejects_routing_controls_and_wrong_method(self):
        a=self._engine('node-a','peer-a'); port=self._status(a)
        status,body=self._post(port,{'node_id':'node-b','peer_id':'peer-b','url':'http://127.0.0.1:5011/api/v0/id'})
        self.assertEqual(status,400); self.assertEqual(body['error']['code'],'bad_request')
        req=urllib.request.Request(f'http://127.0.0.1:{port}/app/storage-summary', method='GET')
        with self.assertRaises(urllib.error.HTTPError) as ctx: urllib.request.urlopen(req, timeout=2)
        self.assertEqual(ctx.exception.code,405)

    def test_ipc_requires_json_and_app_ready(self):
        a=self._engine('node-a',''); port=self._status(a)
        status,body=self._post(port,{'node_id':'node-b','peer_id':'peer-b'})
        self.assertEqual(status,503); self.assertEqual(body['error']['code'],'app_unavailable')
        status,body=self._post(port,{'node_id':'node-b','peer_id':'peer-b'},content_type='text/plain')
        self.assertEqual(status,415); self.assertEqual(body['error']['code'],'unsupported_media_type')

    def test_ipc_direct_timeout_falls_back_to_trusted_rendezvous(self):
        a=self._engine('node-a','peer-a',rendezvous=['peer-r'])
        r=self._engine('node-r','peer-r',relay=True,relay_clients=['peer-a'])
        b=self._engine('node-b','peer-b',trusted_rendezvous=['peer-r'],service=True)
        self._register(a,r); self._register(r,a); self._register(r,b); self._register(b,r); self._register(a,b,port=9)
        r._relay_table.observe(peer_id='peer-b',ip='127.0.0.1',port=b.listen_port,pubkey=b.crypto.pubkey_hex,authenticated=True)
        self._start_rx(a,r,b)
        status,body=self._post(self._status(a),{'node_id':'node-b','peer_id':'peer-b'})
        self.assertEqual(status,200); self.assertEqual(body['summary']['node_id'],'node-b'); self.assertEqual(body['path'],'relay'); self.assertEqual(body['relay_peer_id'],'peer-r')

if __name__=='__main__': unittest.main()
