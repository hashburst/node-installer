#!/usr/bin/env python3
"""HB-TEP-APP/1 service registry and local service handlers.

Step 2 scope:
- explicit service allowlist/registry
- local-only storage.summary handler
- fixed GET to the public HB-Files storage summary endpoint
- no generic proxying, no caller-controlled URL/host/port/path/method/headers

This module is not wired into the production TEP daemon yet.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .hb_tep_app import ProtocolError, ValidatedEnvelope

STORAGE_SUMMARY_SERVICE = "storage.summary"
DEFAULT_STORAGE_SUMMARY_URL = "http://127.0.0.1:8091/api/public/storage-summary"
DEFAULT_LOCAL_TIMEOUT_SEC = 1.0
DEFAULT_MAX_SUMMARY_BYTES = 32768
_ALLOWED_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
_ALLOWED_SUMMARY_PATH = "/api/public/storage-summary"


class ServiceError(RuntimeError):
    """Local service execution error with a stable HB-TEP error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


ServiceHandler = Callable[[Mapping[str, Any]], dict[str, Any]]


class ServiceRegistry:
    """Explicit, fail-closed application service registry.

    Service names must be registered by local code. Remote input can only select
    an already-registered service; it cannot provide import names, URLs, paths,
    methods, commands, or arbitrary call targets.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, ServiceHandler] = {}

    def register(self, service: str, handler: ServiceHandler) -> None:
        if not isinstance(service, str) or not service.strip():
            raise ValueError("service must be non-empty")
        service = service.strip()
        if service in self._handlers:
            raise ValueError(f"service already registered: {service}")
        if not callable(handler):
            raise TypeError("handler must be callable")
        self._handlers[service] = handler

    def supports(self, service: str) -> bool:
        return service in self._handlers

    def services(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    def dispatch(self, envelope: ValidatedEnvelope) -> dict[str, Any]:
        if envelope.msg_type != "req":
            raise ProtocolError("unsupported_type", "service dispatch accepts request messages only")
        service = envelope.service
        if not service or service not in self._handlers:
            raise ProtocolError("unsupported_service", f"unsupported service {service or '<missing>'}")
        payload = envelope.raw.get("payload")
        if not isinstance(payload, dict):
            raise ProtocolError("bad_request", "payload must be an object")
        return self._handlers[service](payload)


@dataclass(frozen=True)
class StorageSummaryConfig:
    """Server-controlled local storage.summary endpoint configuration."""

    url: str = DEFAULT_STORAGE_SUMMARY_URL
    timeout_sec: float = DEFAULT_LOCAL_TIMEOUT_SEC
    max_response_bytes: int = DEFAULT_MAX_SUMMARY_BYTES

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.url)
        if parsed.scheme != "http":
            raise ValueError("storage summary URL must use http on loopback")
        if parsed.hostname not in _ALLOWED_LOOPBACK_HOSTS:
            raise ValueError("storage summary URL must use a loopback host")
        if parsed.path != _ALLOWED_SUMMARY_PATH or parsed.query or parsed.fragment:
            raise ValueError(f"storage summary URL path must be {_ALLOWED_SUMMARY_PATH}")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("storage summary URL must not contain credentials")
        if parsed.port is None or not (1 <= parsed.port <= 65535):
            raise ValueError("storage summary URL must include a valid local port")
        if self.timeout_sec <= 0 or self.timeout_sec > 5:
            raise ValueError("timeout_sec must be in (0, 5]")
        if self.max_response_bytes <= 0 or self.max_response_bytes > 32768:
            raise ValueError("max_response_bytes must be in 1..32768")


class StorageSummaryHandler:
    """Fetch the fixed public HB-Files storage summary from loopback only."""

    _FORBIDDEN_REMOTE_KEYS = frozenset({
        "url", "uri", "host", "hostname", "port", "path", "method", "headers",
        "header", "scheme", "query", "target", "endpoint", "admin", "command",
    })

    def __init__(self, config: StorageSummaryConfig | None = None,
                 opener: Callable[..., Any] | None = None) -> None:
        self.config = config or StorageSummaryConfig()
        self._opener = opener or urllib.request.urlopen

    def __call__(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ProtocolError("bad_request", "storage.summary payload must be an object")
        if payload:
            lowered = {str(k).strip().lower() for k in payload.keys()}
            if lowered & self._FORBIDDEN_REMOTE_KEYS:
                raise ProtocolError(
                    "bad_request",
                    "storage.summary does not accept caller-controlled routing or HTTP parameters",
                )
            raise ProtocolError("bad_request", "storage.summary payload must be empty")

        req = urllib.request.Request(
            self.config.url,
            method="GET",
            headers={
                "Accept": "application/json",
                "User-Agent": "HashBurst-TEP-APP/1",
            },
        )
        try:
            with self._opener(req, timeout=self.config.timeout_sec) as response:
                status = getattr(response, "status", None)
                if status is not None and not (200 <= int(status) <= 299):
                    raise ServiceError("local_service_unavailable", f"storage summary HTTP {status}")
                raw = response.read(self.config.max_response_bytes + 1)
        except ServiceError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ServiceError("local_service_unavailable", "storage summary endpoint unavailable") from exc
        except Exception as exc:
            # Do not expose local exception detail to remote callers.
            raise ServiceError("local_service_unavailable", "storage summary request failed") from exc

        if len(raw) > self.config.max_response_bytes:
            raise ServiceError("response_too_large", "storage summary response exceeds size limit")
        try:
            summary = json.loads(raw.decode("utf-8", "strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ServiceError("local_service_unavailable", "storage summary response is invalid JSON") from exc
        if not isinstance(summary, dict):
            raise ServiceError("local_service_unavailable", "storage summary response must be an object")
        return summary


def build_default_registry(*, storage_summary_config: StorageSummaryConfig | None = None,
                           opener: Callable[..., Any] | None = None) -> ServiceRegistry:
    """Build the HB-TEP-APP/1 v1 allowlist.

    Exactly one application service is enabled in Step 2: storage.summary.
    """
    registry = ServiceRegistry()
    registry.register(
        STORAGE_SUMMARY_SERVICE,
        StorageSummaryHandler(storage_summary_config, opener=opener),
    )
    return registry
