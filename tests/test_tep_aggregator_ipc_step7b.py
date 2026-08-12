from __future__ import annotations

import json
import os
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from aggregator import hb_aggregator, hb_tep_adapter


class IpcHandler(BaseHTTPRequestHandler):
    status = 200
    body = {}
    seen = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n)
        type(self).seen.append({
            "path": self.path,
            "content_type": self.headers.get("Content-Type"),
            "body": json.loads(raw.decode("utf-8")),
        })
        encoded = json.dumps(type(self).body).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class Step7BAggregatorIpcTests(unittest.TestCase):
    def setUp(self):
        self.http = HTTPServer(("127.0.0.1", 0), IpcHandler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.old_port = os.environ.get("HB_TEP_IPC_PORT")
        os.environ["HB_TEP_IPC_PORT"] = str(self.http.server_port)
        IpcHandler.seen.clear()
        IpcHandler.status = 200
        IpcHandler.body = {}

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        if self.old_port is None:
            os.environ.pop("HB_TEP_IPC_PORT", None)
        else:
            os.environ["HB_TEP_IPC_PORT"] = self.old_port

    @staticmethod
    def _node():
        return {
            "name": "node-7",
            "tep_node_id": "node-7",
            "transport": "tep",
            "tep_peer_id": "peer-node7",
            "role": "edge",
            "capacity_class": "best-effort",
        }

    @staticmethod
    def _summary():
        return {
            "available": True,
            "node_id": "node-7",
            "role": "edge",
            "capacity_total_gb": 200,
            "used_gb": 10,
            "timestamp": int(time.time()),
        }

    def test_aggregator_uses_fixed_loopback_ipc_for_tep_node(self):
        IpcHandler.body = {
            "ok": True,
            "summary": self._summary(),
            "path": "direct",
            "relay_peer_id": None,
            "rtt_ms": 7.5,
        }
        result = hb_aggregator._fetch_summary(self._node())
        self.assertTrue(result["online"])
        self.assertEqual(result["transport"], "tep")
        self.assertEqual(result["transport_path"], "direct")
        self.assertEqual(result["rtt_ms"], 7.5)
        self.assertEqual(hb_aggregator._capacity_class(result), "best-effort")
        self.assertEqual(len(IpcHandler.seen), 1)
        seen = IpcHandler.seen[0]
        self.assertEqual(seen["path"], "/app/storage-summary")
        self.assertEqual(seen["content_type"], "application/json")
        self.assertEqual(seen["body"], {"node_id": "node-7", "peer_id": "peer-node7"})

    def test_relay_metadata_is_local_and_edge_remains_best_effort(self):
        remote = self._summary()
        remote["_tep_transport_path"] = "forged"
        remote["_tep_relay_peer_id"] = "forged-peer"
        IpcHandler.body = {
            "ok": True,
            "summary": remote,
            "path": "relay",
            "relay_peer_id": "peer-r",
            "rtt_ms": 22,
        }
        result = hb_aggregator._fetch_summary(self._node())
        self.assertTrue(result["online"])
        self.assertEqual(result["transport_path"], "relay")
        self.assertEqual(result["relay_peer_id"], "peer-r")
        self.assertEqual(hb_aggregator._capacity_class(result), "best-effort")

    def test_ipc_failure_marks_edge_offline_fail_closed(self):
        IpcHandler.status = 503
        IpcHandler.body = {
            "ok": False,
            "error": {"code": "request_timeout", "message": "TEP RPC request timed out"},
        }
        result = hb_aggregator._fetch_summary(self._node())
        self.assertFalse(result["online"])
        self.assertEqual(hb_aggregator._capacity_class(result), "best-effort")
        self.assertIn("request_timeout", result["error"])

    def test_ipc_host_and_path_cannot_be_overridden_by_node_config(self):
        IpcHandler.body = {
            "ok": True,
            "summary": self._summary(),
            "path": "direct",
            "relay_peer_id": None,
        }
        node = self._node()
        node.update({
            "url": "http://203.0.113.10:9999",
            "tep_ipc_url": "http://203.0.113.11/admin",
            "path": "/admin",
            "port": 5011,
        })
        result = hb_aggregator._fetch_summary(node)
        self.assertTrue(result["online"])
        self.assertEqual(IpcHandler.seen[0]["path"], "/app/storage-summary")
        self.assertEqual(IpcHandler.seen[0]["body"], {"node_id": "node-7", "peer_id": "peer-node7"})

    def test_invalid_ipc_port_fails_closed(self):
        os.environ["HB_TEP_IPC_PORT"] = "not-a-port"
        result = hb_aggregator._fetch_summary(self._node())
        self.assertFalse(result["online"])
        self.assertIn("bad_config", result["error"])


if __name__ == "__main__":
    unittest.main()
