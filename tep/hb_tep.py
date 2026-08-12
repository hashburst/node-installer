#!/usr/bin/env python3
"""
HB-TEP v2.1 — HashBurst Transport Encrypted Protocol
Reference: US Patent 11799659B2

Novità v2.1 rispetto a v2.0:
- Discovery peer dalla blockchain (DNS distribuito) invece di solo peers.json
- Il nodo blockchain espone /api/tep/peers con i peer registrati
- peers.json viene aggiornato periodicamente dal nodo Go (tepPeerSyncLoop)
- Fallback automatico a peers.json se il nodo non è raggiungibile
- La TEP pubkey X25519 viene letta e esposta per la registrazione blockchain
"""

from __future__ import annotations
import argparse, hashlib, hmac as hmac_mod, json, logging
import os, socket, struct, threading, time, urllib.request
import base64
from dataclasses import dataclass, asdict, field
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional

from tep.hb_tep_app import (
    Identity, ProtocolError, ReplayCache, decode_message, encode_message,
    new_error, new_response,
)
from tep.hb_tep_services import ServiceError, build_default_registry
from tep.hb_tep_client import TepClientError, TepRpcClient
from tep.hb_tep_relay import (
    RelayError, RelayPolicy, RelayTable, RelayDispatcher, FailoverTepTransport,
    new_relay_request,
)

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PublicFormat, PrivateFormat, NoEncryption
    )
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

TEP_MAGIC     = b'HBT\x02'
TEP_VERSION   = 2
PKT_HEARTBEAT      = 0x01
# HB-TEP-APP/1 packet family. 0x20-0x24 are collision-free in production v2.1.
PKT_APP_REQUEST     = 0x20
PKT_APP_RESPONSE    = 0x21
PKT_APP_ERROR       = 0x22
PKT_RELAY_REQUEST   = 0x23
PKT_RELAY_RESPONSE  = 0x24
APP_PACKET_TYPES = frozenset({
    PKT_APP_REQUEST, PKT_APP_RESPONSE, PKT_APP_ERROR,
    PKT_RELAY_REQUEST, PKT_RELAY_RESPONSE,
})
LISTEN_PORT   = 47777
STATUS_PORT   = 47778
HEARTBEAT_SEC = 10
DNS_SYNC_SEC  = 60      # intervallo sincronizzazione peers dalla blockchain
IPC_MAX_REQUEST_BYTES = 8 * 1024
IPC_MAX_RESPONSE_BYTES = 32 * 1024
IPC_TIMEOUT_SEC = 3.0
IPC_DIRECT_TIMEOUT_SEC = 1.2
IPC_MAX_RELAY_ATTEMPTS = 2

STATE_DIR  = Path('/var/lib/hashburst/tep')
PEERS_FILE = STATE_DIR / 'peers.json'
KEY_FILE   = STATE_DIR / 'node.key'
X25519_KEY = STATE_DIR / 'node_x25519.key'
LOG_FILE   = Path('/var/log/hashburst/tep.log')
LOG        = logging.getLogger('hb-tep')

# URL dell'API blockchain locale per ottenere i peer (DNS distribuito)
BLOCKCHAIN_PEERS_API = "http://127.0.0.1:{rpc_port}/api/tep/peers"

@dataclass
class Peer:
    id: str
    ip: str
    port: int = LISTEN_PORT
    last_seen: float = 0.0
    latency_ms: Optional[float] = None
    online: bool = False
    pubkey: Optional[str] = None
    peer_id: Optional[str] = None  # libp2p peer ID

@dataclass
class TepStats:
    pkts_sent: int = 0
    pkts_recv: int = 0
    pkts_dropped: int = 0
    peers_online: int = 0
    peers_total: int = 0
    uptime_sec: float = 0.0
    node_id: str = ''
    listen_ip: str = '0.0.0.0'
    listen_port: int = LISTEN_PORT
    dns_source: str = 'static'  # 'blockchain' | 'static'
    app_requests: int = 0
    app_responses: int = 0
    app_errors: int = 0
    app_auth_rejected: int = 0
    app_replay_rejected: int = 0
    relay_requests: int = 0

