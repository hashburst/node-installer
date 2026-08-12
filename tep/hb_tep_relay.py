#!/usr/bin/env python3
"""HB-TEP-APP/1 relay primitives for staging.

Step 4 scope:
- authenticated observed-endpoint table
- fail-closed relay policy
- one-hop relay forwarding over injected callables
- direct -> relay failover transport for NAT/symmetric-NAT simulations

No sockets are opened and hb_tep.py is not imported or modified here.
"""
from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from .hb_tep_app import (
    Identity,
    ProtocolError,
    decode_message,
    encode_message,
    validate_envelope,
)

DEFAULT_ROUTE_FRESHNESS_SEC = 30.0
DEFAULT_MAX_PENDING = 256
DEFAULT_MAX_PENDING_PER_SOURCE = 32
DEFAULT_MAX_RELAY_ATTEMPTS = 2
MAX_INNER_BYTES = 32768
ALLOWED_RELAY_SERVICES = frozenset({"storage.summary"})


class RelayError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class ObservedEndpoint:
    peer_id: str
    ip: str
    port: int
    last_seen: float
    pubkey: str = ""


class RelayTable:
    """Thread-safe runtime reachability table.

    Identity is not established here. Callers may update a route only after the
    underlying TEP packet/session has been authenticated.
    """

    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._routes: dict[str, ObservedEndpoint] = {}
        self._lock = threading.Lock()

    def observe(self, *, peer_id: str, ip: str, port: int, pubkey: str = "",
                authenticated: bool) -> ObservedEndpoint:
        peer_id = str(peer_id or "").strip()
        ip = str(ip or "").strip()
        if not authenticated:
            raise RelayError("authentication_failed", "unauthenticated endpoint observation rejected")
        if not peer_id or not ip:
            raise RelayError("bad_route", "peer_id and ip are required")
        if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
            raise RelayError("bad_route", "invalid UDP port")
        route = ObservedEndpoint(peer_id, ip, port, float(self._clock()), str(pubkey or ""))
        with self._lock:
            self._routes[peer_id] = route
        return route

    def get(self, peer_id: str, *, max_age_sec: float = DEFAULT_ROUTE_FRESHNESS_SEC) -> ObservedEndpoint:
        if max_age_sec <= 0:
            raise ValueError("max_age_sec must be > 0")
        now = float(self._clock())
        with self._lock:
            route = self._routes.get(peer_id)
        if route is None:
            raise RelayError("destination_unknown", "relay target has no observed endpoint")
        if now - route.last_seen > max_age_sec:
            raise RelayError("peer_offline", "relay target endpoint is stale")
        return route

    def discard(self, peer_id: str) -> None:
        with self._lock:
            self._routes.pop(peer_id, None)


class RelayPolicy:
    """Fail-closed relay authorization and bounded in-flight accounting."""

    def __init__(self, *, trusted_sources: Iterable[str], registered_targets: Iterable[str],
                 allowed_services: Iterable[str] = ALLOWED_RELAY_SERVICES,
                 max_pending: int = DEFAULT_MAX_PENDING,
                 max_pending_per_source: int = DEFAULT_MAX_PENDING_PER_SOURCE):
        self.trusted_sources = frozenset(str(x) for x in trusted_sources)
        self.registered_targets = frozenset(str(x) for x in registered_targets)
        self.allowed_services = frozenset(str(x) for x in allowed_services)
        if max_pending <= 0 or max_pending_per_source <= 0:
            raise ValueError("pending limits must be > 0")
        self.max_pending = int(max_pending)
        self.max_pending_per_source = int(max_pending_per_source)
        self._pending_total = 0
        self._pending_by_source: dict[str, int] = {}
        self._lock = threading.Lock()

    def authorize(self, *, source_peer_id: str, target_peer_id: str, inner_service: str) -> None:
        if source_peer_id not in self.trusted_sources:
            raise RelayError("relay_unauthorized", "source peer is not authorized to use relay")
        if target_peer_id not in self.registered_targets:
            raise RelayError("destination_unknown", "relay target is not registered")
        if inner_service not in self.allowed_services:
            raise RelayError("unsupported_service", "service is not allowed through relay")

    def acquire(self, source_peer_id: str) -> None:
        with self._lock:
            per_source = self._pending_by_source.get(source_peer_id, 0)
            if self._pending_total >= self.max_pending or per_source >= self.max_pending_per_source:
                raise RelayError("relay_overloaded", "relay pending request limit reached")
            self._pending_total += 1
            self._pending_by_source[source_peer_id] = per_source + 1

    def release(self, source_peer_id: str) -> None:
        with self._lock:
            current = self._pending_by_source.get(source_peer_id, 0)
            if current > 1:
                self._pending_by_source[source_peer_id] = current - 1
            elif current == 1:
                self._pending_by_source.pop(source_peer_id, None)
            if self._pending_total > 0:
                self._pending_total -= 1

    @property
    def pending_total(self) -> int:
        with self._lock:
            return self._pending_total


