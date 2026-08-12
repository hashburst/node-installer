#!/usr/bin/env python3
"""HashBurst TEP Application RPC protocol primitives (HB-TEP-APP/1).

Step 1 scope only:
- UTF-8 JSON encode/decode
- fail-closed envelope validation
- request/response/error builders
- bounded replay cache

No sockets, no relay, no HTTP calls, no production integration.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Mapping

PROTOCOL_VERSION = 1
SUPPORTED_TYPES = frozenset({"req", "res", "err", "relay_req", "relay_res"})
SUPPORTED_SERVICES = frozenset({"storage.summary"})

REQUEST_ID_HEX_LEN = 32
NONCE_HEX_LEN = 32
MAX_REQUEST_BYTES = 8192
MAX_RESPONSE_BYTES = 32768
MAX_GENERIC_BYTES = 65536
MAX_TTL_MS = 5000
DEFAULT_TTL_MS = 3000
DEFAULT_PAST_SKEW_MS = 30_000
DEFAULT_FUTURE_SKEW_MS = 10_000
DEFAULT_REPLAY_WINDOW_SEC = 120
DEFAULT_REPLAY_MAX_ENTRIES = 10_000


class ProtocolError(ValueError):
    """Fail-closed protocol validation error with a stable machine code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class Identity:
    node_id: str
    peer_id: str

    def to_dict(self) -> dict[str, str]:
        return {"node_id": self.node_id, "peer_id": self.peer_id}


@dataclass(frozen=True)
class ValidatedEnvelope:
    raw: dict[str, Any]
    version: int
    msg_type: str
    request_id: str
    source: Identity
    destination: Identity
    service: str | None
    timestamp_ms: int
    nonce: str
    ttl_ms: int | None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _is_hex(value: str, expected_len: int) -> bool:
    if len(value) != expected_len:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return value.lower() == value


