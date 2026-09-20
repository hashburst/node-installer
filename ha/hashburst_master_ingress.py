#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import (
    BaseHTTPRequestHandler,
    ThreadingHTTPServer,
)
from typing import Any, Callable, Mapping

LOG = logging.getLogger("hashburst-master-ingress")

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8096

HA_STATUS_URL = (
    "http" + "://127.0.0.1:47780/v1/status"
)
TEP_STATUS_URL = (
    "http" + "://127.0.0.1:47778/"
)
TEP_MASTER_STATUS_URL = (
    "http"
    + "://127.0.0.1:47781/app/master-status"
)

MAX_RESPONSE_BYTES = 131072
MIN_LEASE_REMAINING_MS = 1000
PCB_TEP_PORT = 8765


class IngressError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        status: int = 503,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class MasterRoute:
    node_id: str
    peer_id: str
    host: str
    port: int
    term: int
    lease_remaining_ms: int
    voters_reachable: int
    quorum: int


class MasterResolver:
    def __init__(
        self,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self._opener = opener or urllib.request.urlopen

    def _request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: Mapping[str, Any] | None = None,
        timeout: float = 3.0,
    ) -> dict[str, Any]:
        body = None
        headers = {
            "Accept": "application/json",
            "User-Agent": "HashBurst-Master-Ingress/1",
        }

        if payload is not None:
            body = json.dumps(
                dict(payload),
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers=headers,
        )

        try:
            with self._opener(
                request,
                timeout=timeout,
            ) as response:
                status = int(
                    getattr(response, "status", 200)
                )
                raw = response.read(
                    MAX_RESPONSE_BYTES + 1
                )

        except urllib.error.HTTPError as exc:
            raise IngressError(
                "upstream_rejected",
                "local upstream rejected the request",
                503,
            ) from exc

        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
        ) as exc:
            raise IngressError(
                "upstream_unavailable",
                "local upstream is unavailable",
                503,
            ) from exc

        if status // 100 != 2:
            raise IngressError(
                "upstream_unavailable",
                f"local upstream returned HTTP {status}",
                503,
            )

        if len(raw) > MAX_RESPONSE_BYTES:
            raise IngressError(
                "response_too_large",
                "local upstream response exceeds limit",
                502,
            )

        try:
            data = json.loads(
                raw.decode("utf-8", "strict")
            )
        except Exception as exc:
            raise IngressError(
                "invalid_upstream_response",
                "local upstream returned invalid JSON",
                502,
            ) from exc

        if not isinstance(data, dict):
            raise IngressError(
                "invalid_upstream_response",
                "local upstream response is not an object",
                502,
            )

        return data

    def ha_status(self) -> dict[str, Any]:
        data = self._request_json(
            HA_STATUS_URL,
            timeout=2.0,
        )

        status = data.get("status")
        if not isinstance(status, dict):
            raise IngressError(
                "ha_status_invalid",
                "HA status is unavailable",
            )

        return status

    def tep_status(self) -> dict[str, Any]:
        data = self._request_json(
            TEP_STATUS_URL,
            timeout=2.0,
        )

        if data.get("app_ready") is not True:
            raise IngressError(
                "tep_not_ready",
                "TEP application transport is not ready",
            )

        return data

    def resolve(self) -> MasterRoute:
        status = self.ha_status()
        view = status.get("cluster_view")

        if not isinstance(view, dict):
            raise IngressError(
                "ha_view_invalid",
                "HA cluster view is unavailable",
            )

        if status.get("armed") is not True:
            raise IngressError(
                "ha_disarmed",
                "HA is not armed",
            )

        holder = str(
            status.get("holder") or ""
        ).strip()

        if not holder:
            raise IngressError(
                "master_unavailable",
                "HA has no current master",
            )

        try:
            term = int(
                status.get("term") or 0
            )
            holder_term = int(
                view.get("holder_term") or 0
            )
            lease_ms = int(
                status.get("lease_remaining_ms") or 0
            )
            voters = int(
                view.get("voters_reachable") or 0
            )
            quorum = int(
                view.get("quorum") or 0
            )
        except (TypeError, ValueError) as exc:
            raise IngressError(
                "ha_view_invalid",
                "HA cluster view contains invalid values",
            ) from exc

        if term < 1 or holder_term != term:
            raise IngressError(
                "ha_term_invalid",
                "HA holder term is not current",
            )

        if (
            quorum < 1
            or voters < quorum
        ):
            raise IngressError(
                "ha_no_quorum",
                "HA quorum is not available",
            )

        if lease_ms < MIN_LEASE_REMAINING_MS:
            raise IngressError(
                "ha_lease_expiring",
                "HA master lease is expiring",
            )

        tep = self.tep_status()
        peers = tep.get("peers")

        if not isinstance(peers, list):
            raise IngressError(
                "tep_registry_invalid",
                "TEP peer registry is unavailable",
            )

        peer = next(
            (
                item
                for item in peers
                if (
                    isinstance(item, dict)
                    and item.get("id") == holder
                )
            ),
            None,
        )

        if peer is None:
            raise IngressError(
                "master_not_registered",
                "HA master is absent from TEP registry",
            )

        if peer.get("online") is not True:
            raise IngressError(
                "master_offline",
                "HA master is offline in TEP",
            )

        peer_id = str(
            peer.get("peer_id") or ""
        ).strip()
        host = str(
            peer.get("ip") or ""
        ).strip()

        if not peer_id or not host:
            raise IngressError(
                "master_identity_incomplete",
                "HA master TEP identity is incomplete",
            )

        return MasterRoute(
            node_id=holder,
            peer_id=peer_id,
            host=host,
            port=PCB_TEP_PORT,
            term=term,
            lease_remaining_ms=lease_ms,
            voters_reachable=voters,
            quorum=quorum,
        )

    def discovery(self) -> dict[str, Any]:
        route = self.resolve()

        return {
            "available": True,
            "master": {
                "node_id": route.node_id,
                "tep_host": route.host,
                "tep_port": route.port,
                "term": route.term,
                "lease_remaining_ms": (
                    route.lease_remaining_ms
                ),
            },
            "ha": {
                "voters_reachable": (
                    route.voters_reachable
                ),
                "quorum": route.quorum,
            },
        }

    def master_status(self) -> tuple[
        dict[str, Any],
        MasterRoute,
        str,
        str | None,
    ]:
        route = self.resolve()

        response = self._request_json(
            TEP_MASTER_STATUS_URL,
            method="POST",
            payload={
                "node_id": route.node_id,
                "peer_id": route.peer_id,
            },
            timeout=6.0,
        )

        if response.get("ok") is not True:
            raise IngressError(
                "tep_master_request_failed",
                "TEP master request failed",
            )

        result = response.get("result")
        if not isinstance(result, dict):
            raise IngressError(
                "invalid_master_response",
                "master status is not an object",
                502,
            )

        if result.get("master_live") is not True:
            raise IngressError(
                "master_not_live",
                "HashBurst master is not live",
            )

        path = str(
            response.get("path") or ""
        )
        if path not in {
            "direct",
            "relay",
        }:
            raise IngressError(
                "invalid_transport_path",
                "TEP returned an invalid transport path",
                502,
            )

        relay_peer_id = response.get(
            "relay_peer_id"
        )

        return (
            result,
            route,
            path,
            (
                str(relay_peer_id)
                if relay_peer_id
                else None
            ),
        )


