#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import hb_tep as core
from . import hb_tep_app as app_protocol
from . import hb_tep_runtime_ha as ha_runtime
from .hb_tep_app import Identity, ProtocolError
from .hb_tep_client import TepClientError, TepRpcClient
from .hb_tep_k325t_service import K325T_EXCHANGE_SERVICE, K325TExchangeHandler
from .hb_tep_relay import FailoverTepTransport

app_protocol.SUPPORTED_SERVICES = frozenset(
    set(app_protocol.SUPPORTED_SERVICES) | {K325T_EXCHANGE_SERVICE}
)

LOG = logging.getLogger("hb-tep-v220")
K325T_IPC_HOST = "127.0.0.1"
K325T_IPC_PORT = 47782
K325T_IPC_MAX_REQUEST_BYTES = 16 * 1024
K325T_IPC_MAX_RESPONSE_BYTES = 40 * 1024
K325T_IPC_TIMEOUT_SEC = 6.0


class TepEngine(ha_runtime.TepEngine):
    """TEP-HA runtime with identity-routed K325T exchange service."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._services.register(K325T_EXCHANGE_SERVICE, K325TExchangeHandler())
        self._k325t_ipc_server = None

    def k325t_exchange_rpc(self, node_id: str, peer_id: str, payload: dict) -> dict:
        node_id = str(node_id or "").strip()
        peer_id = str(peer_id or "").strip()
        if not node_id or len(node_id) > 128:
            raise ProtocolError("bad_request", "invalid node_id")
        if not peer_id or len(peer_id) > 256:
            raise ProtocolError("bad_request", "invalid peer_id")
        if not isinstance(payload, dict):
            raise ProtocolError("bad_request", "payload must be an object")
        if not self.app_ready:
            raise ProtocolError("app_unavailable", "HB-TEP-APP/1 is not ready")
        peer = self.peers.find_by_peer_id(peer_id)
        if peer is None or peer.id != node_id:
            raise ProtocolError("destination_unknown", "K325T destination identity is not registered")

        # Candidate nodes are public infrastructure peers. K325T routing is direct
        # in v2.2 so the existing storage-only relay authorization contract is not
        # silently widened. Relay support can be added only with an explicit
        # service-specific policy and field validation.
        transport = FailoverTepTransport(
            direct=self.app_transport,
            relay=self.relay_transport,
            relay_peer_ids=(),
            direct_timeout_sec=K325T_IPC_TIMEOUT_SEC,
            max_relay_attempts=0,
        )
        client = TepRpcClient(local_identity=self.local_identity, transport=transport)
        result = client.request(
            destination=Identity(node_id=node_id, peer_id=peer_id),
            service=K325T_EXCHANGE_SERVICE,
            payload=payload,
            timeout_sec=K325T_IPC_TIMEOUT_SEC,
        )
        return {"result": result, "path": transport.last_path}

    def start_k325t_ipc_server(self):
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _send(self, status: int, payload: dict) -> None:
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                if len(body) > K325T_IPC_MAX_RESPONSE_BYTES:
                    status = 502
                    body = b'{"ok":false,"error":{"code":"response_too_large"}}'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._send(405, {"ok": False, "error": {"code": "method_not_allowed"}})

            def do_POST(self):
                if self.path != "/app/k325t-exchange":
                    self._send(404, {"ok": False, "error": {"code": "not_found"}})
                    return
                content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if content_type != "application/json":
                    self._send(415, {"ok": False, "error": {"code": "unsupported_media_type"}})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or "0")
                except ValueError:
                    length = 0
                if length <= 0 or length > K325T_IPC_MAX_REQUEST_BYTES:
                    self._send(413, {"ok": False, "error": {"code": "request_too_large"}})
                    return
                try:
                    data = json.loads(self.rfile.read(length).decode("utf-8"))
                except Exception:
                    self._send(400, {"ok": False, "error": {"code": "invalid_json"}})
                    return
                if not isinstance(data, dict) or set(data) != {"node_id", "peer_id", "payload"}:
                    self._send(400, {"ok": False, "error": {"code": "bad_request"}})
                    return
                if not isinstance(data.get("payload"), dict):
                    self._send(400, {"ok": False, "error": {"code": "bad_request"}})
                    return
                try:
                    result = engine.k325t_exchange_rpc(data["node_id"], data["peer_id"], data["payload"])
                    self._send(200, {"ok": True, **result})
                except TepClientError as exc:
                    status = 504 if exc.code == "request_timeout" else 503
                    self._send(status, {"ok": False, "error": {"code": exc.code}})
                except ProtocolError as exc:
                    self._send(503, {"ok": False, "error": {"code": exc.code}})
                except Exception:
                    LOG.exception("Local TEP K325T IPC failed")
                    self._send(500, {"ok": False, "error": {"code": "internal_error"}})

        self._k325t_ipc_server = ThreadingHTTPServer((K325T_IPC_HOST, K325T_IPC_PORT), Handler)
        threading.Thread(
            target=self._k325t_ipc_server.serve_forever,
            daemon=True,
            name="tep-k325t-ipc",
        ).start()
        return self._k325t_ipc_server

    def run(self):
        self.start_k325t_ipc_server()
        LOG.info("TEP K325T IPC: http://%s:%d/app/k325t-exchange", K325T_IPC_HOST, K325T_IPC_PORT)
        return super().run()


def main() -> None:
    core.TepEngine = TepEngine
    core.main()


if __name__ == "__main__":
    main()