class TepCrypto:
    def __init__(self, state_dir: Path):
        state_dir.mkdir(parents=True, exist_ok=True)
        if not KEY_FILE.exists():
            KEY_FILE.write_bytes(os.urandom(32))
            KEY_FILE.chmod(0o600)
        self.hmac_key = KEY_FILE.read_bytes()
        self.x25519_priv = None
        self.x25519_pub  = None
        if HAVE_CRYPTO:
            if not X25519_KEY.exists():
                priv = X25519PrivateKey.generate()
                X25519_KEY.write_bytes(
                    priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))
                X25519_KEY.chmod(0o600)
            self.x25519_priv = X25519PrivateKey.from_private_bytes(X25519_KEY.read_bytes())
            self.x25519_pub  = self.x25519_priv.public_key()

    @property
    def pubkey_hex(self) -> str:
        if self.x25519_pub:
            return self.x25519_pub.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        return ''

    def derive_shared_key(self, peer_pubkey_hex: str) -> bytes:
        if not HAVE_CRYPTO or not self.x25519_priv:
            return self.hmac_key
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
        peer_pub = X25519PublicKey.from_public_bytes(bytes.fromhex(peer_pubkey_hex))
        shared   = self.x25519_priv.exchange(peer_pub)
        return hashlib.sha256(b'hb-tep-v2' + shared).digest()

    def encrypt(self, plaintext: bytes, key: bytes, nonce: bytes) -> bytes:
        if HAVE_CRYPTO:
            return AESGCM(key).encrypt(nonce, plaintext, b'hb-tep-aad')
        return plaintext + hmac_mod.new(key, plaintext+nonce, hashlib.sha256).digest()[:16]

    def decrypt(self, ct_tag: bytes, key: bytes, nonce: bytes) -> Optional[bytes]:
        if HAVE_CRYPTO:
            try:
                return AESGCM(key).decrypt(nonce, ct_tag, b'hb-tep-aad')
            except:
                return None
        ct, tag = ct_tag[:-16], ct_tag[-16:]
        expected = hmac_mod.new(key, ct+nonce, hashlib.sha256).digest()[:16]
        return ct if hmac_mod.compare_digest(tag, expected) else None

class PeerManager:
    """
    Gestisce i peer TEP con due sorgenti:
    1. Blockchain DNS: /api/tep/peers del nodo locale (fonte primaria)
    2. peers.json: file statico come fallback e bootstrap iniziale

    La blockchain diventa il DNS distribuito — nessun DNS esterno necessario.
    """

    def __init__(self, rpc_port: int = 8009):
        self._lock   = threading.Lock()
        self._peers: dict = {}
        self._rpc_port = rpc_port
        self._dns_source = 'static'
        self._load_static()

    def _load_static(self):
        """Carica peers.json come bootstrap iniziale o fallback."""
        if not PEERS_FILE.exists():
            PEERS_FILE.parent.mkdir(parents=True, exist_ok=True)
            PEERS_FILE.write_text(json.dumps({"peers": []}, indent=2))
            return
        try:
            data = json.loads(PEERS_FILE.read_text())
            with self._lock:
                for p in data.get('peers', []):
                    peer = Peer(
                        id=p['id'], ip=p['ip'],
                        port=int(p.get('port', LISTEN_PORT)),
                        pubkey=p.get('pubkey'),
                        peer_id=p.get('peer_id')
                    )
                    self._peers[peer.id] = peer
            LOG.info("Static peers loaded: %d", len(self._peers))
        except Exception as e:
            LOG.error("Failed to load static peers: %s", e)

    def sync_from_blockchain(self) -> bool:
        """
        Sincronizza i peer dalla blockchain locale.
        Ritorna True se la sincronizzazione è riuscita.

        Questo implementa il DNS distribuito:
        invece di leggere DNS → IP, leggiamo blockchain → peer TEP.
        """
        try:
            url = BLOCKCHAIN_PEERS_API.format(rpc_port=self._rpc_port)
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.loads(r.read())

            peers_data = data.get('peers', [])
            if not peers_data:
                return False

            with self._lock:
                # SOSTITUZIONE, non merge: la blockchain e' la fonte autorevole.
                fresh = {}
                for p in peers_data:
                    peer_id = p.get('id', '')
                    if not peer_id or not p.get('ip'):
                        continue
                    prev = self._peers.get(peer_id)
                    if prev is None:
                        LOG.info("BlockchainDNS: new peer discovered: %s (%s)",
                                 peer_id, p['ip'])
                    fresh[peer_id] = Peer(
                        id=peer_id,
                        ip=p['ip'],
                        port=int(p.get('port', LISTEN_PORT)),
                        pubkey=p.get('pubkey'),
                        peer_id=p.get('peer_id'),
                        last_seen=prev.last_seen if prev else 0.0,
                        online=prev.online if prev else False,
                    )
                dropped = set(self._peers) - set(fresh)
                for gone in dropped:
                    LOG.info("BlockchainDNS: peer rimosso (non nel registro): %s", gone)
                self._peers = fresh

            self._dns_source = 'blockchain'
            LOG.info("BlockchainDNS: synced %d peers", len(peers_data))
            return True

        except Exception as e:
            LOG.debug("BlockchainDNS sync failed (using static fallback): %s", e)
            self._dns_source = 'static'
            return False

    def get_all(self) -> list:
        with self._lock:
            return list(self._peers.values())

    def mark_seen(self, peer_id: str, latency_ms: float, pubkey: str = ''):
        with self._lock:
            if peer_id in self._peers:
                p = self._peers[peer_id]
                p.last_seen   = time.time()
                p.latency_ms  = latency_ms
                p.online      = True
                if pubkey:
                    p.pubkey  = pubkey

    def mark_offline(self, peer_id: str):
        with self._lock:
            if peer_id in self._peers:
                self._peers[peer_id].online = False

    def add_peer(self, peer_id: str, ip: str, port: int = LISTEN_PORT, pubkey: str = ''):
        with self._lock:
            if peer_id not in self._peers:
                LOG.info("New peer discovered via TEP: %s (%s:%d)", peer_id, ip, port)
                self._peers[peer_id] = Peer(id=peer_id, ip=ip, port=port, pubkey=pubkey)

    def find_by_node_id(self, node_id: str) -> Optional[Peer]:
        with self._lock:
            return self._peers.get(node_id)

    def find_by_wire_node_id(self, wire_node_id: str) -> Optional[Peer]:
        """Resolve the legacy 16-byte header id only when registry match is unique."""
        with self._lock:
            matches = []
            for peer in self._peers.values():
                candidate = peer.id.encode('ascii', 'replace')[:16].decode('ascii', 'replace')
                if candidate == wire_node_id:
                    matches.append(peer)
            return matches[0] if len(matches) == 1 else None

    def find_by_peer_id(self, peer_id: str) -> Optional[Peer]:
        with self._lock:
            for peer in self._peers.values():
                if peer.peer_id == peer_id:
                    return peer
        return None

    def update_authenticated_endpoint(self, node_id: str, ip: str, port: int) -> bool:
        """Update transport coordinates only after caller authenticated the packet."""
        with self._lock:
            peer = self._peers.get(node_id)
            if peer is None:
                return False
            peer.ip = ip
            peer.port = int(port)
            return True

    def online_count(self) -> int:
        return sum(1 for p in self._peers.values() if p.online)

    def to_json(self) -> list:
        with self._lock:
            return [asdict(p) for p in self._peers.values()]

    @property
    def dns_source(self) -> str:
        return self._dns_source

