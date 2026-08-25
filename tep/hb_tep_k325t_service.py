#!/usr/bin/env python3
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

from .hb_tep_app import ProtocolError
from .hb_tep_services import ServiceError

K325T_EXCHANGE_SERVICE = "k325t.exchange"
DEFAULT_K325T_URL = "http://127.0.0.1:9010/api/v1/bitstream"
DEFAULT_K325T_TIMEOUT_SEC = 5.0
DEFAULT_K325T_MAX_RESPONSE_BYTES = 32768
_ALLOWED_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
_ALLOWED_PATH = "/api/v1/bitstream"


@dataclass(frozen=True)
class K325TExchangeConfig:
    url: str = DEFAULT_K325T_URL
    timeout_sec: float = DEFAULT_K325T_TIMEOUT_SEC
    max_response_bytes: int = DEFAULT_K325T_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.url)
        if parsed.scheme != "http" or parsed.hostname not in _ALLOWED_LOOPBACK_HOSTS:
            raise ValueError("K325T URL must use loopback HTTP")
        if parsed.path != _ALLOWED_PATH or parsed.query or parsed.fragment:
            raise ValueError(f"K325T URL path must be {_ALLOWED_PATH}")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("K325T URL must not contain credentials")
        if parsed.port != 9010:
            raise ValueError("K325T URL must use local port 9010")
        if self.timeout_sec <= 0 or self.timeout_sec > 10:
            raise ValueError("timeout_sec must be in (0, 10]")
        if self.max_response_bytes <= 0 or self.max_response_bytes > 32768:
            raise ValueError("max_response_bytes must be in 1..32768")


class K325TExchangeHandler:
    """Forward authenticated TEP JSON to the fixed loopback K325T bridge."""

    def __init__(self, config: K325TExchangeConfig | None = None, opener=None) -> None:
        self.config = config or K325TExchangeConfig()
        self._opener = opener or urllib.request.urlopen

    @staticmethod
    def _decode_body(raw: bytes, status: int) -> dict[str, Any]:
        try:
            data = json.loads(raw.decode("utf-8", "strict"))
        except Exception:
            data = {"error": "local_k325t_invalid_json"}
        if not isinstance(data, dict):
            data = {"error": "local_k325t_non_object_response"}
        return {"http_status": int(status), "body": data}

    def __call__(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ProtocolError("bad_request", "k325t.exchange payload must be an object")
        body = json.dumps(dict(payload), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(body) > 8192:
            raise ProtocolError("bad_request", "k325t.exchange request exceeds TEP request limit")
        request = urllib.request.Request(
            self.config.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "HashBurst-TEP-K325T/1",
            },
        )
        try:
            with self._opener(request, timeout=self.config.timeout_sec) as response:
                status = int(getattr(response, "status", 200))
                raw = response.read(self.config.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            raw = exc.read(self.config.max_response_bytes + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ServiceError("local_service_unavailable", "local K325T bridge unavailable") from exc
        except Exception as exc:
            raise ServiceError("local_service_unavailable", "local K325T request failed") from exc
        if len(raw) > self.config.max_response_bytes:
            raise ServiceError("response_too_large", "K325T response exceeds TEP response limit")
        if not (100 <= status <= 599):
            raise ServiceError("local_service_unavailable", f"local K325T HTTP {status}")
        return self._decode_body(raw, status)
