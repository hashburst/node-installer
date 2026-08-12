from __future__ import annotations

import io
import json
import unittest
import urllib.request
from unittest import mock

from tep.hb_tep_app import Identity, ProtocolError, decode_message, encode_message, new_request
from tep.hb_tep_services import (
    DEFAULT_STORAGE_SUMMARY_URL,
    ServiceError,
    ServiceRegistry,
    StorageSummaryConfig,
    StorageSummaryHandler,
    build_default_registry,
)

NOW = 1_786_500_000_000
SRC = Identity("blockchainapi.one", "peer-aggregator")
DST = Identity("node-7", "peer-node7")


def envelope(payload=None, service="storage.summary"):
    msg = new_request(
        source=SRC,
        destination=DST,
        service=service,
        payload={} if payload is None else payload,
        timestamp_ms=NOW,
    )
    return decode_message(encode_message(msg), now_ms=NOW)


class FakeResponse:
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self, n=-1):
        return self._payload if n < 0 else self._payload[:n]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class ServiceRegistryTests(unittest.TestCase):
    def test_default_registry_only_exposes_storage_summary(self):
        registry = build_default_registry(opener=lambda *a, **k: FakeResponse(b"{}"))
        self.assertEqual(registry.services(), ("storage.summary",))
        self.assertTrue(registry.supports("storage.summary"))
        self.assertFalse(registry.supports("admin"))

    def test_duplicate_registration_rejected(self):
        registry = ServiceRegistry()
        registry.register("storage.summary", lambda payload: {})
        with self.assertRaises(ValueError):
            registry.register("storage.summary", lambda payload: {})

    def test_non_request_dispatch_rejected(self):
        registry = build_default_registry(opener=lambda *a, **k: FakeResponse(b"{}"))
        req = envelope()
        mutated = dict(req.raw)
        mutated["type"] = "res"
        mutated.pop("ttl_ms")
        mutated["status"] = 200
        env = decode_message(encode_message(mutated), now_ms=NOW)
        with self.assertRaises(ProtocolError) as ctx:
            registry.dispatch(env)
        self.assertEqual(ctx.exception.code, "unsupported_type")


class StorageSummaryConfigTests(unittest.TestCase):
    def test_default_is_fixed_loopback_public_summary(self):
        cfg = StorageSummaryConfig()
        self.assertEqual(cfg.url, DEFAULT_STORAGE_SUMMARY_URL)

    def test_non_loopback_host_rejected(self):
        with self.assertRaises(ValueError):
            StorageSummaryConfig(url="http://8.8.8.8:8091/api/public/storage-summary")

    def test_kubo_5011_wrong_path_rejected(self):
        with self.assertRaises(ValueError):
            StorageSummaryConfig(url="http://127.0.0.1:5011/api/v0/id")

    def test_admin_path_rejected(self):
        with self.assertRaises(ValueError):
            StorageSummaryConfig(url="http://127.0.0.1:8091/api/admin")

    def test_https_rejected(self):
        with self.assertRaises(ValueError):
            StorageSummaryConfig(url="https://127.0.0.1:8091/api/public/storage-summary")

    def test_query_string_rejected(self):
        with self.assertRaises(ValueError):
            StorageSummaryConfig(url="http://127.0.0.1:8091/api/public/storage-summary?x=1")

    def test_credentials_rejected(self):
        with self.assertRaises(ValueError):
            StorageSummaryConfig(url="http://user:pass@127.0.0.1:8091/api/public/storage-summary")


class StorageSummarySecurityTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def opener(req, timeout):
            self.calls.append((req, timeout))
            body = json.dumps({
                "available": True,
                "node_id": "node-7",
                "role": "edge",
                "capacity_total_gb": 100,
                "used_gb": 5,
                "timestamp": 1786500000,
            }).encode()
            return FakeResponse(body)

        self.handler = StorageSummaryHandler(opener=opener)
        self.registry = build_default_registry(opener=opener)

    def assert_routing_payload_rejected(self, payload):
        with self.assertRaises(ProtocolError) as ctx:
            self.handler(payload)
        self.assertEqual(ctx.exception.code, "bad_request")
        self.assertEqual(self.calls, [])

    def test_empty_payload_allowed(self):
        result = self.handler({})
        self.assertEqual(result["node_id"], "node-7")
        self.assertEqual(len(self.calls), 1)
        req, timeout = self.calls[0]
        self.assertEqual(req.full_url, DEFAULT_STORAGE_SUMMARY_URL)
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(timeout, 1.0)

    def test_remote_url_rejected_before_http(self):
        self.assert_routing_payload_rejected({"url": "http://127.0.0.1:5011/api/v0/id"})

    def test_remote_host_rejected_before_http(self):
        self.assert_routing_payload_rejected({"host": "127.0.0.1"})

    def test_remote_port_rejected_before_http(self):
        self.assert_routing_payload_rejected({"port": 5011})

    def test_remote_path_rejected_before_http(self):
        self.assert_routing_payload_rejected({"path": "/api/admin"})

    def test_remote_method_rejected_before_http(self):
        self.assert_routing_payload_rejected({"method": "POST"})

    def test_remote_headers_rejected_before_http(self):
        self.assert_routing_payload_rejected({"headers": {"Authorization": "secret"}})

    def test_unknown_payload_key_rejected(self):
        self.assert_routing_payload_rejected({"foo": "bar"})

    def test_registry_dispatch_calls_fixed_service(self):
        result = self.registry.dispatch(envelope())
        self.assertEqual(result["role"], "edge")
        self.assertEqual(self.calls[0][0].full_url, DEFAULT_STORAGE_SUMMARY_URL)

    def test_admin_service_cannot_be_constructed(self):
        with self.assertRaises(ProtocolError) as ctx:
            new_request(source=SRC, destination=DST, service="admin", payload={}, timestamp_ms=NOW)
        self.assertEqual(ctx.exception.code, "unsupported_service")

    def test_http_proxy_service_cannot_be_constructed(self):
        with self.assertRaises(ProtocolError) as ctx:
            new_request(source=SRC, destination=DST, service="http.proxy", payload={}, timestamp_ms=NOW)
        self.assertEqual(ctx.exception.code, "unsupported_service")

    def test_files_delete_service_cannot_be_constructed(self):
        with self.assertRaises(ProtocolError) as ctx:
            new_request(source=SRC, destination=DST, service="files.delete", payload={}, timestamp_ms=NOW)
        self.assertEqual(ctx.exception.code, "unsupported_service")

    def test_malformed_json_from_local_service_rejected(self):
        handler = StorageSummaryHandler(opener=lambda *a, **k: FakeResponse(b"not-json"))
        with self.assertRaises(ServiceError) as ctx:
            handler({})
        self.assertEqual(ctx.exception.code, "local_service_unavailable")

    def test_non_object_json_from_local_service_rejected(self):
        handler = StorageSummaryHandler(opener=lambda *a, **k: FakeResponse(b"[]"))
        with self.assertRaises(ServiceError) as ctx:
            handler({})
        self.assertEqual(ctx.exception.code, "local_service_unavailable")

    def test_oversized_response_rejected(self):
        payload = b"{" + b'"x":"' + (b"a" * 40000) + b'"}'
        handler = StorageSummaryHandler(opener=lambda *a, **k: FakeResponse(payload))
        with self.assertRaises(ServiceError) as ctx:
            handler({})
        self.assertEqual(ctx.exception.code, "response_too_large")

    def test_local_http_error_detail_not_exposed(self):
        def boom(*args, **kwargs):
            raise OSError("token=super-secret /etc/hashburst/env")
        handler = StorageSummaryHandler(opener=boom)
        with self.assertRaises(ServiceError) as ctx:
            handler({})
        self.assertEqual(ctx.exception.code, "local_service_unavailable")
        self.assertNotIn("super-secret", str(ctx.exception))
        self.assertNotIn("/etc/hashburst/env", str(ctx.exception))


class StaticSafetyTests(unittest.TestCase):
    def test_handler_source_has_no_subprocess_or_shell_execution(self):
        import inspect
        import tep.hb_tep_services as services
        source = inspect.getsource(services)
        forbidden = ["subprocess", "os.system", "shell=True", "eval(", "exec("]
        for token in forbidden:
            self.assertNotIn(token, source)

    def test_remote_request_cannot_change_request_url(self):
        handler = StorageSummaryHandler(opener=lambda req, timeout: FakeResponse(b"{}"))
        with mock.patch.object(urllib.request, "Request", wraps=urllib.request.Request) as request_cls:
            handler({})
            request_cls.assert_called_once()
            args, kwargs = request_cls.call_args
            self.assertEqual(args[0], DEFAULT_STORAGE_SUMMARY_URL)
            self.assertEqual(kwargs["method"], "GET")


if __name__ == "__main__":
    unittest.main()

class RealLoopbackHttpTests(unittest.TestCase):
    def test_real_loopback_get_uses_fixed_public_summary_path(self):
        import http.server
        import threading

        observed = {"method": None, "path": None}
        body = json.dumps({"node_id": "node-7", "role": "edge"}).encode()

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                observed["method"] = "GET"
                observed["path"] = self.path
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                observed["method"] = "POST"
                self.send_response(405)
                self.end_headers()

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            cfg = StorageSummaryConfig(
                url=f"http://127.0.0.1:{port}/api/public/storage-summary",
                timeout_sec=1.0,
            )
            result = StorageSummaryHandler(cfg)({})
            self.assertEqual(result["node_id"], "node-7")
            self.assertEqual(observed["method"], "GET")
            self.assertEqual(observed["path"], "/api/public/storage-summary")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_localhost_hostname_is_not_allowed(self):
        with self.assertRaises(ValueError):
            StorageSummaryConfig(url="http://localhost:8091/api/public/storage-summary")
