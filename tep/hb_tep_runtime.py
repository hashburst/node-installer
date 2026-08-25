#!/usr/bin/env python3
"""HashBurst TEP v2.1.6 runtime preparation.

Extends the v2.1 core with NAT-safe identity reconciliation and fail-closed
heartbeat authentication:
- dynamic/NAT coordinates remain mutable after authentication;
- stable peer_id and X25519 pubkey are enriched from /api/nodes when
  /api/tep/peers omits them;
- /api/tep/peers and /api/nodes are reconciled before one atomic table swap;
- registered peers omitted by /api/tep/peers remain available without a
  remove/restore window;
- heartbeat crypto never falls back to a host-local node.key when a
  registered peer lacks usable X25519 identity;
- the local infrastructure node can act as its own rendezvous for the
  loopback-only storage.summary IPC path.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.request

from . import hb_tep as core
from .hb_tep_app import ProtocolError, decode_message
from .hb_tep_relay import RelayError


IDENTITY_REFRESH_SEC = 30.0


class TepEngine(core.TepEngine):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._identity_refresh_at = {}
        self._install_registry_reconciliation()

    def _authoritative_nodes(self) -> list[dict]:
        """Read the stable node registry from the local blockchain RPC."""
        rpc_port = int(getattr(self.peers, "_rpc_port", 8009))
        url = f"http://127.0.0.1:{rpc_port}/api/nodes"
        with urllib.request.urlopen(url, timeout=3) as response:
            data = json.loads(response.read())
        if not isinstance(data, list):
            raise ValueError("/api/nodes did not return a list")
        return [item for item in data if isinstance(item, dict)]

    def _tep_peer_snapshot(self) -> list[dict]:
        """Read the narrow transport registry without mutating the peer table."""
        rpc_port = int(getattr(self.peers, "_rpc_port", 8009))
        url = core.BLOCKCHAIN_PEERS_API.format(rpc_port=rpc_port)
        with urllib.request.urlopen(url, timeout=5) as response:
            data = json.loads(response.read())
        if not isinstance(data, dict):
            raise ValueError("/api/tep/peers did not return an object")
        peers = data.get("peers", [])
        if not isinstance(peers, list):
            raise ValueError("/api/tep/peers peers is not a list")
        return [item for item in peers if isinstance(item, dict)]

    @staticmethod
    def _identity_from_record(item: dict):
        peer_id = str(item.get("peer_id") or "").strip()
        pubkey = str(item.get("tep_pubkey") or "").strip().lower()
        if not peer_id or len(pubkey) != 64:
            return None
        try:
            bytes.fromhex(pubkey)
        except ValueError:
            return None
        return peer_id, pubkey

    @staticmethod
    def _identity_from_tep_record(item: dict):
        peer_id = str(item.get("peer_id") or "").strip()
        pubkey = str(item.get("pubkey") or item.get("tep_pubkey") or "").strip().lower()
        if not peer_id or len(pubkey) != 64:
            return None
        try:
            bytes.fromhex(pubkey)
        except ValueError:
            return None
        return peer_id, pubkey

    @staticmethod
    def _bootstrap_ip_from_record(item: dict) -> str:
        external_ip = str(item.get("external_ip") or "").strip()
        if external_ip:
            return external_ip
        for addr in item.get("multiaddrs") or []:
            parts = str(addr).split("/")
            if len(parts) >= 3 and parts[1] in {"ip4", "ip6"} and parts[2]:
                return parts[2]
        return ""

    def _authoritative_identity(self, node_id: str):
        """Read stable TEP identity from the blockchain /api/nodes registry."""
        for item in self._authoritative_nodes():
            if str(item.get("node_id") or "") == node_id:
                return self._identity_from_record(item)
        return None

    def _peer_from_registry_records(self, node_id: str, tep_item: dict | None,
                                    node_item: dict | None, previous) -> core.Peer | None:
        authoritative_identity = self._identity_from_record(node_item or {})
        tep_identity = self._identity_from_tep_record(tep_item or {})
        identity = authoritative_identity or tep_identity

        observed = previous is not None and float(previous.last_seen or 0.0) > 0.0
        if observed:
            ip = str(previous.ip or "").strip()
            port = int(previous.port)
        else:
            ip = str((tep_item or {}).get("ip") or "").strip()
            if not ip and node_item is not None:
                ip = self._bootstrap_ip_from_record(node_item)
            try:
                port = int((tep_item or {}).get("port") or (node_item or {}).get("tep_port") or core.LISTEN_PORT)
            except (TypeError, ValueError):
                port = core.LISTEN_PORT

        if not ip:
            return None

        peer_id = identity[0] if identity else str(getattr(previous, "peer_id", "") or "").strip()
        pubkey = identity[1] if identity else str(getattr(previous, "pubkey", "") or "").strip()
        return core.Peer(
            id=node_id,
            ip=ip,
            port=port,
            pubkey=pubkey or None,
            peer_id=peer_id or None,
            last_seen=float(previous.last_seen) if previous is not None else 0.0,
            latency_ms=previous.latency_ms if previous is not None else None,
            online=bool(previous.online) if previous is not None else False,
        )

    def _install_registry_reconciliation(self) -> None:
        """Reconcile both blockchain registries before one peer-table swap."""

        def reconciled_sync() -> bool:
            tep_records: list[dict] = []
            node_records: list[dict] = []
            tep_ok = False
            nodes_ok = False
            try:
                tep_records = self._tep_peer_snapshot()
                tep_ok = True
            except Exception as exc:
                core.LOG.debug("BlockchainDNS TEP registry read failed: %s", exc)
            try:
                node_records = self._authoritative_nodes()
                nodes_ok = True
            except Exception as exc:
                core.LOG.debug("BlockchainDNS node registry read failed: %s", exc)

            if not tep_ok and not nodes_ok:
                self.peers._dns_source = "static"
                return False

            tep_by_id = {
                str(item.get("id") or "").strip(): item
                for item in tep_records
                if str(item.get("id") or "").strip()
            }
            nodes_by_id = {
                str(item.get("node_id") or "").strip(): item
                for item in node_records
                if str(item.get("node_id") or "").strip()
            }
            candidate_ids = (set(tep_by_id) | set(nodes_by_id)) - {self.node_id}

            with self.peers._lock:
                previous = dict(self.peers._peers)
                fresh: dict[str, core.Peer] = {}
                for node_id in sorted(candidate_ids):
                    peer = self._peer_from_registry_records(
                        node_id,
                        tep_by_id.get(node_id),
                        nodes_by_id.get(node_id),
                        previous.get(node_id),
                    )
                    if peer is not None:
                        fresh[node_id] = peer

                added = set(fresh) - set(previous)
                removed = set(previous) - set(fresh)
                for node_id in sorted(added):
                    peer = fresh[node_id]
                    core.LOG.info(
                        "BlockchainDNS: new peer discovered: %s (%s:%d)",
                        node_id, peer.ip, peer.port,
                    )
                for node_id in sorted(removed):
                    core.LOG.info("BlockchainDNS: peer removed from reconciled registry: %s", node_id)

                self.peers._peers = fresh
                self.peers._dns_source = "blockchain"

            core.LOG.info(
                "BlockchainDNS: reconciled %d peers (tep=%d nodes=%d)",
                len(fresh), len(tep_records), len(node_records),
            )
            return True

        self.peers.sync_from_blockchain = reconciled_sync

    def _ensure_peer_identity(self, peer) -> bool:
        """Enrich only stable identity fields; never overwrite NAT coordinates."""
        if peer.peer_id and peer.pubkey:
            return True
        now = time.monotonic()
        last = float(self._identity_refresh_at.get(peer.id, 0.0))
        if now - last < IDENTITY_REFRESH_SEC:
            return bool(peer.peer_id and peer.pubkey)
        self._identity_refresh_at[peer.id] = now
        try:
            identity = self._authoritative_identity(peer.id)
        except Exception as exc:
            core.LOG.debug("Blockchain identity enrichment failed for %s: %s", peer.id, exc)
            return False
        if identity is None:
            core.LOG.warning("Heartbeat auth missing stable identity for %s", peer.id)
            return False
        peer.peer_id, peer.pubkey = identity
        core.LOG.info("Blockchain identity enriched for %s", peer.id)
        return True

    def _get_shared_key(self, peer) -> bytes:
        """Fail closed: heartbeat crypto requires registered X25519 identity."""
        if not self._ensure_peer_identity(peer):
            raise ProtocolError("authentication_failed", "registered peer identity is incomplete")
        try:
            return self.crypto.derive_shared_key(peer.pubkey)
        except Exception as exc:
            raise ProtocolError("authentication_failed", "X25519 shared-key derivation failed") from exc

    def _record_authenticated_heartbeat(self, peer, msg: dict, addr) -> None:
        """Refresh mutable coordinates only after cryptographic authentication."""
        if not isinstance(msg, dict) or msg.get("type") != "heartbeat":
            raise ProtocolError("bad_heartbeat", "invalid heartbeat payload")
        if str(msg.get("node") or "") != peer.id:
            raise ProtocolError("identity_mismatch", "heartbeat node identity mismatch")

        advertised_pubkey = str(msg.get("pubkey") or "")
        if not peer.peer_id or not peer.pubkey:
            raise ProtocolError("authentication_failed", "heartbeat peer identity is incomplete")
        if advertised_pubkey != peer.pubkey:
            raise ProtocolError("identity_mismatch", "heartbeat TEP public key mismatch")
        self.peers.update_authenticated_endpoint(peer.id, addr[0], addr[1])
        self._relay_table.observe(
            peer_id=peer.peer_id,
            ip=addr[0],
            port=addr[1],
            pubkey=peer.pubkey,
            authenticated=True,
        )
        self.peers.mark_seen(peer.id, latency_ms=0.0)

    def _heartbeat_loop(self):
        while not self._stop.is_set():
            for peer in self.peers.get_all():
                payload = json.dumps({
                    "type": "heartbeat",
                    "ts": int(time.time()),
                    "node": self.node_id,
                    "pubkey": self.crypto.pubkey_hex,
                }).encode()
                try:
                    key = self._get_shared_key(peer)
                except ProtocolError as exc:
                    self.peers.mark_offline(peer.id)
                    self.stats.pkts_dropped += 1
                    core.LOG.warning("Heartbeat send rejected for %s: %s", peer.id, exc.code)
                    continue
                pkt = self._build_packet(core.PKT_HEARTBEAT, payload, key)
                try:
                    self.sock.sendto(pkt, (peer.ip, peer.port))
                    self.stats.pkts_sent += 1
                except OSError:
                    self.peers.mark_offline(peer.id)
                    self.stats.pkts_dropped += 1

            self.stats.peers_online = self.peers.online_count()
            self.stats.peers_total = len(self.peers.get_all())
            self.stats.uptime_sec = time.time() - self._start_time
            self.stats.dns_source = self.peers.dns_source
            self._stop.wait(core.HEARTBEAT_SEC)

    def relay_transport(self, rendezvous_peer_id: str, target_peer_id: str,
                        raw_request: bytes, timeout_sec: float) -> bytes:
        """Support the infrastructure node acting as its own local rendezvous."""
        if rendezvous_peer_id != self.peer_id:
            return super().relay_transport(
                rendezvous_peer_id, target_peer_id, raw_request, timeout_sec
            )

        if not self.app_ready:
            raise ProtocolError("app_unavailable", "HB-TEP-APP/1 is not ready")
        if not self._relay_enabled:
            raise ProtocolError("relay_unauthorized", "local rendezvous relay is disabled")

        env = decode_message(raw_request)
        if env.msg_type != "req":
            raise ProtocolError("unsupported_type", "local rendezvous accepts req only")
        if env.source.node_id != self.node_id or env.source.peer_id != self.peer_id:
            raise ProtocolError("identity_mismatch", "local relay source identity mismatch")
        if env.destination.peer_id != target_peer_id:
            raise ProtocolError("identity_mismatch", "local relay target identity mismatch")
        if env.service != "storage.summary":
            raise ProtocolError("unsupported_service", "local relay permits storage.summary only")

        target = self.peers.find_by_peer_id(target_peer_id)
        if target is None or not target.pubkey:
            raise ProtocolError("destination_unknown", "relay target is not registered")
        try:
            route = self._relay_table.get(target_peer_id)
            return self._relay_forward_to_target(
                target_peer_id, bytes(raw_request), float(timeout_sec), route
            )
        except RelayError as exc:
            raise ProtocolError(exc.code, exc.message) from exc

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

            wire_node_id = parsed["node_id"]

            if parsed["type"] in core.APP_PACKET_TYPES:
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
                plain = self.crypto.decrypt(parsed["enc_payload"], key, parsed["nonce"])
                if plain is None:
                    self.stats.pkts_dropped += 1
                    self.stats.app_auth_rejected += 1
                    core.LOG.warning("APP auth failed from %s/%s", wire_node_id, addr)
                    continue
                self._handle_authenticated_app_packet(parsed, peer, plain, addr)
                continue

            peer = self.peers.find_by_wire_node_id(wire_node_id)
            if peer is None:
                peer = next((p for p in self.peers.get_all() if p.ip == addr[0]), None)
            if peer is None:
                self.stats.pkts_dropped += 1
                core.LOG.warning("Heartbeat auth unknown peer from %s/%s", wire_node_id, addr)
                continue
            if not self._ensure_peer_identity(peer):
                self.stats.pkts_dropped += 1
                core.LOG.warning("Heartbeat auth missing peer key from %s/%s", wire_node_id, addr)
                continue

            try:
                key = self.crypto.derive_shared_key(peer.pubkey)
            except Exception:
                self.stats.pkts_dropped += 1
                core.LOG.warning("Heartbeat auth key derivation failed from %s/%s", wire_node_id, addr)
                continue
            plain = self.crypto.decrypt(parsed["enc_payload"], key, parsed["nonce"])
            if plain is None:
                self.stats.pkts_dropped += 1
                core.LOG.warning("Heartbeat AES-GCM auth failed from %s/%s", wire_node_id, addr)
                continue

            try:
                msg = json.loads(plain.decode("utf-8"))
                self._record_authenticated_heartbeat(peer, msg, addr)
            except (ValueError, UnicodeDecodeError, ProtocolError) as exc:
                self.stats.pkts_dropped += 1
                core.LOG.warning("Heartbeat payload rejected from %s/%s: %s", wire_node_id, addr, type(exc).__name__)


def main() -> None:
    core.TepEngine = TepEngine
    core.main()


if __name__ == "__main__":
    main()
