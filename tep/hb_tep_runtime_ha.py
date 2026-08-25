#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import hb_tep as core
from . import hb_tep_app as app_protocol
from . import hb_tep_runtime as runtime
from .hb_tep_app import Identity, ProtocolError, encode_message, new_response
from .hb_tep_client import TepClientError, TepRpcClient
from .hb_tep_ha_service import HA_LEASE_SERVICE, HaLeaseHandler
from .hb_tep_relay import FailoverTepTransport
from .hb_tep_services import ServiceError

# The stable v2.1.6 base runtime keeps its original service allowlist unchanged.
# The HA runtime extends the process-local APP allowlist without changing packet
# numbers or the heartbeat wire format.
app_protocol.SUPPORTED_SERVICES = frozenset(set(app_protocol.SUPPORTED_SERVICES) | {HA_LEASE_SERVICE})

LOG = logging.getLogger("hb-tep-ha")
HA_IPC_HOST = "127.0.0.1"
HA_IPC_PORT = 47781
HA_IPC_MAX_REQUEST_BYTES = 16 * 1024
HA_IPC_MAX_RESPONSE_BYTES = 32 * 1024
HA_IPC_TIMEOUT_SEC = 2.5


class TepEngine(runtime.TepEngine):
    """TEP v2.1.6 runtime with the authenticated ha.lease application service."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ha_handler = HaLeaseHandler()
        self._services.register(HA_LEASE_SERVICE, self._ha_handler)
        self._ha_ipc_server = None

    def _handle_app_request(self, env, peer, addr) -> None:
        if env.service != HA_LEASE_SERVICE:
            return super()._handle_app_request(env, peer, addr)
        try:
            self._replay.check_and_add(env.source.peer_id, env.request_id)
            result = self._ha_handler.dispatch_envelope(env)
            response = new_response(
                env.raw,
                source=self.local_identity,
                destination=env.source,
                payload=result,
            )
            self._send_plain_to_peer(peer, core.PKT_APP_RESPONSE, encode_message(response), addr=addr)
            self.stats.app_requests += 1
            self.stats.app_responses += 1
        except ProtocolError as exc:
            if exc.code == "replay_detected":
                self.stats.app_replay_rejected += 1
            self._send_app_error(env, peer, addr, exc.code, exc.message, status=400)
        except ServiceError as exc:
            self._send_app_error(env, peer, addr, exc.code, exc.message, status=503)
        except Exception:
            LOG.exception("ha.lease service dispatch failed")
            self._send_app_error(env, peer, addr, "internal_error", "ha.lease service failed", status=500)

    def ha_lease_rpc(self, node_id: str, peer_id: str, payload: dict) -> dict:
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
        transport = FailoverTepTransport(
            direct=self.app_transport,
            relay=self.relay_transport,
            relay_peer_ids=self._rendezvous_peer_ids,
            direct_timeout_sec=min(1.2, HA_IPC_TIMEOUT_SEC),
            max_relay_attempts=core.IPC_MAX_RELAY_ATTEMPTS,
        )
        client = TepRpcClient(local_identity=self.local_identity, transport=transport)
        result = client.request(
            destination=Identity(node_id=node_id, peer_id=peer_id),
            service=HA_LEASE_SERVICE,
            payload=payload,
            timeout_sec=HA_IPC_TIMEOUT_SEC,
        )
        return {
            "result": result,
            "path": transport.last_path,
            "relay_peer_id": transport.last_relay_peer_id,
        }

    def start_ha_ipc_server(self):
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _send(self, status: int, payload: dict) -> None:
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                if len(body) > HA_IPC_MAX_RESPONSE_BYTES:
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
                if self.path != "/app/ha-lease":
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
                if length <= 0 or length > HA_IPC_MAX_REQUEST_BYTES:
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
                    result = engine.ha_lease_rpc(data["node_id"], data["peer_id"], data["payload"])
                    self._send(200, {"ok": True, **result})
                except TepClientError as exc:
                    status = 504 if exc.code == "request_timeout" else 503
                    self._send(status, {"ok": False, "error": {"code": exc.code}})
                except ProtocolError as exc:
                    self._send(503, {"ok": False, "error": {"code": exc.code}})
                except Exception:
                    LOG.exception("Local TEP HA IPC failed")
                    self._send(500, {"ok": False, "error": {"code": "internal_error"}})

        self._ha_ipc_server = ThreadingHTTPServer((HA_IPC_HOST, HA_IPC_PORT), Handler)
        threading.Thread(
            target=self._ha_ipc_server.serve_forever,
            daemon=True,
            name="tep-ha-ipc",
        ).start()
        return self._ha_ipc_server

    def run(self):
        self.start_ha_ipc_server()
        LOG.info("TEP HA IPC: http://%s:%d/app/ha-lease", HA_IPC_HOST, HA_IPC_PORT)
        return super().run()


def main() -> None:
    core.TepEngine = TepEngine
    core.main()


if __name__ == "__main__":
    main()