def _b64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64_decode(text: str) -> bytes:
    try:
        raw = base64.b64decode(text.encode("ascii"), altchars=b"-_", validate=True)
    except Exception as exc:
        raise RelayError("bad_request", "relay inner is not valid base64") from exc
    if len(raw) > MAX_INNER_BYTES:
        raise RelayError("bad_request", "relay inner exceeds size limit")
    return raw


def new_relay_request(*, source: Identity, rendezvous: Identity, target_peer_id: str,
                      inner_request: bytes, ttl_ms: int = 2500,
                      timestamp_ms: int | None = None) -> dict[str, Any]:
    if not isinstance(inner_request, (bytes, bytearray, memoryview)):
        raise TypeError("inner_request must be bytes-like")
    inner = bytes(inner_request)
    if len(inner) > MAX_INNER_BYTES:
        raise RelayError("bad_request", "relay inner exceeds size limit")
    import secrets
    import time as _time
    message = {
        "v": 1,
        "type": "relay_req",
        "request_id": secrets.token_hex(16),
        "source": source.to_dict(),
        "destination": rendezvous.to_dict(),
        "relay_target": {"peer_id": str(target_peer_id)},
        "timestamp_ms": int(_time.time() * 1000) if timestamp_ms is None else int(timestamp_ms),
        "nonce": secrets.token_hex(16),
        "ttl_ms": int(ttl_ms),
        "payload": {"inner": _b64_encode(inner)},
    }
    validate_envelope(message, check_time=False)
    return message


def new_relay_response(relay_request: Mapping[str, Any], *, source: Identity,
                       destination: Identity, target_peer_id: str,
                       inner_response: bytes, timestamp_ms: int | None = None) -> dict[str, Any]:
    import secrets
    import time as _time
    inner = bytes(inner_response)
    if len(inner) > MAX_INNER_BYTES:
        raise RelayError("bad_response", "relay response exceeds size limit")
    message = {
        "v": 1,
        "type": "relay_res",
        "request_id": relay_request.get("request_id"),
        "source": source.to_dict(),
        "destination": destination.to_dict(),
        "relay_target": {"peer_id": str(target_peer_id)},
        "timestamp_ms": int(_time.time() * 1000) if timestamp_ms is None else int(timestamp_ms),
        "nonce": secrets.token_hex(16),
        "payload": {"inner": _b64_encode(inner)},
    }
    validate_envelope(message, check_time=False)
    return message


TargetForwardCallable = Callable[[ObservedEndpoint, bytes, float], bytes | bytearray | memoryview | str | Mapping[str, Any]]