def _require_dict(obj: Any, field: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ProtocolError("bad_request", f"{field} must be an object")
    return obj


def _require_nonempty_str(obj: Mapping[str, Any], field: str, *, max_len: int = 256) -> str:
    value = obj.get(field)
    if not isinstance(value, str):
        raise ProtocolError("bad_request", f"{field} must be a string")
    value = value.strip()
    if not value:
        raise ProtocolError("bad_request", f"{field} must not be empty")
    if len(value) > max_len:
        raise ProtocolError("bad_request", f"{field} is too long")
    return value


def _validate_identity(value: Any, field: str) -> Identity:
    obj = _require_dict(value, field)
    node_id = _require_nonempty_str(obj, "node_id", max_len=128)
    peer_id = _require_nonempty_str(obj, "peer_id", max_len=256)
    return Identity(node_id=node_id, peer_id=peer_id)


def _max_size_for_type(msg_type: str) -> int:
    if msg_type in {"req", "relay_req"}:
        return MAX_REQUEST_BYTES
    if msg_type in {"res", "err", "relay_res"}:
        return MAX_RESPONSE_BYTES
    return MAX_GENERIC_BYTES


def encode_message(message: Mapping[str, Any]) -> bytes:
    """Serialize a protocol message deterministically to UTF-8 JSON.

    The message is validated before serialization. This prevents locally generated
    malformed envelopes from reaching the TEP packet layer.
    """
    validate_envelope(dict(message), check_time=False)
    try:
        raw = json.dumps(message, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError("bad_request", f"message is not JSON serializable: {exc}") from exc
    msg_type = str(message.get("type"))
    if len(raw) > _max_size_for_type(msg_type):
        raise ProtocolError("bad_request", "encoded message exceeds size limit")
    return raw


def decode_message(raw: bytes | bytearray | memoryview | str, *, now_ms: int | None = None,
                   check_time: bool = True) -> ValidatedEnvelope:
    """Decode UTF-8 JSON and return a validated envelope."""
    if isinstance(raw, str):
        raw_bytes = raw.encode("utf-8")
    elif isinstance(raw, (bytes, bytearray, memoryview)):
        raw_bytes = bytes(raw)
    else:
        raise ProtocolError("bad_request", "message must be bytes or UTF-8 string")

    if not raw_bytes:
        raise ProtocolError("bad_request", "empty message")
    if len(raw_bytes) > MAX_GENERIC_BYTES:
        raise ProtocolError("bad_request", "message exceeds absolute size limit")
    try:
        text = raw_bytes.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ProtocolError("bad_request", "message is not valid UTF-8") from exc
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError("bad_request", "message is not valid JSON") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("bad_request", "message root must be an object")

    msg_type = obj.get("type")
    if isinstance(msg_type, str) and len(raw_bytes) > _max_size_for_type(msg_type):
        raise ProtocolError("bad_request", "message exceeds type-specific size limit")
    return validate_envelope(obj, now_ms=now_ms, check_time=check_time)


def validate_envelope(message: dict[str, Any], *, now_ms: int | None = None,
                      check_time: bool = True,
                      past_skew_ms: int = DEFAULT_PAST_SKEW_MS,
                      future_skew_ms: int = DEFAULT_FUTURE_SKEW_MS) -> ValidatedEnvelope:
    """Validate the common HB-TEP-APP/1 envelope and type-specific fields."""
    _require_dict(message, "message")

    version = message.get("v")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ProtocolError("bad_request", "v must be integer 1")
    if version != PROTOCOL_VERSION:
        raise ProtocolError("unsupported_version", f"unsupported protocol version {version}")

    msg_type = message.get("type")
    if not isinstance(msg_type, str):
        raise ProtocolError("bad_request", "type must be a string")
    if msg_type not in SUPPORTED_TYPES:
        raise ProtocolError("unsupported_type", f"unsupported type {msg_type}")

    request_id = message.get("request_id")
    if not isinstance(request_id, str) or not _is_hex(request_id, REQUEST_ID_HEX_LEN):
        raise ProtocolError("bad_request", "request_id must be 128-bit lowercase hex")

    source = _validate_identity(message.get("source"), "source")
    destination = _validate_identity(message.get("destination"), "destination")

    nonce = message.get("nonce")
    if not isinstance(nonce, str) or not _is_hex(nonce, NONCE_HEX_LEN):
        raise ProtocolError("bad_request", "nonce must be 128-bit lowercase hex")

    timestamp_ms = message.get("timestamp_ms")
    if not isinstance(timestamp_ms, int) or isinstance(timestamp_ms, bool) or timestamp_ms < 0:
        raise ProtocolError("bad_request", "timestamp_ms must be a non-negative integer")

    if check_time:
        current = _now_ms() if now_ms is None else now_ms
        if timestamp_ms < current - past_skew_ms:
            raise ProtocolError("request_expired", "timestamp is outside past acceptance window")
        if timestamp_ms > current + future_skew_ms:
            raise ProtocolError("request_expired", "timestamp is outside future acceptance window")

    service: str | None = None
    ttl_ms: int | None = None

    if msg_type in {"req", "res", "err"}:
        service = _require_nonempty_str(message, "service", max_len=128)
        if service not in SUPPORTED_SERVICES:
            raise ProtocolError("unsupported_service", f"unsupported service {service}")

    if msg_type in {"req", "relay_req"}:
        ttl_ms = message.get("ttl_ms")
        if not isinstance(ttl_ms, int) or isinstance(ttl_ms, bool):
            raise ProtocolError("bad_request", "ttl_ms must be an integer")
        if ttl_ms <= 0 or ttl_ms > MAX_TTL_MS:
            raise ProtocolError("bad_request", f"ttl_ms must be between 1 and {MAX_TTL_MS}")

    if msg_type in {"req", "res"}:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            raise ProtocolError("bad_request", "payload must be an object")

    if msg_type == "err":
        status = message.get("status")
        if not isinstance(status, int) or isinstance(status, bool) or not (400 <= status <= 599):
            raise ProtocolError("bad_request", "error status must be 400..599")
        error = _require_dict(message.get("error"), "error")
        _require_nonempty_str(error, "code", max_len=128)
        text = error.get("message")
        if text is not None and (not isinstance(text, str) or len(text) > 512):
            raise ProtocolError("bad_request", "error.message must be a string up to 512 chars")

    if msg_type == "res":
        status = message.get("status")
        if not isinstance(status, int) or isinstance(status, bool) or not (200 <= status <= 399):
            raise ProtocolError("bad_request", "response status must be 200..399")

    if msg_type in {"relay_req", "relay_res"}:
        relay_target = _require_dict(message.get("relay_target"), "relay_target")
        _require_nonempty_str(relay_target, "peer_id", max_len=256)
        inner = message.get("payload")
        if not isinstance(inner, dict) or "inner" not in inner:
            raise ProtocolError("bad_request", "relay payload must contain inner")
        if not isinstance(inner["inner"], str) or not inner["inner"]:
            raise ProtocolError("bad_request", "relay inner must be a non-empty string")

    return ValidatedEnvelope(
        raw=message,
        version=version,
        msg_type=msg_type,
        request_id=request_id,
        source=source,
        destination=destination,
        service=service,
        timestamp_ms=timestamp_ms,
        nonce=nonce,
        ttl_ms=ttl_ms,
    )


def new_request(*, source: Identity, destination: Identity, service: str,
                payload: dict[str, Any] | None = None, ttl_ms: int = DEFAULT_TTL_MS,
                timestamp_ms: int | None = None) -> dict[str, Any]:
    message = {
        "v": PROTOCOL_VERSION,
        "type": "req",
        "request_id": secrets.token_hex(16),
        "source": source.to_dict(),
        "destination": destination.to_dict(),
        "service": service,
        "timestamp_ms": _now_ms() if timestamp_ms is None else timestamp_ms,
        "nonce": secrets.token_hex(16),
        "ttl_ms": ttl_ms,
        "payload": {} if payload is None else payload,
    }
    validate_envelope(message, check_time=False)
    return message


def new_response(request: Mapping[str, Any], *, source: Identity, destination: Identity,
                 payload: dict[str, Any], status: int = 200,
                 timestamp_ms: int | None = None) -> dict[str, Any]:
    message = {
        "v": PROTOCOL_VERSION,
        "type": "res",
        "request_id": request.get("request_id"),
        "source": source.to_dict(),
        "destination": destination.to_dict(),
        "service": request.get("service"),
        "timestamp_ms": _now_ms() if timestamp_ms is None else timestamp_ms,
        "nonce": secrets.token_hex(16),
        "status": status,
        "payload": payload,
    }
    validate_envelope(message, check_time=False)
    return message


def new_error(request: Mapping[str, Any], *, source: Identity, destination: Identity,
              code: str, message_text: str = "", status: int = 400,
              timestamp_ms: int | None = None) -> dict[str, Any]:
    message = {
        "v": PROTOCOL_VERSION,
        "type": "err",
        "request_id": request.get("request_id"),
        "source": source.to_dict(),
        "destination": destination.to_dict(),
        "service": request.get("service"),
        "timestamp_ms": _now_ms() if timestamp_ms is None else timestamp_ms,
        "nonce": secrets.token_hex(16),
        "status": status,
        "error": {"code": code, "message": message_text},
    }
    validate_envelope(message, check_time=False)
    return message


class ReplayCache:
    """Thread-safe bounded TTL replay cache.

    Keys are (source_peer_id, request_id). Entries are evicted by age and by
    maximum cardinality. check_and_add() is atomic.
    """

    def __init__(self, *, window_sec: float = DEFAULT_REPLAY_WINDOW_SEC,
                 max_entries: int = DEFAULT_REPLAY_MAX_ENTRIES,
                 clock=time.monotonic):
        if window_sec <= 0:
            raise ValueError("window_sec must be > 0")
        if max_entries <= 0:
            raise ValueError("max_entries must be > 0")
        self.window_sec = float(window_sec)
        self.max_entries = int(max_entries)
        self._clock = clock
        self._entries: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._lock = threading.Lock()

    def _evict_expired_locked(self, now: float) -> None:
        cutoff = now - self.window_sec
        while self._entries:
            key, seen = next(iter(self._entries.items()))
            if seen > cutoff:
                break
            self._entries.popitem(last=False)

    def check_and_add(self, source_peer_id: str, request_id: str) -> None:
        if not isinstance(source_peer_id, str) or not source_peer_id.strip():
            raise ValueError("source_peer_id must be non-empty")
        if not isinstance(request_id, str) or not _is_hex(request_id, REQUEST_ID_HEX_LEN):
            raise ValueError("request_id must be 128-bit lowercase hex")
        key = (source_peer_id, request_id)
        now = float(self._clock())
        with self._lock:
            self._evict_expired_locked(now)
            if key in self._entries:
                raise ProtocolError("replay_detected", "request_id already seen for source peer")
            self._entries[key] = now
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            self._evict_expired_locked(float(self._clock()))
            return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
