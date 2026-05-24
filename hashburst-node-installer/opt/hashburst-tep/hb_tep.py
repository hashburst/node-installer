#!/usr/bin/env python3
"""
HB-TEP v2 — HashBurst Transport Encrypted Protocol
Reference: US Patent 11799659B2

UDP tunnel Layer 3 for direct node-to-node reachability
even when DNS/CDN (Cloudflare) is unavailable.

Packet format:
  Magic(4) | Version(1) | Type(1) | NodeID(16) | Nonce(12) | PayloadLen(2)
  EncryptedPayload (AES-256-GCM)

Status HTTP API: http://127.0.0.1:47778/
"""

from __future__ import annotations
import argparse, hashlib, hmac as hmac_mod, json, logging
import os, socket, struct, threading, time
from dataclasses import dataclass, asdict, field
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, PrivateFormat, NoEncryption
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False
    logging.warning("cryptography not installed — using HMAC fallback")

TEP_MAGIC    = b'HBT\x02'
TEP_VERSION  = 2
PKT_HEARTBEAT = 0x01
LISTEN_PORT  = 47777
STATUS_PORT  = 47778
HEARTBEAT_SEC = 10

STATE_DIR  = Path('/var/lib/hashburst/tep')
PEERS_FILE = STATE_DIR / 'peers.json'
KEY_FILE   = STATE_DIR / 'node.key'
X25519_KEY = STATE_DIR / 'node_x25519.key'
LOG_FILE   = Path('/var/log/hashburst/tep.log')
LOG        = logging.getLogger('hb-tep')

@dataclass
class Peer:
    id: str; ip: str; port: int = LISTEN_PORT
    last_seen: float = 0.0; latency_ms: Optional[float] = None
    online: bool = False; pubkey: Optional[str] = None

@dataclass
class TepStats:
    pkts_sent: int = 0; pkts_recv: int = 0; pkts_dropped: int = 0
    peers_online: int = 0; uptime_sec: float = 0.0
    node_id: str = ''; listen_ip: str = '0.0.0.0'; listen_port: int = LISTEN_PORT

class TepCrypto:
    def __init__(self, state_dir: Path):
        state_dir.mkdir(parents=True, exist_ok=True)
        if not KEY_FILE.exists():
            KEY_FILE.write_bytes(os.urandom(32)); KEY_FILE.chmod(0o600)
        self.hmac_key = KEY_FILE.read_bytes()
        self.x25519_priv = None; self.x25519_pub = None
        if HAVE_CRYPTO:
            if not X25519_KEY.exists():
                priv = X25519PrivateKey.generate()
                X25519_KEY.write_bytes(priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))
                X25519_KEY.chmod(0o600)
            raw = X25519_KEY.read_bytes()
            self.x25519_priv = X25519PrivateKey.from_private_bytes(raw)
            self.x25519_pub  = self.x25519_priv.public_key()

    @property
    def pubkey_hex(self) -> str:
        if self.x25519_pub:
            return self.x25519_pub.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        return ''

    def derive_shared_key(self, peer_pubkey_hex: str) -> bytes:
        if not HAVE_CRYPTO or not self.x25519_priv: return self.hmac_key
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
        peer_pub = X25519PublicKey.from_public_bytes(bytes.fromhex(peer_pubkey_hex))
        shared = self.x25519_priv.exchange(peer_pub)
        return hashlib.sha256(b'hb-tep-v2' + shared).digest()

    def encrypt(self, plaintext: bytes, key: bytes, nonce: bytes) -> bytes:
        if HAVE_CRYPTO:
            return AESGCM(key).encrypt(nonce, plaintext, b'hb-tep-aad')
        return plaintext + hmac_mod.new(key, plaintext + nonce, hashlib.sha256).digest()[:16]

    def decrypt(self, ct_tag: bytes, key: bytes, nonce: bytes) -> Optional[bytes]:
        if HAVE_CRYPTO:
            try: return AESGCM(key).decrypt(nonce, ct_tag, b'hb-tep-aad')
            except: return None
        ct, tag = ct_tag[:-16], ct_tag[-16:]
        expected = hmac_mod.new(key, ct + nonce, hashlib.sha256).digest()[:16]
        return ct if hmac_mod.compare_digest(tag, expected) else None

class PeerManager:
    def __init__(self):
        self._lock = threading.Lock(); self._peers: dict = {}; self._load()

    def _load(self):
        if not PEERS_FILE.exists():
            PEERS_FILE.parent.mkdir(parents=True, exist_ok=True)
            PEERS_FILE.write_text(json.dumps({"peers": [
                {"id": "node4-hashburst-io",  "ip": "77.90.188.157", "port": 47777},
                {"id": "node5-blockchainapi", "ip": "64.31.4.9",     "port": 47777},
            ]}, indent=2))
        data = json.loads(PEERS_FILE.read_text())
        for p in data.get('peers', []):
            peer = Peer(id=p['id'], ip=p['ip'], port=int(p.get('port', LISTEN_PORT)), pubkey=p.get('pubkey'))
            self._peers[peer.id] = peer

    def get_all(self): 
        with self._lock: return list(self._peers.values())

    def mark_seen(self, peer_id, latency_ms, pubkey=''):
        with self._lock:
            if peer_id in self._peers:
                p = self._peers[peer_id]
                p.last_seen = time.time(); p.latency_ms = latency_ms; p.online = True
                if pubkey: p.pubkey = pubkey

    def mark_offline(self, peer_id):
        with self._lock:
            if peer_id in self._peers: self._peers[peer_id].online = False

    def add_peer(self, peer_id, ip, port=LISTEN_PORT, pubkey=''):
        with self._lock:
            if peer_id not in self._peers:
                self._peers[peer_id] = Peer(id=peer_id, ip=ip, port=port, pubkey=pubkey)

    def online_count(self): return sum(1 for p in self._peers.values() if p.online)
    def to_json(self):
        with self._lock: return [asdict(p) for p in self._peers.values()]

