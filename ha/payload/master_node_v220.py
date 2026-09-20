#!/usr/bin/env python3
"""HashBurst DePIN Master Node - reviewed PoC build.

TEP UDP server + Monero-style Stratum upstream + REST status API.
HA-managed candidate build; network coordinates are external configuration.
"""

import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import struct
import time
import uuid
from pathlib import Path
from typing import Optional

LOG_DIR = Path("/var/log/hashburst")
LOG_DIR.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("master")
log.setLevel(logging.INFO)
log.propagate = False
log.handlers.clear()
_handler = RotatingFileHandler(LOG_DIR / "master.log", maxBytes=10_000_000, backupCount=5)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
))
log.addHandler(_handler)

CONFIG_PATH = Path("/etc/hashburst/config.json")
WORKER_TIMEOUT = 300.0

DEFAULT_CONFIG = {
    "network": "stagenet",
    "tep_host": "0.0.0.0",
    "tep_port": 8765,
    "api_host": "127.0.0.1",
    "api_port": 9000,
    "coins": {
        "XMR": {
            "enabled": True,
            "algorithm": "RandomX",
            "pool_host": "stagenet.xmr.pm",
            "pool_port": 3333,
            "wallet": "CHANGE_ME",
            "password": "x",
            "worker_types": ["cpu", "fpga", "gpu", "asic"],
        }
    },
    "segmentation": {"nonce_segment_size": 500000, "max_workers": 64},
}


def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        raise RuntimeError(f"required config missing: {CONFIG_PATH}")
    try:
        with CONFIG_PATH.open() as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid config {CONFIG_PATH}: {exc}") from exc
    if not isinstance(cfg, dict):
        raise RuntimeError(f"invalid config {CONFIG_PATH}: top-level JSON must be an object")
    required = {"network", "tep_host", "tep_port", "api_port", "coins", "segmentation"}
    missing = sorted(required - set(cfg))
    if missing:
        raise RuntimeError(f"invalid config {CONFIG_PATH}: missing keys: {','.join(missing)}")
    if not isinstance(cfg.get("coins"), dict) or not cfg["coins"]:
        raise RuntimeError(f"invalid config {CONFIG_PATH}: coins must be a non-empty object")
    segmentation = cfg.get("segmentation")
    if not isinstance(segmentation, dict) or int(segmentation.get("nonce_segment_size", 0) or 0) <= 0:
        raise RuntimeError(f"invalid config {CONFIG_PATH}: segmentation.nonce_segment_size must be positive")
    log.info("Config loaded from %s", CONFIG_PATH)
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)
    log.info("Config saved to %s", CONFIG_PATH)


TEP_MAGIC = b"HBT1"
TEP_REGISTER = 0x10
TEP_JOB = 0x11
TEP_SHARE = 0x12
TEP_COIN_SWITCH = 0x13
TEP_HEARTBEAT = 0x14
TEP_STATS = 0x15
TEP_ACK = 0x16
TEP_BITSTREAM = 0x17


def tep_pack(msg_type: int, payload: dict) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(body) > 65535:
        raise ValueError("TEP payload too large")
    return TEP_MAGIC + struct.pack(">BH", msg_type, len(body)) + body