def build_handler(
    resolver: MasterResolver,
):
    class Handler(BaseHTTPRequestHandler):
        server_version = (
            "HashBurstMasterIngress/1.0"
        )

        def log_message(
            self,
            fmt,
            *args,
        ):
            LOG.info(
                "%s - %s",
                self.client_address[0],
                fmt % args,
            )

        def _send(
            self,
            status: int,
            payload: Mapping[str, Any],
            headers: Mapping[str, str] | None = None,
        ) -> None:
            raw = json.dumps(
                dict(payload),
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")

            self.send_response(status)
            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8",
            )
            self.send_header(
                "Cache-Control",
                "no-store",
            )
            self.send_header(
                "Content-Length",
                str(len(raw)),
            )

            for name, value in (
                headers or {}
            ).items():
                self.send_header(
                    name,
                    value,
                )

            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            self._send(
                405,
                {
                    "available": False,
                    "error": {
                        "code": "method_not_allowed",
                    },
                },
            )

        def do_GET(self):
            path = self.path.split(
                "?",
                1,
            )[0].rstrip("/")

            try:
                if path == "/health":
                    self._send(
                        200,
                        {
                            "ok": True,
                            "service": (
                                "hashburst-master-ingress"
                            ),
                        },
                    )
                    return

                if path == "/api/hashburst/ha/status":
                    status = resolver.ha_status()
                    self._send(
                        200,
                        {
                            "available": True,
                            "status": status,
                        },
                    )
                    return

                if (
                    path
                    == "/api/hashburst/master/discovery"
                ):
                    self._send(
                        200,
                        resolver.discovery(),
                    )
                    return

                if (
                    path
                    == "/api/hashburst/master/status"
                ):
                    (
                        result,
                        route,
                        transport_path,
                        relay_peer_id,
                    ) = resolver.master_status()

                    self._send(
                        200,
                        result,
                        headers={
                            "X-HashBurst-Master": (
                                route.node_id
                            ),
                            "X-HashBurst-Transport": (
                                transport_path
                            ),
                            "X-HashBurst-Term": str(
                                route.term
                            ),
                            "X-HashBurst-Relay": (
                                relay_peer_id or ""
                            ),
                        },
                    )
                    return

                self._send(
                    404,
                    {
                        "available": False,
                        "error": {
                            "code": "not_found",
                        },
                    },
                )

            except IngressError as exc:
                self._send(
                    exc.status,
                    {
                        "available": False,
                        "error": {
                            "code": exc.code,
                            "message": exc.message,
                        },
                    },
                )

            except Exception:
                LOG.exception(
                    "Unhandled ingress request failure"
                )
                self._send(
                    500,
                    {
                        "available": False,
                        "error": {
                            "code": "internal_error",
                        },
                    },
                )

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bind",
        default=DEFAULT_BIND,
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
    )
    args = parser.parse_args()

    if args.bind not in {
        "127.0.0.1",
        "::1",
    }:
        raise SystemExit(
            "master ingress must bind to loopback"
        )

    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s %(levelname)s "
            "%(name)s: %(message)s"
        ),
    )

    server = ThreadingHTTPServer(
        (
            args.bind,
            args.port,
        ),
        build_handler(MasterResolver()),
    )

    LOG.info(
        "HashBurst master ingress listening on %s:%d",
        args.bind,
        args.port,
    )

    server.serve_forever()


if __name__ == "__main__":
    main()