class TepEngine:
    HEADER_SIZE = 36

    def __init__(self, node_id: str, listen_host: str = '0.0.0.0'):
        self.node_id = node_id; self.node_id_bytes = node_id.encode('ascii','replace')[:16]
        self.listen_host = listen_host; self.crypto = TepCrypto(STATE_DIR)
        self.peers = PeerManager(); self.stats = TepStats(node_id=node_id)
        self._counter = 0; self._start_time = time.time(); self._stop = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((listen_host, LISTEN_PORT)); self.sock.settimeout(2.0)
        LOG.info("HB-TEP started | node=%s pubkey=%s", node_id, self.crypto.pubkey_hex[:16]+'...')

    def _next_counter(self): self._counter += 1; return self._counter

    def _get_shared_key(self, peer: Peer) -> bytes:
        if peer.pubkey and HAVE_CRYPTO:
            try: return self.crypto.derive_shared_key(peer.pubkey)
            except: pass
        return self.crypto.hmac_key

    def _build_packet(self, pkt_type, payload, key):
        nonce = struct.pack('>Q', int(time.time())) + struct.pack('>I', self._next_counter() & 0xFFFFFFFF)
        encrypted = self.crypto.encrypt(payload, key, nonce)
        header = TEP_MAGIC + bytes([TEP_VERSION, pkt_type]) + self.node_id_bytes[:16].ljust(16, b'\x00') + nonce + struct.pack('>H', len(encrypted))
        return header + encrypted

    def _parse_packet(self, data):
        if len(data) < self.HEADER_SIZE or data[:4] != TEP_MAGIC: return None
        return {'version': data[4], 'type': data[5], 'node_id': data[6:22].rstrip(b'\x00').decode('ascii','replace'),
                'nonce': data[22:34], 'enc_payload': data[36:36+struct.unpack('>H', data[34:36])[0]]}

    def _heartbeat_loop(self):
        while not self._stop.is_set():
            for peer in self.peers.get_all():
                payload = json.dumps({'type':'heartbeat','ts':int(time.time()),'node':self.node_id,'pubkey':self.crypto.pubkey_hex}).encode()
                key = self._get_shared_key(peer)
                pkt = self._build_packet(PKT_HEARTBEAT, payload, key)
                try:
                    self.sock.sendto(pkt, (peer.ip, peer.port)); self.stats.pkts_sent += 1
                except: self.peers.mark_offline(peer.id); self.stats.pkts_dropped += 1
            self.stats.peers_online = self.peers.online_count()
            self.stats.uptime_sec = time.time() - self._start_time
            self._stop.wait(HEARTBEAT_SEC)

    def _recv_loop(self):
        while not self._stop.is_set():
            try: data, addr = self.sock.recvfrom(65535)
            except socket.timeout: continue
            except OSError: break
            self.stats.pkts_recv += 1
            parsed = self._parse_packet(data)
            if not parsed: self.stats.pkts_dropped += 1; continue
            peer_id = parsed['node_id']
            peer = next((p for p in self.peers.get_all() if p.id == peer_id or p.ip == addr[0]), None)
            if peer is None: self.peers.add_peer(peer_id, addr[0], addr[1]); peer = next(p for p in self.peers.get_all() if p.id == peer_id)
            key = self._get_shared_key(peer)
            plain = self.crypto.decrypt(parsed['enc_payload'], key, parsed['nonce'])
            if plain is None: self.stats.pkts_dropped += 1; continue
            try:
                msg = json.loads(plain.decode())
                self.peers.mark_seen(peer_id, latency_ms=0.0, pubkey=msg.get('pubkey',''))
            except: self.stats.pkts_dropped += 1

    def run(self):
        threading.Thread(target=self._heartbeat_loop, daemon=True, name='tep-hb').start()
        threading.Thread(target=self._recv_loop,      daemon=True, name='tep-rx').start()
        engine = self

        class StatusHandler(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                engine.stats.peers_online = engine.peers.online_count()
                engine.stats.uptime_sec = time.time() - engine._start_time
                body = json.dumps({'node_id': engine.node_id, 'pubkey': engine.crypto.pubkey_hex,
                                   'stats': asdict(engine.stats), 'peers': engine.peers.to_json(),
                                   'crypto_mode': 'AES-256-GCM' if HAVE_CRYPTO else 'HMAC-fallback'}, indent=2).encode()
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(body)

        threading.Thread(target=HTTPServer(('127.0.0.1', STATUS_PORT), StatusHandler).serve_forever, daemon=True, name='tep-status').start()
        LOG.info("TEP status: http://127.0.0.1:%d", STATUS_PORT)
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt:
            self._stop.set(); LOG.info("HB-TEP stopped")

def main():
    ap = argparse.ArgumentParser(description='HB-TEP — HashBurst Transport Encrypted Protocol')
    ap.add_argument('--node-id', default=os.uname().nodename)
    ap.add_argument('--listen',  default='0.0.0.0')
    ap.add_argument('--log-level', default='INFO', choices=['DEBUG','INFO','WARNING','ERROR'])
    args = ap.parse_args()
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
                        handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()])
    TepEngine(node_id=args.node_id, listen_host=args.listen).run()

if __name__ == '__main__':
    main()