class RelayDispatcher:
    """Validate and forward one relay hop through an injected target transport."""

    def __init__(self, *, local_identity: Identity, table: RelayTable,
                 policy: RelayPolicy, forward_target: TargetForwardCallable,
                 route_freshness_sec: float = DEFAULT_ROUTE_FRESHNESS_SEC):
        self.local_identity = local_identity
        self.table = table
        self.policy = policy
        self.forward_target = forward_target
        self.route_freshness_sec = float(route_freshness_sec)

    def handle(self, raw_relay_request: bytes | bytearray | memoryview | str | Mapping[str, Any],
               timeout_sec: float) -> dict[str, Any]:
        try:
            env = (validate_envelope(dict(raw_relay_request)) if isinstance(raw_relay_request, Mapping)
                   else decode_message(raw_relay_request))
        except ProtocolError as exc:
            raise RelayError(exc.code, exc.message) from exc
        if env.msg_type != "relay_req":
            raise RelayError("unsupported_type", "relay dispatcher accepts relay_req only")
        if env.destination.peer_id != self.local_identity.peer_id:
            raise RelayError("relay_wrong_destination", "relay request is addressed to another rendezvous")
        target_peer = str(env.raw["relay_target"]["peer_id"])
        inner_raw = _b64_decode(str(env.raw["payload"]["inner"]))
        try:
            inner_env = decode_message(inner_raw)
        except ProtocolError as exc:
            raise RelayError(exc.code, exc.message) from exc
        if inner_env.msg_type != "req":
            raise RelayError("unsupported_type", "relay inner must be an application request")
        if inner_env.destination.peer_id != target_peer:
            raise RelayError("identity_mismatch", "relay target does not match inner destination")
        # Nested relays are structurally impossible here because inner must be req.
        self.policy.authorize(source_peer_id=env.source.peer_id,
                              target_peer_id=target_peer,
                              inner_service=str(inner_env.service or ""))
        route = self.table.get(target_peer, max_age_sec=self.route_freshness_sec)
        if timeout_sec <= 0:
            raise RelayError("relay_ttl_expired", "relay timeout budget exhausted")
        relay_ttl_sec = float(env.ttl_ms or 0) / 1000.0
        budget = min(float(timeout_sec), relay_ttl_sec)
        if budget <= 0:
            raise RelayError("relay_ttl_expired", "relay TTL exhausted")
        self.policy.acquire(env.source.peer_id)
        try:
            try:
                raw_response = self.forward_target(route, inner_raw, budget)
            except TimeoutError as exc:
                raise RelayError("relay_timeout", "relay target did not respond in time") from exc
            except RelayError:
                raise
            except Exception as exc:
                raise RelayError("relay_unavailable", "relay target transport failed") from exc
            if isinstance(raw_response, Mapping):
                response_bytes = encode_message(dict(raw_response))
            elif isinstance(raw_response, str):
                response_bytes = raw_response.encode("utf-8")
            else:
                response_bytes = bytes(raw_response)
            try:
                response_env = decode_message(response_bytes)
            except ProtocolError as exc:
                raise RelayError("bad_response", "relay target returned invalid application response") from exc
            if response_env.msg_type not in {"res", "err"}:
                raise RelayError("bad_response", "relay target response must be res or err")
            if response_env.request_id != inner_env.request_id:
                raise RelayError("bad_response", "relay target response request_id mismatch")
            if response_env.source.peer_id != target_peer:
                raise RelayError("bad_response", "relay target response source mismatch")
            return new_relay_response(
                env.raw,
                source=self.local_identity,
                destination=env.source,
                target_peer_id=target_peer,
                inner_response=response_bytes,
            )
        finally:
            self.policy.release(env.source.peer_id)


DirectCallable = Callable[[str, bytes, float], bytes | bytearray | memoryview | str | Mapping[str, Any]]
RelayCallable = Callable[[str, str, bytes, float], bytes | bytearray | memoryview | str | Mapping[str, Any]]


class FailoverTepTransport:
    """Injected transport for TepRpcClient: direct first, then bounded relays."""

    def __init__(self, *, direct: DirectCallable,
                 relay: RelayCallable,
                 relay_peer_ids: Iterable[str],
                 direct_timeout_sec: float = 1.2,
                 max_relay_attempts: int = DEFAULT_MAX_RELAY_ATTEMPTS,
                 clock=time.monotonic):
        if not callable(direct) or not callable(relay):
            raise TypeError("direct and relay must be callable")
        if direct_timeout_sec <= 0:
            raise ValueError("direct_timeout_sec must be > 0")
        if max_relay_attempts < 0:
            raise ValueError("max_relay_attempts must be >= 0")
        self.direct = direct
        self.relay = relay
        self.relay_peer_ids = tuple(dict.fromkeys(str(x) for x in relay_peer_ids if str(x)))
        self.direct_timeout_sec = float(direct_timeout_sec)
        self.max_relay_attempts = int(max_relay_attempts)
        self._clock = clock
        self.last_path: str | None = None
        self.last_relay_peer_id: str | None = None

    def __call__(self, target_peer_id: str, encoded_request: bytes, timeout_sec: float):
        if timeout_sec <= 0:
            raise TimeoutError("TEP RPC timeout budget exhausted")
        started = float(self._clock())
        direct_budget = min(self.direct_timeout_sec, float(timeout_sec))
        try:
            response = self.direct(target_peer_id, encoded_request, direct_budget)
            self.last_path = "direct"
            self.last_relay_peer_id = None
            return response
        except (TimeoutError, RelayError, OSError):
            pass
        attempted = 0
        last_error: Exception | None = None
        for relay_peer in self.relay_peer_ids:
            if attempted >= self.max_relay_attempts:
                break
            elapsed = max(0.0, float(self._clock()) - started)
            remaining = float(timeout_sec) - elapsed
            if remaining <= 0:
                break
            attempted += 1
            try:
                response = self.relay(relay_peer, target_peer_id, encoded_request, remaining)
                self.last_path = "relay"
                self.last_relay_peer_id = relay_peer
                return response
            except (TimeoutError, RelayError, OSError) as exc:
                last_error = exc
                continue
        self.last_path = "failed"
        self.last_relay_peer_id = None
        if last_error is not None:
            raise TimeoutError("direct and relay TEP paths failed") from last_error
        raise TimeoutError("direct TEP path failed and no relay path is available")
