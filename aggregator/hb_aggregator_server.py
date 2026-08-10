#!/usr/bin/env python3
"""
Server minimale che espone l'aggregato di rete su un endpoint HTTP.
Gira sul nodo che serve la pagina pubblica (o su qualsiasi nodo).
  GET /api/network/storage  ->  aggregato di rete (nodi, capacita', eccedenza)

Cache di 30s per non martellare i nodi a ogni richiesta.
"""
import json, time, os
from http.server import HTTPServer, BaseHTTPRequestHandler
import hb_aggregator

PORT = int(os.environ.get("HB_AGGREGATOR_PORT", "8094"))
_cache = {"ts": 0, "data": None}
CACHE_SEC = 30

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path.rstrip("/") == "/api/network/storage":
            now = time.time()
            if _cache["data"] is None or now - _cache["ts"] > CACHE_SEC:
                _cache["data"] = hb_aggregator.aggregate()
                _cache["ts"] = now
            body = json.dumps(_cache["data"]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()

if __name__ == "__main__":
    print(f"Aggregatore su :{PORT}  (GET /api/network/storage)")
    HTTPServer(("127.0.0.1", PORT), H).serve_forever()
