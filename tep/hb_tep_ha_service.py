#!/usr/bin/env python3
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

from .hb_tep_app import ProtocolError, ValidatedEnvelope
from .hb_tep_services import ServiceError

HA_LEASE_SERVICE = "ha.lease"
DEFAULT_HA_AGENT_URL = "http://127.0.0.1:47780/v1/tep"
DEFAULT_HA_TIMEOUT_SEC = 1.2
DEFAULT_HA_MAX_RESPONSE_BYTES = 16384
_ALLOWED_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
_ALLOWED_PATH = "/v1/tep"
_ALLOWED_OP_KEYS = {
    "status": frozenset({"op", "cluster_id"}),
    "vote_request": frozenset({"op", "cluster_id", "candidate", "priority", "term", "lease_ms"}),
    "renew": frozenset({"op", "cluster_id", "holder", "term", "lease_ms"}),
}


@dataclass(frozen=True)
class HaLeaseConfig:
    url: str = DEFAULT_HA_AGENT_URL
    timeout_sec: float = DEFAULT_HA_TIMEOUT_SEC
    max_response_bytes: int = DEFAULT_HA_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.url)
        if parsed.scheme != "http" or parsed.hostname not in _ALLOWED_LOOPBACK_HOSTS:
            raise ValueError("HA agent URL must use loopback HTTP")
        if parsed.path != _ALLOWED_PATH or parsed.query or parsed.fragment:
            raise ValueError(f"HA agent URL path must be {_ALLOWED_PATH}")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("HA agent URL must not contain credentials")
        if parsed.port is None or not (1 <= parsed.port <= 65535):
            raise ValueError("HA agent URL must include a valid local port")
        if self.timeout_sec <= 0 or self.timeout_sec > 5:
            raise ValueError("timeout_sec must be in (0, 5]")
        if self.max_response_bytes <= 0 or self.max_response_bytes > 32768:
            raise ValueError("max_response_bytes must be in 1..32768")


class HaLeaseHandler:
    """Envelope-aware bridge from authenticated HB-TEP-APP/1 to the local HA agent."""

    def __init__(self, config: HaLeaseConfig | None = None, opener=None) -> None:
        self.config = config or HaLeaseConfig()
        self._opener = opener or urllib.request.urlopen

    @staticmethod
    def _validate_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ProtocolError("bad_request", "ha.lease payload must be an object")
        out = dict(payload)
        op = str(out.get("op") or "").strip()
        allowed = _ALLOWED_OP_KEYS.get(op)
        if allowed is None:
            raise ProtocolError("unsupported_op", f"unsupported ha.lease operation {op or '<missing>'}")
        if set(out) != set(allowed):
            raise ProtocolError("bad_request", "ha.lease payload contains missing or unexpected fields")
        cluster_id = str(out.get("cluster_id") or "").strip()
        if not cluster_id or len(cluster_id) > 128:
            raise ProtocolError("bad_request", "invalid cluster_id")
        if op == "vote_request":
            candidate = str(out.get("candidate") or "").strip()
            if not candidate or len(candidate) > 128:
                raise ProtocolError("bad_request", "invalid candidate")
            try:
                priority = int(out.get("priority"))
                term = int(out.get("term"))
                lease_ms = int(out.get("lease_ms"))
            except (TypeError, ValueError) as exc:
                raise ProtocolError("bad_request", "invalid vote_request numeric fields") from exc
            if priority < 0 or term < 1 or not (1000 <= lease_ms <= 120000):
                raise ProtocolError("bad_request", "vote_request fields out of range")
        if op == "renew":
            holder = str(out.get("holder") or "").strip()
            if not holder or len(holder) > 128:
                raise ProtocolError("bad_request", "invalid holder")
            try:
                term = int(out.get("term"))
                lease_ms = int(out.get("lease_ms"))
            except (TypeError, ValueError) as exc:
                raise ProtocolError("bad_request", "invalid renew numeric fields") from exc
            if term < 1 or not (1000 <= lease_ms <= 120000):
                raise ProtocolError("bad_request", "renew fields out of range")
        return out

    def __call__(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        raise ServiceError("identity_context_required", "ha.lease requires authenticated envelope context")

    def dispatch_envelope(self, envelope: ValidatedEnvelope) -> dict[str, Any]:
        if envelope.msg_type != "req" or envelope.service != HA_LEASE_SERVICE:
            raise ProtocolError("bad_request", "invalid ha.lease envelope")
        payload = self._validate_payload(envelope.raw.get("payload") or {})
        forwarded = {
            "source": {
                "node_id": envelope.source.node_id,
                "peer_id": envelope.source.peer_id,
            },
            "payload": payload,
        }
        body = json.dumps(forwarded, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.config.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "HashBurst-TEP-HA/1",
            },
        )
        try:
            with self._opener(request, timeout=self.config.timeout_sec) as response:
                status = int(getattr(response, "status", 200))
                raw = response.read(self.config.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read(self.config.max_response_bytes + 1).decode("utf-8"))
                code = str((data.get("error") or {}).get("code") or "ha_agent_rejected")
            except Exception:
                code = "ha_agent_rejected"
            raise ServiceError(code, "local HA agent rejected request") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ServiceError("ha_agent_unavailable", "local HA agent unavailable") from exc
        except Exception as exc:
            raise ServiceError("ha_agent_unavailable", "local HA agent request failed") from exc
        if status // 100 != 2:
            raise ServiceError("ha_agent_unavailable", f"local HA agent HTTP {status}")
        if len(raw) > self.config.max_response_bytes:
            raise ServiceError("response_too_large", "HA agent response exceeds size limit")
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ServiceError("ha_agent_bad_response", "local HA agent returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise ServiceError("ha_agent_bad_response", "local HA agent response must be an object")
        if not bool(data.get("ok", False)):
            error = data.get("error") or {}
            raise ServiceError(str(error.get("code") or "ha_agent_rejected"), "local HA agent rejected request")
        result = data.get("result")
        if not isinstance(result, dict):
            raise ServiceError("ha_agent_bad_response", "local HA agent result must be an object")
        return result
