#!/usr/bin/env python3
"""HashBurst TEP v2.1.5 runtime.

Adds production NAT behavior to the v2.1 core without weakening its protocol
or authentication rules:
- authenticated heartbeat coordinates refresh registered dynamic/NAT peers;
- the local infrastructure node can act as its own rendezvous for the
  loopback-only storage.summary IPC path.
"""
from __future__ import annotations

import json
import socket

from . import hb_tep as core
from .hb_tep_app import ProtocolError, decode_message
from .hb_tep_relay import RelayError


class TepEngine(core.TepEngine):
    def _record_authenticated_heartbeat(self, peer, msg: dict, addr) -> None:
        """Refresh mutable coordinates only after cryptographic authentication."""
        if not isinstance(msg, dict) or msg.get("type") != "heartbeat":
            raise ProtocolError("bad_heartbeat", "invalid heartbeat payload")
        if str(msg.get("node") or "") != peer.id:
            raise ProtocolError("identity_mismatch", "heartbeat node identity mismatch")

        advertised_pubkey = str(msg.get("pubkey") or "")
        if peer.peer_id and peer.pubkey:
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
            return

        # Legacy heartbeat compatibility: never create an authenticated relay
        # route for peers that do not have stable peer_id + registered pubkey.
        self.peers.mark_seen(peer.id, latency_ms=0.0, pubkey=advertised_pubkey)

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

            # Prefer the stable registry identity encoded in the wire header.
            # This is what allows an authenticated peer to move to a new public
            # IP/UDP mapping. Source-IP lookup remains legacy fallback only.
            peer = self.peers.find_by_wire_node_id(wire_node_id)
            if peer is None:
                peer = next((p for p in self.peers.get_all() if p.ip == addr[0]), None)
            if peer is None:
                self.peers.add_peer(wire_node_id, addr[0], addr[1])
                peer = self.peers.find_by_node_id(wire_node_id)
            if peer is None:
                self.stats.pkts_dropped += 1
                continue

            key = self._get_shared_key(peer)
            plain = self.crypto.decrypt(parsed["enc_payload"], key, parsed["nonce"])
            if plain is None:
                self.stats.pkts_dropped += 1
                core.LOG.warning("Auth failed from %s/%s", wire_node_id, addr)
                continue

            try:
                msg = json.loads(plain.decode("utf-8"))
                self._record_authenticated_heartbeat(peer, msg, addr)
            except (ValueError, UnicodeDecodeError, ProtocolError):
                self.stats.pkts_dropped += 1


def main() -> None:
    # Reuse the canonical argument parser and startup path while selecting the
    # v2.1.5 engine implementation.
    core.TepEngine = TepEngine
    core.main()


if __name__ == "__main__":
    main()