class TepEngine:
    HEADER_SIZE = 36

    def __init__(self, node_id: str, listen_host: str = '0.0.0.0', rpc_port: int = 8009,
                 peer_id: str = '', listen_port: int = LISTEN_PORT,
                 status_port: int = STATUS_PORT, relay_enabled: bool = False,
                 relay_clients: Optional[list[str]] = None,
                 trusted_rendezvous: Optional[list[str]] = None,
                 rendezvous_peer_ids: Optional[list[str]] = None,
                 service_registry=None):
        self.node_id       = node_id
        self.node_id_bytes = node_id.encode('ascii', 'replace')[:16]
        self.peer_id       = str(peer_id or '').strip()
        self.listen_host   = listen_host
        self.listen_port   = int(listen_port)
        self.status_port   = int(status_port)
        self.crypto        = TepCrypto(STATE_DIR)
        self.peers         = PeerManager(rpc_port=rpc_port)
        self.stats         = TepStats(node_id=node_id, listen_ip=listen_host, listen_port=self.listen_port)
        self._counter      = 0
        self._start_time   = time.time()
        self._stop         = threading.Event()
        self._replay       = ReplayCache()
        self._services     = service_registry or build_default_registry()
        self._relay_table  = RelayTable()
        self._relay_enabled = bool(relay_enabled)
        self._relay_clients = tuple(relay_clients or ())
        self._trusted_rendezvous = tuple(trusted_rendezvous or ())
        self._rendezvous_peer_ids = tuple(dict.fromkeys(str(x).strip() for x in (rendezvous_peer_ids or ()) if str(x).strip()))
        self._app_pending: dict[str, dict] = {}
        self._app_pending_lock = threading.Lock()

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((listen_host, self.listen_port))
        self.listen_port = int(self.sock.getsockname()[1])
        self.stats.listen_port = self.listen_port
        self.sock.settimeout(0.2)

        LOG.info("HB-TEP v2.1 started | node=%s | crypto=%s | pubkey=%s | app_ready=%s",
                 node_id,
                 'AES-256-GCM' if HAVE_CRYPTO else 'HMAC-fallback',
                 self.crypto.pubkey_hex[:16]+'...', self.app_ready)

    def _next_counter(self) -> int:
        self._counter += 1
        return self._counter

    def _get_shared_key(self, peer: Peer) -> bytes:
        if peer.pubkey and HAVE_CRYPTO:
            try:
                return self.crypto.derive_shared_key(peer.pubkey)
            except:
                pass
        return self.crypto.hmac_key

    def _build_packet(self, pkt_type: int, payload: bytes, key: bytes) -> bytes:
        nonce = (struct.pack('>Q', int(time.time())) +
                 struct.pack('>I', self._next_counter() & 0xFFFFFFFF))
        encrypted = self.crypto.encrypt(payload, key, nonce)
        header = (TEP_MAGIC +
                  bytes([TEP_VERSION, pkt_type]) +
                  self.node_id_bytes[:16].ljust(16, b'\x00') +
                  nonce +
                  struct.pack('>H', len(encrypted)))
        return header + encrypted

    def _parse_packet(self, data: bytes) -> Optional[dict]:
        if len(data) < self.HEADER_SIZE or data[:4] != TEP_MAGIC:
            return None
        plen = struct.unpack('>H', data[34:36])[0]
        return {
            'version':     data[4],
            'type':        data[5],
            'node_id':     data[6:22].rstrip(b'\x00').decode('ascii', 'replace'),
            'nonce':       data[22:34],
            'enc_payload': data[36:36+plen],
        }

    @property
    def app_ready(self) -> bool:
        return bool(HAVE_CRYPTO and self.peer_id and self.crypto.x25519_priv and self.crypto.pubkey_hex)

    @property
    def local_identity(self) -> Identity:
        return Identity(node_id=self.node_id, peer_id=self.peer_id)

    def _app_peer_for_packet(self, parsed: dict) -> Optional[Peer]:
        """APP/relay requires a registry peer with TEP pubkey and stable peer_id."""
        peer = self.peers.find_by_wire_node_id(parsed['node_id'])
        if peer is None or not peer.pubkey or not peer.peer_id or not HAVE_CRYPTO:
            return None
        return peer

    def _validate_app_identity(self, env, peer: Peer) -> None:
        if env.source.node_id != peer.id or env.source.peer_id != peer.peer_id:
            raise ProtocolError('identity_mismatch', 'application source does not match authenticated TEP peer')
        if env.destination.node_id != self.node_id or env.destination.peer_id != self.peer_id:
            raise ProtocolError('identity_mismatch', 'application destination does not match local TEP identity')

    def _send_plain_to_peer(self, peer: Peer, pkt_type: int, plain: bytes,
                            addr: Optional[tuple[str, int]] = None) -> None:
        if not peer.pubkey or not HAVE_CRYPTO:
            raise ProtocolError('authentication_failed', 'APP transport requires registered X25519 peer key')
        key = self.crypto.derive_shared_key(peer.pubkey)
        pkt = self._build_packet(pkt_type, plain, key)
        self.sock.sendto(pkt, addr or (peer.ip, peer.port))
        self.stats.pkts_sent += 1

    def _pending_create(self, request_id: str) -> dict:
        item = {'event': threading.Event(), 'raw': None}
        with self._app_pending_lock:
            if request_id in self._app_pending:
                raise ProtocolError('duplicate_request_id', 'request already pending')
            if len(self._app_pending) >= 256:
                raise ProtocolError('client_overloaded', 'too many pending APP requests')
            self._app_pending[request_id] = item
        return item

    def _pending_complete(self, request_id: str, raw: bytes) -> bool:
        with self._app_pending_lock:
            item = self._app_pending.get(request_id)
            if item is None:
                return False
            item['raw'] = raw
            item['event'].set()
            return True

    def _pending_remove(self, request_id: str) -> None:
        with self._app_pending_lock:
            self._app_pending.pop(request_id, None)

    def app_transport(self, destination_peer_id: str, raw_request: bytes,
                      timeout_sec: float, *, addr_override=None) -> bytes:
        """TransportCallable for TepRpcClient using the daemon's existing UDP socket/crypto."""
        if not self.app_ready:
            raise ProtocolError('app_unavailable', 'HB-TEP-APP/1 is not ready')
        env = decode_message(raw_request)
        if env.msg_type != 'req':
            raise ProtocolError('unsupported_type', 'app_transport accepts req only')
        if env.source.node_id != self.node_id or env.source.peer_id != self.peer_id:
            raise ProtocolError('identity_mismatch', 'local request source identity mismatch')
        if env.destination.peer_id != destination_peer_id:
            raise ProtocolError('identity_mismatch', 'destination peer mismatch')
        peer = self.peers.find_by_peer_id(destination_peer_id)
        if peer is None or not peer.pubkey:
            raise ProtocolError('destination_unknown', 'destination peer is not registered for APP')
        pending = self._pending_create(env.request_id)
        try:
            self._send_plain_to_peer(peer, PKT_APP_REQUEST, bytes(raw_request), addr=addr_override)
            if not pending['event'].wait(float(timeout_sec)):
                raise TimeoutError('TEP APP request timed out')
            return bytes(pending['raw'])
        finally:
            self._pending_remove(env.request_id)

    def relay_transport(self, rendezvous_peer_id: str, target_peer_id: str,
                        raw_request: bytes, timeout_sec: float) -> bytes:
        if not self.app_ready:
            raise ProtocolError('app_unavailable', 'HB-TEP-APP/1 is not ready')
        rendezvous = self.peers.find_by_peer_id(rendezvous_peer_id)
        if rendezvous is None or not rendezvous.pubkey:
            raise ProtocolError('destination_unknown', 'rendezvous peer is not registered')
        relay_msg = new_relay_request(
            source=self.local_identity,
            rendezvous=Identity(node_id=rendezvous.id, peer_id=rendezvous.peer_id),
            target_peer_id=target_peer_id,
            inner_request=bytes(raw_request),
            ttl_ms=min(5000, max(1, int(float(timeout_sec) * 1000))),
        )
        encoded = encode_message(relay_msg)
        pending = self._pending_create(relay_msg['request_id'])
        try:
            self._send_plain_to_peer(rendezvous, PKT_RELAY_REQUEST, encoded)
            if not pending['event'].wait(float(timeout_sec)):
                raise TimeoutError('TEP relay request timed out')
            env = decode_message(bytes(pending['raw']))
            if env.msg_type != 'relay_res':
                raise ProtocolError('bad_response', 'rendezvous did not return relay_res')
            inner = env.raw.get('payload', {}).get('inner', '')
            try:
                return base64.urlsafe_b64decode(inner.encode('ascii'))
            except Exception as exc:
                raise ProtocolError('bad_response', 'invalid relay response payload') from exc
        finally:
            self._pending_remove(relay_msg['request_id'])

    def storage_summary_rpc(self, node_id: str, peer_id: str) -> dict:
        """Local-only IPC operation: fixed HB-TEP storage.summary RPC.

        The caller selects only a registered TEP identity. Service, payload,
        transport policy, timeouts and relay candidates are daemon-controlled.
        """
        node_id = str(node_id or '').strip()
        peer_id = str(peer_id or '').strip()
        if not node_id or len(node_id) > 128:
            raise ProtocolError('bad_request', 'invalid node_id')
        if not peer_id or len(peer_id) > 256:
            raise ProtocolError('bad_request', 'invalid peer_id')
        if not self.app_ready:
            raise ProtocolError('app_unavailable', 'HB-TEP-APP/1 is not ready')

        transport = FailoverTepTransport(
            direct=self.app_transport,
            relay=self.relay_transport,
            relay_peer_ids=self._rendezvous_peer_ids,
            direct_timeout_sec=IPC_DIRECT_TIMEOUT_SEC,
            max_relay_attempts=IPC_MAX_RELAY_ATTEMPTS,
        )
        client = TepRpcClient(local_identity=self.local_identity, transport=transport)
        summary = client.request(
            destination=Identity(node_id=node_id, peer_id=peer_id),
            service='storage.summary',
            payload={},
            timeout_sec=IPC_TIMEOUT_SEC,
        )
        return {
            'summary': summary,
            'path': transport.last_path,
            'relay_peer_id': transport.last_relay_peer_id,
        }

    def _send_app_error(self, request_env, peer: Peer, addr, code: str, message: str,
                        status: int = 400) -> None:
        try:
            err = new_error(request_env.raw, source=self.local_identity,
                            destination=request_env.source, code=code,
                            message_text=message[:160], status=status)
            self._send_plain_to_peer(peer, PKT_APP_ERROR, encode_message(err), addr=addr)
            self.stats.app_errors += 1
        except Exception:
            self.stats.pkts_dropped += 1

    def _handle_app_request(self, env, peer: Peer, addr) -> None:
        try:
            self._replay.check_and_add(env.source.peer_id, env.request_id)
            result = self._services.dispatch(env)
            response = new_response(env.raw, source=self.local_identity,
                                    destination=env.source, payload=result)
            self._send_plain_to_peer(peer, PKT_APP_RESPONSE, encode_message(response), addr=addr)
            self.stats.app_requests += 1
            self.stats.app_responses += 1
        except ProtocolError as exc:
            if exc.code == 'replay_detected':
                self.stats.app_replay_rejected += 1
            self._send_app_error(env, peer, addr, exc.code, exc.message, status=400)
        except ServiceError as exc:
            self._send_app_error(env, peer, addr, exc.code, exc.message, status=503)
        except Exception:
            self._send_app_error(env, peer, addr, 'internal_error', 'application service failed', status=500)

    def _relay_roundtrip(self, peer: Peer, relay_message: dict, timeout_sec: float,
                         addr_override=None) -> bytes:
        encoded = encode_message(relay_message)
        pending = self._pending_create(relay_message['request_id'])
        try:
            self._send_plain_to_peer(peer, PKT_RELAY_REQUEST, encoded, addr=addr_override)
            if not pending['event'].wait(float(timeout_sec)):
                raise TimeoutError('TEP relay hop timed out')
            return bytes(pending['raw'])
        finally:
            self._pending_remove(relay_message['request_id'])

    def _relay_forward_to_target(self, target_peer_id: str, inner_raw: bytes,
                                 timeout_sec: float, route) -> bytes:
        target = self.peers.find_by_peer_id(target_peer_id)
        if target is None or not target.pubkey:
            raise RelayError('destination_unknown', 'relay target is not registered')
        forward = new_relay_request(
            source=self.local_identity,
            rendezvous=Identity(node_id=target.id, peer_id=target.peer_id),
            target_peer_id=target_peer_id,
            inner_request=inner_raw,
            ttl_ms=min(5000, max(1, int(float(timeout_sec) * 1000))),
        )
        raw_outer = self._relay_roundtrip(target, forward, timeout_sec,
                                          addr_override=(route.ip, route.port))
        outer_env = decode_message(raw_outer)
        if outer_env.msg_type != 'relay_res':
            raise RelayError('bad_response', 'relay target did not return relay_res')
        inner = outer_env.raw.get('payload', {}).get('inner', '')
        try:
            return base64.urlsafe_b64decode(str(inner).encode('ascii'))
        except Exception as exc:
            raise RelayError('bad_response', 'relay target returned invalid inner response') from exc

    def _handle_relay_delivery(self, env, peer: Peer, addr) -> None:
        """Handle rendezvous -> target hop; the rendezvous is trusted infrastructure."""
        if env.source.peer_id not in self._trusted_rendezvous:
            self.stats.pkts_dropped += 1
            return
        try:
            inner_text = str(env.raw.get('payload', {}).get('inner', ''))
            inner_raw = base64.urlsafe_b64decode(inner_text.encode('ascii'))
            inner_env = decode_message(inner_raw)
            if inner_env.msg_type != 'req':
                raise ProtocolError('unsupported_type', 'relay inner must be req')
            if inner_env.destination.node_id != self.node_id or inner_env.destination.peer_id != self.peer_id:
                raise ProtocolError('identity_mismatch', 'relay inner destination mismatch')
            self._replay.check_and_add(inner_env.source.peer_id, inner_env.request_id)
            result = self._services.dispatch(inner_env)
            inner_response = new_response(inner_env.raw, source=self.local_identity,
                                          destination=inner_env.source, payload=result)
            from tep.hb_tep_relay import new_relay_response
            outer_response = new_relay_response(
                env.raw, source=self.local_identity, destination=env.source,
                target_peer_id=self.peer_id, inner_response=encode_message(inner_response))
            self._send_plain_to_peer(peer, PKT_RELAY_RESPONSE, encode_message(outer_response), addr=addr)
            self.stats.relay_requests += 1
            self.stats.app_requests += 1
            self.stats.app_responses += 1
        except Exception:
            self.stats.pkts_dropped += 1

    def _handle_relay_request(self, env, peer: Peer, addr) -> None:
        target_peer = str(env.raw.get('relay_target', {}).get('peer_id', ''))
        # Second hop: trusted rendezvous is delivering a request to this node.
        if target_peer == self.peer_id:
            self._handle_relay_delivery(env, peer, addr)
            return
        # First hop: this node is acting as rendezvous for an authorized client.
        if not self._relay_enabled or env.source.peer_id not in self._relay_clients:
            self.stats.pkts_dropped += 1
            return
        registered = [p.peer_id for p in self.peers.get_all() if p.peer_id and p.pubkey]
        try:
            self._replay.check_and_add(env.source.peer_id, env.request_id)
            policy = RelayPolicy(trusted_sources=self._relay_clients,
                                 registered_targets=registered)
            # Validate outer/inner and routing policy using the Step 4 dispatcher,
            # but perform the second hop as relay_req rather than raw APP_REQUEST.
            inner_text = str(env.raw.get('payload', {}).get('inner', ''))
            inner_raw = base64.urlsafe_b64decode(inner_text.encode('ascii'))
            inner_env = decode_message(inner_raw)
            if inner_env.msg_type != 'req' or inner_env.destination.peer_id != target_peer:
                raise RelayError('identity_mismatch', 'relay inner destination mismatch')
            policy.authorize(source_peer_id=env.source.peer_id,
                             target_peer_id=target_peer,
                             inner_service=str(inner_env.service or ''))
            route = self._relay_table.get(target_peer)
            budget = min(5.0, max(0.001, float(env.ttl_ms or 1) / 1000.0))
            inner_response_raw = self._relay_forward_to_target(target_peer, inner_raw, budget, route)
            from tep.hb_tep_relay import new_relay_response
            outer_response = new_relay_response(
                env.raw, source=self.local_identity, destination=env.source,
                target_peer_id=target_peer, inner_response=inner_response_raw)
            self._send_plain_to_peer(peer, PKT_RELAY_RESPONSE, encode_message(outer_response), addr=addr)
            self.stats.relay_requests += 1
        except (ProtocolError, RelayError, TimeoutError):
            self.stats.pkts_dropped += 1
        except Exception:
            self.stats.pkts_dropped += 1

    def _handle_authenticated_app_packet(self, parsed: dict, peer: Peer, plain: bytes, addr) -> None:
        try:
            if parsed.get('version') != TEP_VERSION:
                raise ProtocolError('unsupported_version', 'unsupported TEP transport version')
            env = decode_message(plain)
            self._validate_app_identity(env, peer)
            expected = {
                PKT_APP_REQUEST: 'req',
                PKT_APP_RESPONSE: 'res',
                PKT_APP_ERROR: 'err',
                PKT_RELAY_REQUEST: 'relay_req',
                PKT_RELAY_RESPONSE: 'relay_res',
            }.get(parsed['type'])
            if env.msg_type != expected:
                raise ProtocolError('packet_envelope_mismatch', 'TEP packet type does not match APP envelope')
            self.peers.update_authenticated_endpoint(peer.id, addr[0], addr[1])
            self._relay_table.observe(peer_id=peer.peer_id, ip=addr[0], port=addr[1],
                                      pubkey=peer.pubkey or '', authenticated=True)
            self.peers.mark_seen(peer.id, latency_ms=0.0)
            if env.msg_type == 'req':
                threading.Thread(target=self._handle_app_request, args=(env, peer, addr), daemon=True).start()
            elif env.msg_type == 'relay_req':
                threading.Thread(target=self._handle_relay_request, args=(env, peer, addr), daemon=True).start()
            else:
                if not self._pending_complete(env.request_id, plain):
                    self.stats.pkts_dropped += 1
        except ProtocolError as exc:
            self.stats.pkts_dropped += 1
            if exc.code in {'identity_mismatch', 'authentication_failed'}:
                self.stats.app_auth_rejected += 1
            LOG.warning('APP packet rejected from %s/%s: %s', parsed.get('node_id'), addr, exc.code)

    def _heartbeat_loop(self):
        while not self._stop.is_set():
            for peer in self.peers.get_all():
                payload = json.dumps({
                    'type':   'heartbeat',
                    'ts':     int(time.time()),
                    'node':   self.node_id,
                    'pubkey': self.crypto.pubkey_hex,
                }).encode()
                key = self._get_shared_key(peer)
                pkt = self._build_packet(PKT_HEARTBEAT, payload, key)
                try:
                    self.sock.sendto(pkt, (peer.ip, peer.port))
                    self.stats.pkts_sent += 1
                except:
                    self.peers.mark_offline(peer.id)
                    self.stats.pkts_dropped += 1

            self.stats.peers_online = self.peers.online_count()
            self.stats.peers_total  = len(self.peers.get_all())
            self.stats.uptime_sec   = time.time() - self._start_time
            self.stats.dns_source   = self.peers.dns_source
            self._stop.wait(HEARTBEAT_SEC)

    def _recv_loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break

            self.stats.pkts_recv += 1
            parsed = self._parse_packet(data)
            if not parsed:
                self.stats.pkts_dropped += 1
                continue

            peer_id = parsed['node_id']

            # HB-TEP-APP/1 is fail-closed: only a pre-registered peer with
            # pubkey + stable peer_id may reach APP/relay dispatch. No dynamic
            # peer creation occurs before cryptographic authentication.
            if parsed['type'] in APP_PACKET_TYPES:
                if not self.app_ready:
                    self.stats.pkts_dropped += 1
                    continue
                peer = self._app_peer_for_packet(parsed)
                if peer is None:
                    self.stats.pkts_dropped += 1
                    self.stats.app_auth_rejected += 1
                    continue
                try:
                    key = self.crypto.derive_shared_key(peer.pubkey)
                except Exception:
                    self.stats.pkts_dropped += 1
                    self.stats.app_auth_rejected += 1
                    continue
                plain = self.crypto.decrypt(parsed['enc_payload'], key, parsed['nonce'])
                if plain is None:
                    self.stats.pkts_dropped += 1
                    self.stats.app_auth_rejected += 1
                    LOG.warning("APP auth failed from %s/%s", peer_id, addr)
                    continue
                self._handle_authenticated_app_packet(parsed, peer, plain, addr)
                continue

            # Legacy heartbeat behavior is retained for backward compatibility.
            peer = next((p for p in self.peers.get_all()
                         if p.id == peer_id or p.ip == addr[0]), None)
            if peer is None:
                self.peers.add_peer(peer_id, addr[0], addr[1])
                peer = next(p for p in self.peers.get_all() if p.id == peer_id)

            key   = self._get_shared_key(peer)
            plain = self.crypto.decrypt(parsed['enc_payload'], key, parsed['nonce'])
            if plain is None:
                self.stats.pkts_dropped += 1
                LOG.warning("Auth failed from %s/%s", peer_id, addr)
                continue

            try:
                msg = json.loads(plain.decode())
                # The legacy wire header carries only 16 bytes of node_id.
                # A peer may therefore have been resolved by source IP even
                # when parsed['node_id'] is truncated. Always mark the
                # canonical registry identity that was actually resolved.
                self.peers.mark_seen(peer.id, latency_ms=0.0,
                                     pubkey=msg.get('pubkey', ''))
            except:
                self.stats.pkts_dropped += 1

    def status_payload(self) -> dict:
        self.stats.peers_online = self.peers.online_count()
        self.stats.peers_total  = len(self.peers.get_all())
        self.stats.uptime_sec   = time.time() - self._start_time
        self.stats.dns_source   = self.peers.dns_source
        return {
            'node_id': self.node_id,
            'pubkey': self.crypto.pubkey_hex,
            'stats': asdict(self.stats),
            'peers': self.peers.to_json(),
            'crypto_mode': 'AES-256-GCM' if HAVE_CRYPTO else 'HMAC-fallback',
            'dns_source': self.peers.dns_source,
            'note': ('blockchain' if self.peers.dns_source == 'blockchain'
                     else 'using static peers.json (blockchain DNS not yet populated)'),
            'app_protocols': ['HB-TEP-APP/1'] if self.app_ready else [],
            'services': list(self._services.services()) if self.app_ready else [],
            'app_ready': self.app_ready,
            'relay': bool(self.app_ready and self._relay_enabled),
            'app_packet_types': {
                'PKT_APP_REQUEST': PKT_APP_REQUEST,
                'PKT_APP_RESPONSE': PKT_APP_RESPONSE,
                'PKT_APP_ERROR': PKT_APP_ERROR,
                'PKT_RELAY_REQUEST': PKT_RELAY_REQUEST,
                'PKT_RELAY_RESPONSE': PKT_RELAY_RESPONSE,
            },
        }

    def _blockchain_dns_sync_loop(self):
        """Sincronizza periodicamente i peer dalla blockchain."""
        while not self._stop.is_set():
            self.peers.sync_from_blockchain()
            self._stop.wait(DNS_SYNC_SEC)

    def start_status_server(self):
        engine = self
        class StatusHandler(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def _send_json(self, status: int, payload: dict) -> None:
                body = json.dumps(payload, separators=(',', ':')).encode('utf-8')
                if len(body) > IPC_MAX_RESPONSE_BYTES:
                    status = 502
                    body = b'{"ok":false,"error":{"code":"response_too_large"}}'
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == '/favicon.ico':
                    self.send_response(204); self.end_headers(); return
                if self.path.startswith('/app/'):
                    self._send_json(405, {'ok': False, 'error': {'code': 'method_not_allowed'}})
                    return
                self._send_json(200, engine.status_payload())

            def do_POST(self):
                if self.path != '/app/storage-summary':
                    self._send_json(404, {'ok': False, 'error': {'code': 'not_found'}})
                    return
                content_type = (self.headers.get('Content-Type') or '').split(';', 1)[0].strip().lower()
                if content_type != 'application/json':
                    self._send_json(415, {'ok': False, 'error': {'code': 'unsupported_media_type'}})
                    return
                raw_length = self.headers.get('Content-Length')
                if raw_length is None:
                    self._send_json(411, {'ok': False, 'error': {'code': 'length_required'}})
                    return
                try:
                    length = int(raw_length)
                except ValueError:
                    self._send_json(400, {'ok': False, 'error': {'code': 'bad_content_length'}})
                    return
                if length <= 0 or length > IPC_MAX_REQUEST_BYTES:
                    self._send_json(413, {'ok': False, 'error': {'code': 'request_too_large'}})
                    return
                try:
                    data = json.loads(self.rfile.read(length).decode('utf-8'))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._send_json(400, {'ok': False, 'error': {'code': 'invalid_json'}})
                    return
                if not isinstance(data, dict) or set(data) != {'node_id', 'peer_id'}:
                    self._send_json(400, {'ok': False, 'error': {'code': 'bad_request'}})
                    return
                try:
                    result = engine.storage_summary_rpc(data['node_id'], data['peer_id'])
                    self._send_json(200, {'ok': True, **result})
                except TepClientError as exc:
                    status = 504 if exc.code == 'request_timeout' else 503
                    self._send_json(status, {'ok': False, 'error': {'code': exc.code}})
                except ProtocolError as exc:
                    self._send_json(503, {'ok': False, 'error': {'code': exc.code}})
                except Exception:
                    LOG.exception('Local TEP IPC storage.summary failed')
                    self._send_json(500, {'ok': False, 'error': {'code': 'internal_error'}})

        server = ThreadingHTTPServer(('127.0.0.1', self.status_port), StatusHandler)
        self.status_port = int(server.server_port)
        threading.Thread(target=server.serve_forever, daemon=True, name='tep-status').start()
        return server

    def run(self):
        threading.Thread(target=self._heartbeat_loop, daemon=True, name='tep-hb').start()
        threading.Thread(target=self._recv_loop,      daemon=True, name='tep-rx').start()
        threading.Thread(target=self._blockchain_dns_sync_loop, daemon=True, name='tep-dns').start()

        self.start_status_server()

        LOG.info("TEP status: http://127.0.0.1:%d | dns_source=%s",
                 self.status_port, self.peers.dns_source)

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self._stop.set()
            LOG.info("HB-TEP stopped")


def main():
    ap = argparse.ArgumentParser(description='HB-TEP v2.1 — HashBurst Transport Encrypted Protocol')
    ap.add_argument('--node-id',   default=os.environ.get('HB_TEP_NODE_ID', os.uname().nodename))
    ap.add_argument('--listen',    default=os.environ.get('HB_TEP_LISTEN', '0.0.0.0'))
    ap.add_argument('--peer-id',   default=os.environ.get('HB_TEP_PEER_ID', ''))
    ap.add_argument('--rpc-port',  type=int, default=int(os.environ.get('HB_TEP_RPC_PORT', '8009')),
                    help='Port of local HashBurst node RPC (for blockchain DNS)')
    ap.add_argument('--relay-enabled', action='store_true',
                    default=os.environ.get('HB_TEP_RELAY_ENABLED', '0') == '1')
    default_relay_clients = [x.strip() for x in os.environ.get('HB_TEP_RELAY_CLIENTS', '').split(',') if x.strip()]
    ap.add_argument('--relay-client', action='append', default=default_relay_clients,
                    help='Stable peer_id authorized to request relay; repeatable')
    default_trusted_rendezvous = [x.strip() for x in os.environ.get('HB_TEP_TRUSTED_RENDEZVOUS', '').split(',') if x.strip()]
    ap.add_argument('--trusted-rendezvous', action='append', default=default_trusted_rendezvous,
                    help='Stable rendezvous peer_id trusted to deliver relayed APP requests')
    default_ipc_rendezvous = [x.strip() for x in os.environ.get('HB_TEP_RENDEZVOUS_PEERS', '').split(',') if x.strip()]
    ap.add_argument('--rendezvous-peer', action='append', default=default_ipc_rendezvous,
                    help='Stable rendezvous peer_id used by local storage-summary IPC fallback; repeatable')
    ap.add_argument('--log-level', default=os.environ.get('HB_TEP_LOG_LEVEL', 'INFO'),
                    choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    args = ap.parse_args()

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
    )

    TepEngine(
        node_id=args.node_id,
        listen_host=args.listen,
        rpc_port=args.rpc_port,
        peer_id=args.peer_id,
        relay_enabled=args.relay_enabled,
        relay_clients=args.relay_client,
        trusted_rendezvous=args.trusted_rendezvous,
        rendezvous_peer_ids=args.rendezvous_peer,
    ).run()


if __name__ == '__main__':
    main()