def tep_unpack(data: bytes):
    if len(data) < 7 or data[:4] != TEP_MAGIC:
        return None, None
    msg_type = data[4]
    length = struct.unpack(">H", data[5:7])[0]
    if length > len(data) - 7:
        return None, None
    try:
        payload = json.loads(data[7:7 + length].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    return msg_type, payload


class Worker:
    def __init__(self, node_id: str, addr: tuple, caps: dict):
        now = time.time()
        self.node_id = node_id
        self.addr = addr
        self.caps = caps
        self.coin = "XMR"
        self.hashrate = 0.0
        self.shares = 0
        self.last_seen = now
        self.active = True
        self.registered_at = now
        self.last_job_id: Optional[str] = None

    def to_dict(self):
        return {
            "node_id": self.node_id,
            "addr": f"{self.addr[0]}:{self.addr[1]}",
            "caps": self.caps,
            "coin": self.coin,
            "hashrate": self.hashrate,
            "shares": self.shares,
            "last_seen": self.last_seen,
            "uptime": int(time.time() - self.registered_at),
            "active": self.active,
            "last_job_id": self.last_job_id,
        }


class StratumUpstream:
    """Monero-style Stratum JSON-RPC upstream (login/job/submit)."""

    def __init__(self, coin_cfg: dict, event_cb=None):
        self.cfg = coin_cfg
        self.reader = None
        self.writer = None
        self.job_id = None
        self.target = "f3220000"
        self.seed_hash = "0" * 64
        self.blob = "0" * 76
        self.session_id = None
        self.job_callbacks = []
        self._req_id = 1
        self._pending_submits = {}
        self.event_cb = event_cb
        self.connected = False

    def on_job(self, cb):
        self.job_callbacks.append(cb)

    def _event(self, event_type: str, data: dict):
        if self.event_cb:
            self.event_cb(event_type, data)

    async def connect(self):
        while True:
            try:
                log.info("Stratum: connecting to %s:%s", self.cfg["pool_host"], self.cfg["pool_port"])
                self.reader, self.writer = await asyncio.open_connection(
                    self.cfg["pool_host"], self.cfg["pool_port"]
                )
                self.connected = True
                log.info("Stratum: connected")
                self._event("stratum_connected", {
                    "pool": f'{self.cfg["pool_host"]}:{self.cfg["pool_port"]}'
                })
                await self._login()
                await self._recv_loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                log.warning("Stratum: disconnected (%s), retry in 15s", exc)
                self._event("stratum_disconnected", {"error": str(exc)})
                if self.writer:
                    self.writer.close()
                    try:
                        await self.writer.wait_closed()
                    except Exception:
                        pass
                self.reader = self.writer = None
                await asyncio.sleep(15)

    async def _send(self, msg: dict):
        if not self.writer:
            raise ConnectionError("Stratum writer unavailable")
        line = json.dumps(msg, separators=(",", ":")) + "\n"
        log.info("Stratum TX: %s", line.rstrip())
        self._event("stratum_tx", {"message": msg})
        self.writer.write(line.encode("utf-8"))
        await self.writer.drain()

    async def _login(self):
        await self._send({
            "id": 1,
            "method": "login",
            "params": {
                "login": f'{self.cfg["wallet"]}.master',
                "pass": self.cfg.get("password", "x"),
                "agent": "HashBurst-Master/1.1",
            },
        })

    async def _recv_loop(self):
        async for line in self.reader:
            raw = line.decode("utf-8", errors="replace").strip()
            if not raw:
                continue
            log.info("Stratum RX: %s", raw[:500])
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            self._event("stratum_rx", {"message": msg})
            await self._handle(msg)
        raise ConnectionError("Stratum upstream closed connection")

    async def _handle(self, msg: dict):
        msg_id = msg.get("id")

        if msg_id == 1 and msg.get("result"):
            result = msg["result"]
            self.session_id = result.get("id")
            job = result.get("job", {})
            if job:
                await self._dispatch_job(job)
            return

        if msg.get("method") == "job":
            await self._dispatch_job(msg.get("params", {}))
            return

        if msg_id in self._pending_submits:
            meta = self._pending_submits.pop(msg_id)
            accepted = not msg.get("error") and msg.get("result") is not None
            evt = "share_accepted" if accepted else "share_rejected"
            data = {**meta, "request_id": msg_id, "response": msg}
            self._event(evt, data)
            log.info(
                "Pool share %s: worker=%s job=%s nonce=%s response=%s",
                "ACCEPTED" if accepted else "REJECTED",
                meta["worker"], meta["job_id"], meta["nonce"], msg,
            )

    async def _dispatch_job(self, job: dict):
        self.job_id = job.get("job_id", self.job_id)
        self.blob = job.get("blob", self.blob)
        self.target = job.get("target", self.target)
        self.seed_hash = job.get("seed_hash", self.seed_hash)
        height = job.get("height", 0)
        job_data = {
            "job_id": self.job_id,
            "blob": self.blob,
            "seed_hash": self.seed_hash,
            "target": self.target,
            "height": height,
            "coin": "XMR",
            "timestamp": time.time(),
        }
        log.info("New job: %s height=%s target=%s", self.job_id, height, self.target)
        for cb in list(self.job_callbacks):
            await cb(job_data)

    async def submit_share(self, worker_id: str, job_id: str, nonce: str, result: str):
        self._req_id += 1
        req_id = self._req_id
        msg = {
            "id": req_id,
            "method": "submit",
            "params": {
                "id": self.session_id or "1",
                "job_id": job_id,
                "nonce": nonce,
                "result": result,
            },
        }
        self._pending_submits[req_id] = {
            "worker": worker_id,
            "job_id": job_id,
            "nonce": nonce,
            "submitted_at": time.time(),
        }
        log.info("Stratum SUBMIT: worker=%s nonce=%s", worker_id, nonce)
        await self._send(msg)


class TEPServer(asyncio.DatagramProtocol):
    def __init__(self, master: "MasterNode"):
        self.master = master
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        log.info("TEP UDP server ready")

    def datagram_received(self, data: bytes, addr: tuple):
        msg_type, payload = tep_unpack(data)
        if msg_type is None:
            log.warning("Invalid TEP packet from %s", addr)
            return
        asyncio.create_task(self.master.handle_tep(msg_type, payload, addr))

    def send(self, data: bytes, addr: tuple):
        if self.transport:
            self.transport.sendto(data, addr)

    def error_received(self, exc):
        log.warning("TEP error: %s", exc)


class MasterNode:
    def __init__(self):
        self.cfg = load_config()
        self.workers: dict[str, Worker] = {}
        self.stratum: dict[str, StratumUpstream] = {}
        self._started = False
        self._start_lock = asyncio.Lock()
        self._tasks = set()
        self.tep_proto: Optional[TEPServer] = None
        self.current_job = None
        self.nonce_cursor = 0
        self.stats_log = []
        self.started_at = time.time()
        self.pool_accepted = 0
        self.pool_rejected = 0

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _log_event(self, event_type: str, data: dict):
        entry = {"ts": time.time(), "type": event_type, **data}
        self.stats_log.append(entry)
        if len(self.stats_log) > 500:
            self.stats_log = self.stats_log[-500:]

    def _stratum_event(self, event_type: str, data: dict):
        if event_type == "share_accepted":
            self.pool_accepted += 1
        elif event_type == "share_rejected":
            self.pool_rejected += 1
        self._log_event(event_type, data)

    def _touch_worker(self, node_id: str, addr: tuple, payload: Optional[dict] = None,
                      create: bool = False) -> Optional[Worker]:
        worker = self.workers.get(node_id)
        if worker is None and create:
            worker = Worker(node_id, addr, (payload or {}).get("caps", {}))
            self.workers[node_id] = worker
        if worker is None:
            return None
        worker.addr = addr
        worker.last_seen = time.time()
        worker.active = True
        if payload:
            if "hashrate" in payload:
                try:
                    worker.hashrate = max(0.0, float(payload["hashrate"]))
                except (TypeError, ValueError):
                    pass
            if payload.get("coin"):
                worker.coin = payload["coin"]
        return worker

    async def start(self):
        async with self._start_lock:
            if self._started:
                log.warning("MasterNode.start() ignored: already started")
                return
            self._started = True

            log.info("HashBurst Master Node starting - network: %s", self.cfg.get("network"))

            loop = asyncio.get_running_loop()
            _, proto = await loop.create_datagram_endpoint(
                lambda: TEPServer(self),
                local_addr=(self.cfg["tep_host"], self.cfg["tep_port"]),
            )
            self.tep_proto = proto
            log.info("TEP UDP listening on %s:%s", self.cfg["tep_host"], self.cfg["tep_port"])

            self._spawn(self._api_server())
            self._spawn(self._housekeeping_loop())

            for coin, ccfg in self.cfg.get("coins", {}).items():
                if ccfg.get("enabled"):
                    up = StratumUpstream(ccfg, self._stratum_event)
                    up.on_job(self._on_new_job)
                    self.stratum[coin] = up
                    self._spawn(up.connect())

    async def _dispatch_segment(self, worker: Worker, job: Optional[dict] = None):
        job = job or self.current_job
        if not job or not self.tep_proto:
            return
        if time.time() - worker.last_seen > WORKER_TIMEOUT:
            worker.active = False
            return

        segsz = int(self.cfg["segmentation"]["nonce_segment_size"])
        nonce_start = self.nonce_cursor * segsz
        self.nonce_cursor += 1
        dispatch = {
            **job,
            "node_id": worker.node_id,
            "nonce_start": hex(nonce_start),
            "nonce_count": segsz,
        }
        worker.last_job_id = job.get("job_id")
        self.tep_proto.send(tep_pack(TEP_JOB, dispatch), worker.addr)
        log.info("Job dispatched to %s nonce_start=%s", worker.node_id, hex(nonce_start))
        self._log_event("dispatch", {
            "node_id": worker.node_id,
            "job_id": job.get("job_id"),
            "nonce_start": hex(nonce_start),
            "nonce_count": segsz,
        })

    async def _on_new_job(self, job: dict):
        self.current_job = job
        self.nonce_cursor = 0
        log.info("New job: %s coin=XMR", job.get("job_id"))
        self._log_event("job", job)
        now = time.time()
        for worker in list(self.workers.values()):
            if now - worker.last_seen <= WORKER_TIMEOUT:
                worker.active = True
                await self._dispatch_segment(worker, job)

    async def handle_tep(self, msg_type: int, payload: dict, addr: tuple):
        if msg_type == TEP_REGISTER:
            node_id = payload.get("node_id") or str(uuid.uuid4())[:8]
            worker = self._touch_worker(node_id, addr, payload, create=True)
            if payload.get("caps"):
                worker.caps = payload["caps"]
            log.info("Worker registered: %s from %s", node_id, addr)
            self._log_event("register", {"node_id": node_id, "addr": str(addr)})
            self.tep_proto.send(tep_pack(TEP_ACK, {
                "status": "ok", "node_id": node_id, "network": self.cfg.get("network")
            }), addr)
            if self.current_job:
                await self._dispatch_segment(worker)
            return

        node_id = payload.get("node_id", "unknown")

        if msg_type == TEP_SHARE:
            worker = self._touch_worker(node_id, addr, payload, create=True)
            if worker:
                worker.shares += 1
            log.info("Share from %s: %s", node_id, payload)
            self._log_event("share", payload)
            coin = payload.get("coin", "XMR")
            if coin in self.stratum:
                try:
                    await self.stratum[coin].submit_share(
                        node_id,
                        payload.get("job_id", ""),
                        payload.get("nonce", "0"),
                        payload.get("result", "0" * 64),
                    )
                except Exception as exc:
                    log.warning("Share forwarding failed: %s", exc)
                    self._log_event("share_forward_error", {
                        "node_id": node_id, "error": str(exc)
                    })
            self.tep_proto.send(tep_pack(TEP_ACK, {
                "status": "ok", "share": payload.get("nonce")
            }), addr)
            if worker and self.current_job and payload.get("job_id") == self.current_job.get("job_id"):
                await self._dispatch_segment(worker)
            return

        if msg_type == TEP_HEARTBEAT:
            worker = self._touch_worker(node_id, addr, payload, create=True)
            self.tep_proto.send(tep_pack(TEP_ACK, {"status": "ok", "ts": time.time()}), addr)
            if (worker and self.current_job and
                    worker.last_job_id != self.current_job.get("job_id")):
                await self._dispatch_segment(worker)
            return

        if msg_type == TEP_STATS:
            worker = self._touch_worker(node_id, addr, payload, create=True)
            if worker and payload.get("event") == "segment_done":
                if self.current_job and payload.get("job_id") == self.current_job.get("job_id"):
                    await self._dispatch_segment(worker)
            return

    async def _housekeeping_loop(self):
        while True:
            await asyncio.sleep(30)
            now = time.time()
            for node_id, worker in list(self.workers.items()):
                was_active = worker.active
                worker.active = (now - worker.last_seen) <= WORKER_TIMEOUT
                if was_active and not worker.active:
                    worker.hashrate = 0.0
                    log.info("Worker %s marked inactive (timeout)", node_id)
                    self._log_event("inactive", {"node_id": node_id})

    async def _api_server(self):
        from aiohttp import web

        async def handle_status(_req):
            workers = [w.to_dict() for w in self.workers.values()]
            return web.json_response({
                "network": self.cfg.get("network"),
                "master_live": True,
                "uptime": int(time.time() - self.started_at),
                "workers": workers,
                "active_workers": sum(1 for w in self.workers.values() if w.active),
                "total_hashrate": sum(w.hashrate for w in self.workers.values() if w.active),
                "total_shares": sum(w.shares for w in self.workers.values()),
                "pool_accepted": self.pool_accepted,
                "pool_rejected": self.pool_rejected,
                "current_job": self.current_job,
                "stratum": {
                    k: {
                        "connected": up.connected,
                        "pool": f'{up.cfg["pool_host"]}:{up.cfg["pool_port"]}',
                        "session_id": up.session_id,
                        "job_id": up.job_id,
                    }
                    for k, up in self.stratum.items()
                },
                "coins": {
                    k: {"enabled": v.get("enabled"), "pool": v.get("pool_host")}
                    for k, v in self.cfg.get("coins", {}).items()
                },
                "log": self.stats_log[-100:],
            }, headers={"Cache-Control": "no-store"})

        async def handle_config(req):
            if req.method == "POST":
                new_cfg = await req.json()
                self.cfg.update(new_cfg)
                save_config(self.cfg)
                return web.json_response({
                    "status": "saved",
                    "warning": "Restart hashburst-master to apply listener/upstream changes",
                })
            return web.json_response(self.cfg)

        app = web.Application()
        app.router.add_get("/api/status", handle_status)
        app.router.add_get("/api/config", handle_config)
        app.router.add_post("/api/config", handle_config)
        runner = web.AppRunner(app)
        await runner.setup()
        api_host = str(self.cfg.get("api_host") or "127.0.0.1")
        site = web.TCPSite(runner, api_host, int(self.cfg["api_port"]))
        await site.start()
        log.info("REST API listening on %s:%s", api_host, self.cfg["api_port"])
        await asyncio.Event().wait()


async def main():
    master = MasterNode()
    await master.start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
