#!/usr/bin/env python3
"""Step 6 isolated multi-process HB-TEP staging network test.

Creates three independent TepEngine OS processes (client, rendezvous, edge), a
separate local HTTP storage.summary process, and an AF_PACKET loopback capture.
No production addresses or ports are used.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import multiprocessing as mp
import os
import queue
import socket
import struct
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCHED = ROOT / "patched" / "hb_tep.py"
SUMMARY_PORT = 8091
NODES = {
    "a": {"node_id": "client-a", "peer_id": "peer-a", "host": "127.0.0.11", "port": 48771, "status": 48871},
    "r": {"node_id": "rendezvous-r", "peer_id": "peer-r", "host": "127.0.0.12", "port": 48772, "status": 48872},
    "b": {"node_id": "edge-b", "peer_id": "peer-b", "host": "127.0.0.13", "port": 48773, "status": 48873},
}
TEP_TYPES = {0x01: "heartbeat", 0x20: "app_req", 0x21: "app_res", 0x22: "app_err", 0x23: "relay_req", 0x24: "relay_res"}


def _load_daemon():
    spec = importlib.util.spec_from_file_location("hb_tep_step6", PATCHED)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _configure_state(mod, state_dir: Path):
    mod.STATE_DIR = state_dir
    mod.PEERS_FILE = state_dir / "peers.json"
    mod.KEY_FILE = state_dir / "node.key"
    mod.X25519_KEY = state_dir / "node_x25519.key"
    mod.LOG_FILE = state_dir / "tep.log"


def _worker(label: str, base: str, cmd_q: mp.Queue, result_q: mp.Queue):
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    mod = _load_daemon()
    state = Path(base) / label
    _configure_state(mod, state)
    cfg = NODES[label]
    engine = mod.TepEngine(
        node_id=cfg["node_id"], peer_id=cfg["peer_id"], listen_host=cfg["host"],
        listen_port=cfg["port"], status_port=cfg["status"], rpc_port=65500,
        relay_enabled=(label == "r"),
        relay_clients=["peer-a"] if label == "r" else [],
        trusted_rendezvous=["peer-r"] if label == "b" else [],
    )
    threading.Thread(target=engine._recv_loop, daemon=True).start()
    threading.Thread(target=engine._heartbeat_loop, daemon=True).start()
    status_server = engine.start_status_server()
    result_q.put({"label": label, "event": "ready", "app_ready": engine.app_ready,
                  "listen_port": engine.listen_port, "status_port": engine.status_port})
    try:
        while True:
            cmd = cmd_q.get()
            op = cmd.get("op")
            if op == "stop":
                break
            try:
                from tep.hb_tep_app import Identity
                from tep.hb_tep_client import TepRpcClient
                if op == "direct":
                    target = cmd["target"]
                    tcfg = NODES[target]
                    client = TepRpcClient(local_identity=engine.local_identity, transport=engine.app_transport)
                    payload = client.request(destination=Identity(tcfg["node_id"], tcfg["peer_id"]),
                                             service="storage.summary", payload={}, timeout_sec=2.0)
                    result_q.put({"label": label, "event": op, "ok": True, "payload": payload})
                elif op == "bootstrap_to_r":
                    rcfg = NODES["r"]
                    client = TepRpcClient(local_identity=engine.local_identity, transport=engine.app_transport)
                    payload = client.request(destination=Identity(rcfg["node_id"], rcfg["peer_id"]),
                                             service="storage.summary", payload={}, timeout_sec=2.0)
                    result_q.put({"label": label, "event": op, "ok": True, "payload": payload})
                elif op == "failover":
                    from tep.hb_tep_relay import FailoverTepTransport
                    tcfg = NODES[cmd["target"]]
                    target_peer = engine.peers.find_by_peer_id(tcfg["peer_id"])
                    old_port = target_peer.port
                    target_peer.port = 49991  # deliberately unused: force direct timeout
                    ft = FailoverTepTransport(
                        direct=engine.app_transport,
                        relay=lambda rp, tp, raw, timeout: engine.relay_transport(rp, tp, raw, timeout),
                        relay_peer_ids=["peer-r"], direct_timeout_sec=0.25, max_relay_attempts=1)
                    client = TepRpcClient(local_identity=engine.local_identity, transport=ft)
                    try:
                        payload = client.request(destination=Identity(tcfg["node_id"], tcfg["peer_id"]),
                                                 service="storage.summary", payload={}, timeout_sec=3.0)
                        result_q.put({"label": label, "event": op, "ok": True, "payload": payload,
                                      "path": ft.last_path, "relay": ft.last_relay_peer_id})
                    finally:
                        target_peer.port = old_port
                elif op == "status":
                    result_q.put({"label": label, "event": op, "ok": True, "status": engine.status_payload()})
                else:
                    result_q.put({"label": label, "event": op, "ok": False, "error": "unknown operation"})
            except Exception as exc:
                result_q.put({"label": label, "event": op, "ok": False,
                              "error": f"{type(exc).__name__}: {exc}"})
    finally:
        engine._stop.set()
        try:
            status_server.shutdown()
            status_server.server_close()
        except Exception:
            pass
        try:
            engine.sock.close()
        except Exception:
            pass


class _SummaryHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        if self.path != "/api/public/storage-summary":
            self.send_response(404); self.end_headers(); return
        body = json.dumps({
            "available": True, "node_id": "edge-b", "role": "edge",
            "capacity_total_gb": 200, "used_gb": 10, "timestamp": int(time.time()),
            "capacity_source": "step6-mock",
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)


def _summary_process():
    HTTPServer.allow_reuse_address = True
    srv = HTTPServer(("127.0.0.1", SUMMARY_PORT), _SummaryHandler)
    srv.serve_forever()


def _write_pcap_header(f):
    f.write(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))


def _capture_loop(path: str, ports: set[int], stop: threading.Event, stats: dict):
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
    s.bind(("lo", 0))
    s.settimeout(0.2)
    counts = {}
    frames = 0
    with open(path, "wb") as f:
        _write_pcap_header(f)
        while not stop.is_set():
            try:
                frame = s.recv(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(frame) < 42 or frame[12:14] != b"\x08\x00":
                continue
            ihl = (frame[14] & 0x0F) * 4
            if len(frame) < 14 + ihl + 8 or frame[23] != 17:
                continue
            udp = 14 + ihl
            sport, dport = struct.unpack("!HH", frame[udp:udp+4])
            if sport not in ports and dport not in ports:
                continue
            payload = frame[udp+8:]
            if not payload.startswith(b"HBT\x02"):
                continue
            frames += 1
            if len(payload) >= 6:
                ptype = payload[5]
                counts[ptype] = counts.get(ptype, 0) + 1
            now = time.time(); sec = int(now); usec = int((now-sec)*1_000_000)
            f.write(struct.pack("<IIII", sec, usec, len(frame), len(frame))); f.write(frame); f.flush()
    s.close()
    stats.update({"frames": frames, "packet_types": counts})


def _generate_states(base: Path):
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption
    pubs = {}
    for label in NODES:
        d = base / label; d.mkdir(parents=True, exist_ok=True)
        priv = X25519PrivateKey.generate()
        raw = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        (d/"node_x25519.key").write_bytes(raw)
        (d/"node.key").write_bytes(os.urandom(32))
        pubs[label] = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    for label in NODES:
        peers = []
        for other, cfg in NODES.items():
            if other == label: continue
            peers.append({"id": cfg["node_id"], "ip": cfg["host"], "port": cfg["port"],
                          "pubkey": pubs[other], "peer_id": cfg["peer_id"]})
        (base/label/"peers.json").write_text(json.dumps({"peers": peers}, indent=2))


def _get_result(q: mp.Queue, label: str, event: str, timeout=8.0):
    deadline = time.time() + timeout
    stash = []
    while time.time() < deadline:
        try: item = q.get(timeout=0.25)
        except queue.Empty: continue
        if item.get("label") == label and item.get("event") == event:
            for x in stash: q.put(x)
            return item
        stash.append(item)
    for x in stash: q.put(x)
    raise TimeoutError(f"missing result {label}/{event}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", default=str(ROOT/"evidence"/"step6"))
    args = ap.parse_args()
    out = Path(args.output_dir); out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hb-tep-step6-") as td:
        state = Path(td); _generate_states(state)
        ctx = mp.get_context("spawn")
        result_q = ctx.Queue(); cmds = {k: ctx.Queue() for k in NODES}
        summary = ctx.Process(target=_summary_process, name="tep-summary-mock")
        workers = {k: ctx.Process(target=_worker, args=(k, td, cmds[k], result_q), name=f"tep-{k}") for k in NODES}
        capture_stop = threading.Event(); capture_stats = {}
        pcap = out / "tep-step6-loopback.pcap"
        cap = threading.Thread(target=_capture_loop, args=(str(pcap), {v['port'] for v in NODES.values()}, capture_stop, capture_stats), daemon=True)
        summary.start(); cap.start()
        for p in workers.values(): p.start()
        try:
            ready = [_get_result(result_q, k, "ready") for k in NODES]
            if not all(x.get("app_ready") for x in ready): raise AssertionError(f"APP not ready: {ready}")
            time.sleep(0.4)
            # B -> R authenticated APP establishes R's observed route for B.
            cmds["b"].put({"op": "bootstrap_to_r"}); boot = _get_result(result_q, "b", "bootstrap_to_r")
            if not boot.get("ok"): raise AssertionError(boot)
            # A -> B direct path.
            cmds["a"].put({"op": "direct", "target": "b"}); direct = _get_result(result_q, "a", "direct")
            if not direct.get("ok"): raise AssertionError(direct)
            # Force A direct port bad, then verify bounded failover through R.
            cmds["a"].put({"op": "failover", "target": "b"}); failover = _get_result(result_q, "a", "failover", timeout=10)
            if not failover.get("ok") or failover.get("path") != "relay" or failover.get("relay") != "peer-r":
                raise AssertionError(failover)
            # Real HTTP status endpoints from all distinct processes.
            statuses = {}
            for label, cfg in NODES.items():
                with urllib.request.urlopen(f"http://127.0.0.1:{cfg['status']}/", timeout=2) as resp:
                    statuses[label] = json.loads(resp.read())
                if not statuses[label].get("app_ready"): raise AssertionError(f"status app_ready false: {label}")
            time.sleep(0.5)
        finally:
            for k in NODES: cmds[k].put({"op": "stop"})
            for p in workers.values(): p.join(3)
            summary.terminate(); summary.join(2)
            capture_stop.set(); cap.join(2)
        raw = pcap.read_bytes()
        plaintext_hits = [s.decode() for s in (b"storage.summary", b"capacity_total_gb", b"step6-mock") if s in raw]
        if plaintext_hits: raise AssertionError(f"plaintext leaked in TEP pcap: {plaintext_hits}")
        named_counts = {TEP_TYPES.get(int(k), f"0x{int(k):02x}"): v for k, v in capture_stats.get("packet_types", {}).items()}
        required_wire = ["app_req", "app_res", "relay_req", "relay_res", "heartbeat"]
        missing = [x for x in required_wire if named_counts.get(x, 0) <= 0]
        if missing: raise AssertionError(f"missing packet types in pcap: {missing}; counts={named_counts}")
        report = {
            "ok": True, "process_model": "3 TepEngine OS processes + 1 HTTP mock process",
            "direct": direct, "failover": failover,
            "status": {k: {"node_id": v.get("node_id"), "app_ready": v.get("app_ready"), "relay": v.get("relay"),
                            "crypto_mode": v.get("crypto_mode"), "app_packet_types": v.get("app_packet_types")} for k,v in statuses.items()},
            "capture": {"path": str(pcap), "sha256": hashlib.sha256(raw).hexdigest(),
                        "frames": capture_stats.get("frames", 0), "packet_types": named_counts,
                        "plaintext_hits": plaintext_hits},
        }
        (out/"staging-network-report.json").write_text(json.dumps(report, indent=2, sort_keys=True))
        print(json.dumps(report, indent=2, sort_keys=True))

if __name__ == "__main__":
    main()
