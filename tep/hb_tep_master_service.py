#!/usr/bin/env python3
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .hb_tep_app import ProtocolError
from .hb_tep_services import ServiceError

MASTER_STATUS_SERVICE = "master.status"
DEFAULT_MASTER_STATUS_URL = (
    "http" + "://127.0.0.1:9000/api/status"
)
DEFAULT_MASTER_TIMEOUT_SEC = 1.5
DEFAULT_MASTER_MAX_RESPONSE_BYTES = 65536

_ALLOWED_LOOPBACK_HOSTS = frozenset({
    "127.0.0.1",
    "::1",
})
_ALLOWED_PATH = "/api/status"


@dataclass(frozen=True)
class MasterStatusConfig:
    url: str = DEFAULT_MASTER_STATUS_URL
    timeout_sec: float = DEFAULT_MASTER_TIMEOUT_SEC
    max_response_bytes: int = DEFAULT_MASTER_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.url)

        if (
            parsed.scheme != "http"
            or parsed.hostname not in _ALLOWED_LOOPBACK_HOSTS
        ):
            raise ValueError(
                "master status URL must use loopback HTTP"
            )

        if (
            parsed.path != _ALLOWED_PATH
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "master status URL path must be "
                + _ALLOWED_PATH
            )

        if (
            parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError(
                "master status URL must not contain credentials"
            )

        if (
            parsed.port is None
            or not 1 <= parsed.port <= 65535
        ):
            raise ValueError(
                "master status URL must include a valid local port"
            )

        if (
            self.timeout_sec <= 0
            or self.timeout_sec > 5
        ):
            raise ValueError(
                "timeout_sec must be in (0, 5]"
            )

        if (
            self.max_response_bytes <= 0
            or self.max_response_bytes > 65536
        ):
            raise ValueError(
                "max_response_bytes must be in 1..65536"
            )


class MasterStatusHandler:
    """Read-only bridge to the local HashBurst master."""

    _FORBIDDEN_REMOTE_KEYS = frozenset({
        "url",
        "uri",
        "host",
        "hostname",
        "port",
        "path",
        "method",
        "headers",
        "header",
        "scheme",
        "query",
        "target",
        "endpoint",
        "command",
    })

    def __init__(
        self,
        config: MasterStatusConfig | None = None,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config or MasterStatusConfig()
        self._opener = opener or urllib.request.urlopen

    def __call__(
        self,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ProtocolError(
                "bad_request",
                "master.status payload must be an object",
            )

        if payload:
            lowered = {
                str(key).strip().lower()
                for key in payload
            }

            if lowered & self._FORBIDDEN_REMOTE_KEYS:
                raise ProtocolError(
                    "bad_request",
                    "master.status does not accept "
                    "caller-controlled routing or HTTP parameters",
                )

            raise ProtocolError(
                "bad_request",
                "master.status payload must be empty",
            )

        request = urllib.request.Request(
            self.config.url,
            method="GET",
            headers={
                "Accept": "application/json",
                "User-Agent": "HashBurst-TEP-APP/1",
            },
        )

        try:
            with self._opener(
                request,
                timeout=self.config.timeout_sec,
            ) as response:
                status = getattr(response, "status", None)

                if (
                    status is not None
                    and not 200 <= int(status) <= 299
                ):
                    raise ServiceError(
                        "local_service_unavailable",
                        f"master status HTTP {status}",
                    )

                raw = response.read(
                    self.config.max_response_bytes + 1
                )

        except ServiceError:
            raise

        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
        ) as exc:
            raise ServiceError(
                "local_service_unavailable",
                "HashBurst master status endpoint unavailable",
            ) from exc

        except Exception as exc:
            raise ServiceError(
                "local_service_unavailable",
                "HashBurst master status request failed",
            ) from exc

        if len(raw) > self.config.max_response_bytes:
            raise ServiceError(
                "response_too_large",
                "master status response exceeds size limit",
            )

        try:
            result = json.loads(
                raw.decode("utf-8", "strict")
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise ServiceError(
                "local_service_unavailable",
                "master status response is invalid JSON",
            ) from exc

        if not isinstance(result, dict):
            raise ServiceError(
                "local_service_unavailable",
                "master status response must be an object",
            )

        if result.get("master_live") is not True:
            raise ServiceError(
                "master_not_live",
                "HashBurst master is not live",
            )

        return result
