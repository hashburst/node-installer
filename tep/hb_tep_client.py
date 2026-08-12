#!/usr/bin/env python3
"""HB-TEP-APP/1 request/response client primitives.

Step 3 scope:
- create application requests
- correlate responses by request_id, peer and service
- enforce bounded pending requests and deadlines
- delegate actual transport to an injected callable

No sockets are opened here. Production daemon integration is intentionally deferred.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .hb_tep_app import (
    Identity,
    ProtocolError,
    ValidatedEnvelope,
    decode_message,
    encode_message,
    new_request,
    validate_envelope,
)

DEFAULT_REQUEST_TIMEOUT_SEC = 3.0
DEFAULT_MAX_PENDING = 256


class TepClientError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class PendingRequest:
    request_id: str
    expected_peer_id: str
    expected_service: str
    created_at: float
    deadline: float


class PendingRequestTable:
    """Thread-safe bounded request correlation table."""

    def __init__(self, *, max_pending: int = DEFAULT_MAX_PENDING, clock=time.monotonic):
        if max_pending <= 0:
            raise ValueError("max_pending must be > 0")
        self.max_pending = int(max_pending)
        self._clock = clock
        self._items: dict[str, PendingRequest] = {}
        self._lock = threading.Lock()

    def add(self, request_id: str, expected_peer_id: str, expected_service: str,
            timeout_sec: float) -> PendingRequest:
        if timeout_sec <= 0 or timeout_sec > 30:
            raise ValueError("timeout_sec must be in (0, 30]")
        now = float(self._clock())
        with self._lock:
            self._expire_locked(now)
            if len(self._items) >= self.max_pending:
                raise TepClientError("client_overloaded", "too many pending TEP RPC requests")
            if request_id in self._items:
                raise TepClientError("duplicate_request_id", "request_id already pending")
            item = PendingRequest(
                request_id=request_id,
                expected_peer_id=expected_peer_id,
                expected_service=expected_service,
                created_at=now,
                deadline=now + float(timeout_sec),
            )
            self._items[request_id] = item
            return item

    def correlate(self, envelope: ValidatedEnvelope) -> PendingRequest:
        now = float(self._clock())
        with self._lock:
            self._expire_locked(now)
            item = self._items.get(envelope.request_id)
            if item is None:
                raise TepClientError("unknown_response", "response does not match a pending request")
            if envelope.source.peer_id != item.expected_peer_id:
                raise TepClientError("response_peer_mismatch", "response source peer does not match request")
            if envelope.service != item.expected_service:
                raise TepClientError("response_service_mismatch", "response service does not match request")
            if now > item.deadline:
                self._items.pop(envelope.request_id, None)
                raise TepClientError("request_timeout", "response arrived after request deadline")
            self._items.pop(envelope.request_id, None)
            return item

    def discard(self, request_id: str) -> None:
        with self._lock:
            self._items.pop(request_id, None)

    def _expire_locked(self, now: float) -> None:
        expired = [rid for rid, item in self._items.items() if now > item.deadline]
        for rid in expired:
            self._items.pop(rid, None)

    def expire(self) -> int:
        now = float(self._clock())
        with self._lock:
            before = len(self._items)
            self._expire_locked(now)
            return before - len(self._items)

    def __len__(self) -> int:
        with self._lock:
            self._expire_locked(float(self._clock()))
            return len(self._items)


TransportCallable = Callable[[str, bytes, float], bytes | bytearray | memoryview | str | Mapping[str, Any]]


class TepRpcClient:
    """Synchronous HB-TEP-APP/1 RPC client over an injected transport.

    transport(peer_id, encoded_request, timeout_sec) must return an encoded or
    mapping response. The production transport will be wired to hb_tep.py later.
    """

    def __init__(self, *, local_identity: Identity, transport: TransportCallable,
                 max_pending: int = DEFAULT_MAX_PENDING,
                 clock=time.monotonic):
        if not callable(transport):
            raise TypeError("transport must be callable")
        self.local_identity = local_identity
        self.transport = transport
        self.pending = PendingRequestTable(max_pending=max_pending, clock=clock)

    def request(self, *, destination: Identity, service: str,
                payload: dict[str, Any] | None = None,
                timeout_sec: float = DEFAULT_REQUEST_TIMEOUT_SEC) -> dict[str, Any]:
        req = new_request(
            source=self.local_identity,
            destination=destination,
            service=service,
            payload={} if payload is None else payload,
            ttl_ms=min(max(1, int(timeout_sec * 1000)), 5000),
        )
        rid = str(req["request_id"])
        self.pending.add(rid, destination.peer_id, service, timeout_sec)
        try:
            encoded = encode_message(req)
            raw_response = self.transport(destination.peer_id, encoded, timeout_sec)
            if isinstance(raw_response, Mapping):
                env = validate_envelope(dict(raw_response))
            else:
                env = decode_message(raw_response)
            if env.msg_type not in {"res", "err"}:
                raise TepClientError("unexpected_response_type", "TEP RPC response must be res or err")
            if env.destination.peer_id != self.local_identity.peer_id:
                raise TepClientError("response_destination_mismatch", "response is addressed to another peer")
            self.pending.correlate(env)
            if env.msg_type == "err":
                error = env.raw.get("error") or {}
                raise TepClientError(str(error.get("code") or "remote_error"),
                                     str(error.get("message") or "remote TEP service error"))
            payload_out = env.raw.get("payload")
            if not isinstance(payload_out, dict):
                raise TepClientError("bad_response", "response payload must be an object")
            return payload_out
        except (ProtocolError, TepClientError):
            raise
        except TimeoutError as exc:
            raise TepClientError("request_timeout", "TEP RPC request timed out") from exc
        except Exception as exc:
            raise TepClientError("transport_error", "TEP RPC transport failed") from exc
        finally:
            self.pending.discard(rid)
