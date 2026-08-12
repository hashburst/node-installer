#!/usr/bin/env python3
"""HB-TEP-APP/1 wire integration helpers for staging.

This module is deliberately daemon-agnostic. It provides:
- collision-safe APP/RELAY packet type allocation from an explicit occupied set
- capability/status metadata merge helpers
- a small UDP endpoint that reuses an injected packet codec

It does NOT assign production packet numbers by assumption and does not import the
production hb_tep.py. The final daemon patch must scan the real daemon constants
and pass that occupied set into allocate_packet_types().
"""
from __future__ import annotations

import json
import socket
import threading
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Protocol

from .hb_tep_app import decode_message, encode_message

APP_PACKET_NAMES = (
    "PKT_APP_REQUEST",
    "PKT_APP_RESPONSE",
    "PKT_APP_ERROR",
    "PKT_RELAY_REQUEST",
    "PKT_RELAY_RESPONSE",
)

# Candidate range intentionally kept away from the known heartbeat 0x01, but the
# allocator still refuses every value supplied in occupied. These are NOT frozen
# production assignments until the real hb_tep.py has been scanned.
DEFAULT_CANDIDATES = tuple(range(0x20, 0x40))


class WireIntegrationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PacketTypes:
    app_request: int
    app_response: int
    app_error: int
    relay_request: int
    relay_response: int

    def as_dict(self) -> dict[str, int]:
        return dict(zip(APP_PACKET_NAMES, (
            self.app_request,
            self.app_response,
            self.app_error,
            self.relay_request,
            self.relay_response,
        )))

    def type_for_message(self, msg_type: str) -> int:
        mapping = {
            "req": self.app_request,
            "res": self.app_response,
            "err": self.app_error,
            "relay_req": self.relay_request,
            "relay_res": self.relay_response,
        }
        try:
            return mapping[msg_type]
        except KeyError as exc:
            raise WireIntegrationError("unsupported_type", f"unsupported message type {msg_type}") from exc

    def message_for_type(self, packet_type: int) -> str:
        mapping = {
            self.app_request: "req",
            self.app_response: "res",
            self.app_error: "err",
            self.relay_request: "relay_req",
            self.relay_response: "relay_res",
        }
        try:
            return mapping[int(packet_type)]
        except KeyError as exc:
            raise WireIntegrationError("unknown_packet_type", f"unknown APP packet type {packet_type}") from exc


def allocate_packet_types(occupied: Iterable[int], *, candidates: Iterable[int] = DEFAULT_CANDIDATES) -> PacketTypes:
    used = {int(v) for v in occupied}
    available = []
    for value in candidates:
        value = int(value)
        if not (0 <= value <= 255):
            raise WireIntegrationError("invalid_packet_type", "packet type must fit one byte")
        if value in used or value in available:
            continue
        available.append(value)
        if len(available) == 5:
            break
    if len(available) != 5:
        raise WireIntegrationError("packet_type_exhausted", "five collision-free APP packet types are required")
    return PacketTypes(*available)


def validate_frozen_packet_types(packet_types: PacketTypes, occupied: Iterable[int]) -> None:
    values = list(packet_types.as_dict().values())
    if len(set(values)) != len(values):
        raise WireIntegrationError("packet_type_collision", "APP packet types collide with each other")
    occupied_set = {int(v) for v in occupied}
    collisions = sorted(set(values) & occupied_set)
    if collisions:
        raise WireIntegrationError("packet_type_collision", f"APP packet types collide with daemon values: {collisions}")


def merge_status_capabilities(status: Mapping[str, object], *, relay_enabled: bool,
                              packet_types: PacketTypes | None = None) -> dict[str, object]:
    out = dict(status)
    out["app_protocols"] = ["HB-TEP-APP/1"]
    out["services"] = ["storage.summary"]
    out["relay"] = bool(relay_enabled)
    if packet_types is not None:
        out["app_packet_types"] = packet_types.as_dict()
    return out


class PacketCodec(Protocol):
    """Adapter implemented by the production TEP engine in the final patch."""

    def encode_packet(self, packet_type: int, plaintext: bytes) -> bytes: ...
    def decode_packet(self, datagram: bytes) -> tuple[int, bytes]: ...


class UdpAppEndpoint:
    """Small localhost/staging endpoint using an injected TEP-compatible codec.

    This class exists only to test APP dispatch and encrypted UDP framing before
    modifying the production daemon. It intentionally contains no discovery,
    NAT, relay policy or identity registry logic.
    """

    def __init__(self, *, bind_host: str, bind_port: int, codec: PacketCodec,
                 packet_types: PacketTypes,
                 request_handler: Callable[[dict], dict] | None = None):
        if bind_host not in {"127.0.0.1", "::1"}:
            raise WireIntegrationError("unsafe_bind", "staging UDP endpoint must bind loopback only")
        self.codec = codec
        self.packet_types = packet_types
        self.request_handler = request_handler
        family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
        self.sock = socket.socket(family, socket.SOCK_DGRAM)
        self.sock.bind((bind_host, int(bind_port)))
        self.sock.settimeout(0.2)
        self.address = self.sock.getsockname()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self.sock.close()

    def send_message(self, message: Mapping[str, object], address: tuple) -> bytes:
        raw = encode_message(dict(message))
        msg_type = str(message.get("type"))
        packet_type = self.packet_types.type_for_message(msg_type)
        datagram = self.codec.encode_packet(packet_type, raw)
        self.sock.sendto(datagram, address)
        return datagram

    def recv_message(self) -> tuple[dict, tuple, bytes]:
        datagram, address = self.sock.recvfrom(65535)
        packet_type, plaintext = self.codec.decode_packet(datagram)
        expected_type = self.packet_types.message_for_type(packet_type)
        env = decode_message(plaintext)
        if env.msg_type != expected_type:
            raise WireIntegrationError("packet_envelope_mismatch", "packet type does not match APP envelope type")
        return env.raw, address, datagram

    def start_request_server(self) -> None:
        if self.request_handler is None:
            raise WireIntegrationError("missing_handler", "request handler is required")
        if self._thread is not None:
            raise WireIntegrationError("already_started", "server already started")

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    message, address, _ = self.recv_message()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    continue
                if message.get("type") != "req":
                    continue
                response = self.request_handler(message)
                self.send_message(response, address)

        self._thread = threading.Thread(target=loop, name="tep-app-localhost-test", daemon=True)
        self._thread.start()
