#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

LOG = logging.getLogger("hashburst-ha-ingress")
DEFAULT_HA_STATUS = "http://127.0.0.1:47780/v1/status"
DEFAULT_TEP_STATUS = "http://127.0.0.1:47778/"
DEFAULT_K325T_RPC = "http://127.0.0.1:47782/app/k325t-exchange"
LOGICAL_K325T_ADDRESS = "tep://hashburst-production/HASHBURST_PRIMARY/k325t.exchange"
MAX_REQUEST_BYTES = 8192
MAX_RESPONSE_BYTES = 40 * 1024


def json_request(url: str, payload: dict[str, Any] | None = None, timeout: float = 3.0) -> dict[str, Any]:
    if payload is None:
        request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    else:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raw = exc.read(MAX_RESPONSE_BYTES + 1)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            data = {"ok": False, "error": {"code": "upstream_http_error"}}
        if isinstance(data, dict):
            return data
        raise RuntimeError("upstream_bad_response")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("upstream_response_too_large")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("upstream_bad_response")
    return data


def current_holder() -> str:
    data = json_request(DEFAULT_HA_STATUS, timeout=1.5)
    status = data.get("status") if isinstance(data, dict) else None
    if not isinstance(status, dict):
        return ""
    view = status.get("cluster_view")
    holder = str(view.get("holder") or "").strip() if isinstance(view, dict) else ""
    if holder:
        return holder
    if status.get("local_role") == "primary":
        return str(status.get("node_id") or "").strip()
    return ""


def resolve_holder(holder: str) -> tuple[str, str]:
    status = json_request(DEFAULT_TEP_STATUS, timeout=1.5)
    if str(status.get("node_id") or "") == holder:
        peer_id = str(status.get("peer_id") or (status.get("identity") or {}).get("peer_id") or "").strip()
        if peer_id:
            return holder, peer_id
    for peer in status.get("peers") or []:
        if isinstance(peer, dict) and str(peer.get("id") or "") == holder:
            peer_id = str(peer.get("peer_id") or "").strip()
            if peer_id:
                return holder, peer_id
    raise RuntimeError("lease_holder_not_in_tep_registry")


def route_exchange(payload: dict[str, Any]) -> tuple[int, dict[str, Any], str]:
    holder = current_holder()
    if not holder:
        return 503, {"error": "hashburst_primary_unavailable"}, ""
    try:
        node_id, peer_id = resolve_holder(holder)
        response = json_request(
            DEFAULT_K325T_RPC,
            {"node_id": node_id, "peer_id": peer_id, "payload": payload},
            timeout=7.0,
        )
    except Exception as exc:
        LOG.warning("K325T logical route failed holder=%s error=%s", holder, type(exc).__name__)
        return 503, {"error": "hashburst_primary_route_unavailable"}, holder
    if response.get("ok") is not True:
        code = str((response.get("error") or {}).get("code") or "tep_route_failed")
        return 503, {"error": code}, holder
    result = response.get("result")
    if not isinstance(result, dict):
        return 502, {"error": "invalid_k325t_tep_response"}, holder
    try:
        status = int(result.get("http_status", 502))
    except (TypeError, ValueError):
        status = 502
    body = result.get("body")
    if not isinstance(body, dict):
        body = {"error": "invalid_k325t_body"}
        status = 502
    return max(100, min(599, status)), body, holder


class IngressHandler(BaseHTTPRequestHandler):
    server_version = "HashBurstIngress/2.2"

    def log_message(self, fmt, *args):
        LOG.info("%s - %s", self.client_address[0], fmt % args)

    def _send(self, status: int, payload: dict[str, Any], holder: str = "") -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_RESPONSE_BYTES:
            status = 502
            body = b'{"error":"response_too_large"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-HashBurst-Logical-Route", LOGICAL_K325T_ADDRESS)
        if holder:
            self.send_header("X-HashBurst-Primary", holder)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            try:
                holder = current_holder()
            except Exception:
                holder = ""
            self._send(
                200 if holder else 503,
                {
                    "status": "ok" if holder else "no-primary",
                    "logical_address": LOGICAL_K325T_ADDRESS,
                    "holder": holder,
                },
                holder,
            )
            return
        self._send(404, {"error": "not_found"})

    def do_POST(self):
        if self.path != "/api/v1/bitstream":
            self._send(404, {"error": "not_found"})
            return
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._send(415, {"error": "unsupported_media_type"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._send(413, {"error": "request_too_large"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._send(400, {"error": "invalid_json"})
            return
        if not isinstance(payload, dict):
            self._send(400, {"error": "json_object_required"})
            return
        status, body, holder = route_exchange(payload)
        self._send(status, body, holder)


def main() -> None:
    parser = argparse.ArgumentParser(description="HashBurst identity-routed K325T ingress")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=49010)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server = ThreadingHTTPServer((args.bind, args.port), IngressHandler)
    LOG.warning(
        "HashBurst logical ingress listening http://%s:%d logical=%s",
        args.bind, args.port, LOGICAL_K325T_ADDRESS,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
