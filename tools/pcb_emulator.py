#!/usr/bin/env python3
"""
HashBurst TEP bitstream PRIVATE RandomX diagnostic
Server B: 77.90.188.180
Master:   discovered through HashBurst HA ingress

This build computes real RandomX hashes over the Monero Stratum hashing blob,
within nonce segments assigned by the HashBurst master. A hash that meets the
pool target is emitted as TEP_SHARE and is therefore forwarded by the master to
its existing Stratum upstream connection.

Operational safety:
- changes only the emulator process on Server B;
- no direct connection from this process to the pool;
- no changes to Server A or other services;
- one fixed UDP source port per emulated board (54200, 54201);
- RandomX mode defaults to auto: fast if memory permits, otherwise light.
"""

import argparse
import asyncio
from collections import deque
import ctypes
import ctypes.util
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import socket
import struct
import time
import threading

Path("/var/log/hashburst").mkdir(parents=True, exist_ok=True)
log = logging.getLogger("emulator")
log.setLevel(logging.INFO)
log.propagate = False
log.handlers.clear()
_handler = RotatingFileHandler("/var/log/hashburst/emulator.log", maxBytes=20_000_000, backupCount=5)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
))
log.addHandler(_handler)

TEP_MAGIC = b"HBT1"
TEP_REGISTER = 0x10
TEP_JOB = 0x11
TEP_SHARE = 0x12
TEP_COIN_SWITCH = 0x13
TEP_HEARTBEAT = 0x14
TEP_STATS = 0x15
TEP_ACK = 0x16
TEP_BITSTREAM = 0x17
TYPE_NAMES = {
    TEP_REGISTER: "REGISTER", TEP_JOB: "JOB", TEP_SHARE: "SHARE",
    TEP_COIN_SWITCH: "COIN_SWITCH", TEP_HEARTBEAT: "HEARTBEAT",
    TEP_STATS: "STATS", TEP_ACK: "ACK", TEP_BITSTREAM: "BITSTREAM",
}

QUALIFICATION_FILE = Path(os.environ.get("HASHBURST_QUALIFICATION_FILE", "/var/lib/hashburst/qualification-status.json"))
EVIDENCE_FILE = Path(os.environ.get("HASHBURST_EVIDENCE_FILE", "/var/lib/hashburst/share-chain-evidence.jsonl"))
REQUIRE_QUALIFIED_POOL = os.environ.get("HASHBURST_REQUIRE_QUALIFIED_POOL", "1").strip().lower() not in {"0", "false", "no"}

def read_qualification():
    try:
        q = json.loads(QUALIFICATION_FILE.read_text())
        if isinstance(q, dict): return q
    except Exception:
        pass
    return {"version":"3.2","gate_open":False,"gate_reason":"QUALIFICATION_STATUS_UNAVAILABLE"}

def pool_gate_open():
    return (not REQUIRE_QUALIFIED_POOL) or bool(read_qualification().get("gate_open"))

def read_evidence_tail(limit=80):
    try:
        lines = EVIDENCE_FILE.read_text(errors="replace").splitlines()[-limit:]
        out=[]
        for line in lines:
            try: out.append(json.loads(line))
            except Exception: pass
        return out
    except Exception:
        return []


def tep_encode(msg_type: int, payload: dict) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(body) > 4089:
        raise ValueError(f"TEP payload too large: {len(body)}")
    return TEP_MAGIC + struct.pack(">BH", msg_type, len(body)) + body


def tep_decode(data: bytes):
    if len(data) < 7 or data[:4] != TEP_MAGIC:
        return None, None
    length = struct.unpack(">H", data[5:7])[0]
    if len(data) < 7 + length:
        return None, None
    try:
        return data[4], json.loads(data[7:7 + length].decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, None


def log_packet(direction: str, board_id: str, msg_type: int, payload: dict, raw: bytes):
    arrow = "TX -->" if direction == "TX" else "RX <--"
    name = TYPE_NAMES.get(msg_type, f"0x{msg_type:02x}")
    log.info("[%s] TEP %s %s len=%dB JSON=%s", board_id, arrow, name, len(raw),
             json.dumps(payload, separators=(",", ":"))[:500])


def mem_available_bytes() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 0


class RandomXEngine:
    """ctypes wrapper around the official RandomX C API."""

    FLAG_LARGE_PAGES = 1
    FLAG_FULL_MEM = 4

    def __init__(self, board_id: str, mode: str = "auto"):
        self.board_id = board_id
        self.mode_requested = mode
        self.mode = "light"
        self.lib = self._load_library()
        self._bind()
        self.flags = int(self.lib.randomx_get_flags())
        self.cache = None
        self.dataset = None
        self.vm = None
        self.seed = None
        self.hashes = 0
        self._rate_window = 10.0
        self._rate_samples = deque()
        # Native RandomX VM/cache/dataset lifecycle must be serialized.
        # asyncio cancellation does not stop a running asyncio.to_thread call.
        self._native_lock = threading.RLock()
        self._configure_mode()

    def _load_library(self):
        candidates = [
            os.environ.get("HASHBURST_RANDOMX_LIB"),
            "/usr/local/lib/libRandomX.so",
            "/usr/local/lib/librandomx.so",
            "/usr/lib/libRandomX.so",
            "/usr/lib/librandomx.so",
            ctypes.util.find_library("randomx"),
            ctypes.util.find_library("RandomX"),
        ]
        for path in candidates:
            if not path:
                continue
            try:
                return ctypes.CDLL(path)
            except OSError:
                continue
        raise RuntimeError(
            "RandomX shared library not found. Run ./install-randomx.sh before installing the emulator."
        )

    def _bind(self):
        L = self.lib
        L.randomx_get_flags.restype = ctypes.c_uint32
        L.randomx_alloc_cache.argtypes = [ctypes.c_uint32]
        L.randomx_alloc_cache.restype = ctypes.c_void_p
        L.randomx_init_cache.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        L.randomx_init_cache.restype = None
        L.randomx_release_cache.argtypes = [ctypes.c_void_p]
        L.randomx_release_cache.restype = None
        L.randomx_alloc_dataset.argtypes = [ctypes.c_uint32]
        L.randomx_alloc_dataset.restype = ctypes.c_void_p
        L.randomx_dataset_item_count.restype = ctypes.c_ulong
        L.randomx_init_dataset.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
        L.randomx_init_dataset.restype = None
        L.randomx_release_dataset.argtypes = [ctypes.c_void_p]
        L.randomx_release_dataset.restype = None
        L.randomx_create_vm.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
        L.randomx_create_vm.restype = ctypes.c_void_p
        L.randomx_destroy_vm.argtypes = [ctypes.c_void_p]
        L.randomx_destroy_vm.restype = None
        L.randomx_calculate_hash.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
        L.randomx_calculate_hash.restype = None

    def _configure_mode(self):
        req = self.mode_requested.lower()
        if req not in {"auto", "fast", "light"}:
            req = "auto"
        # Each board owns its own engine. Fast mode requires ~2.1 GiB per board.
        # Require 5.5 GiB available before selecting fast automatically for two boards.
        if req == "fast" or (req == "auto" and mem_available_bytes() >= 5_500_000_000):
            self.mode = "fast"
            self.flags |= self.FLAG_FULL_MEM
        else:
            self.mode = "light"
            self.flags &= ~self.FLAG_FULL_MEM
        # Large pages can fail depending on host sysctl; do not require them.
        self.flags &= ~self.FLAG_LARGE_PAGES
        log.info("[%s] RandomX mode=%s flags=0x%x", self.board_id, self.mode, self.flags)

    def _destroy(self):
        if self.vm:
            self.lib.randomx_destroy_vm(self.vm)
            self.vm = None
        if self.dataset:
            self.lib.randomx_release_dataset(self.dataset)
            self.dataset = None
        if self.cache:
            self.lib.randomx_release_cache(self.cache)
            self.cache = None

    def init_seed(self, seed_hex: str):
        if seed_hex == self.seed and self.vm:
            return
        seed = bytes.fromhex(seed_hex)
        if len(seed) != 32:
            raise ValueError("seed_hash must be 32 bytes")
        self._destroy()
        self.cache = self.lib.randomx_alloc_cache(self.flags)
        if not self.cache:
            raise RuntimeError("randomx_alloc_cache failed")
        seed_buf = ctypes.create_string_buffer(seed)
        self.lib.randomx_init_cache(self.cache, seed_buf, len(seed))

        if self.mode == "fast":
            self.dataset = self.lib.randomx_alloc_dataset(self.flags)
            if not self.dataset:
                raise RuntimeError("randomx_alloc_dataset failed")
            count = int(self.lib.randomx_dataset_item_count())
            log.info("[%s] RandomX dataset init: %d items", self.board_id, count)
            self.lib.randomx_init_dataset(self.dataset, self.cache, 0, count)
            self.vm = self.lib.randomx_create_vm(self.flags, None, self.dataset)
        else:
            self.vm = self.lib.randomx_create_vm(self.flags, self.cache, None)

        if not self.vm:
            raise RuntimeError("randomx_create_vm failed")
        self.seed = seed_hex
        log.info("[%s] RandomX seed ready %s...", self.board_id, seed_hex[:16])

    @staticmethod
    def nonce_offset(blob: bytes) -> int:
        # Current Monero hashing blobs place the 4-byte nonce at offset 39.
        # Validate the blob is long enough. This is exposed in API for diagnostics.
        if len(blob) < 43:
            raise ValueError(f"Monero hashing blob too short: {len(blob)}")
        return 39

    @staticmethod
    def decode_target_xmrig(target_hex: str):
        """Decode a 4-byte Monero Stratum target using XMRig-compatible scaling.

        The wire target is a little-endian uint32. XMRig converts that compact
        target to a uint64 threshold before testing the last 8 bytes of the
        256-bit hash as a little-endian uint64.
        """
        raw = bytes.fromhex(target_hex)
        if len(raw) != 4:
            raise ValueError(f"expected 4-byte pool target, got {len(raw)} bytes")
        target32 = int.from_bytes(raw, "little")
        if target32 == 0:
            raise ValueError("target decodes to zero")
        u32_max = (1 << 32) - 1
        u64_max = (1 << 64) - 1
        divisor = u32_max // target32
        if divisor == 0:
            raise ValueError("invalid target divisor")
        target64 = u64_max // divisor
        return target32, target64

    @staticmethod
    def hash_value64_xmrig(hash_bytes: bytes) -> int:
        if len(hash_bytes) != 32:
            raise ValueError(f"expected 32-byte RandomX hash, got {len(hash_bytes)} bytes")
        return int.from_bytes(hash_bytes[24:32], "little")

    @classmethod
    def meets_target(cls, hash_bytes: bytes, target64: int) -> bool:
        return cls.hash_value64_xmrig(hash_bytes) < target64

    def calculate(self, blob: bytes) -> bytes:
        inbuf = ctypes.create_string_buffer(blob)
        out = ctypes.create_string_buffer(32)
        self.lib.randomx_calculate_hash(self.vm, inbuf, len(blob), out)
        return out.raw

    def scan_batch(self, seed_hash: str, blob_hex: str, nonce_start: int,
                   nonce_count: int, target_hex: str, max_batch: int = 32):
        # CRITICAL: serialize *all* VM/cache/dataset access. A cancelled asyncio
        # task does not terminate the worker thread that is currently inside
        # randomx_calculate_hash(). Without this lock a newer job can call
        # init_seed() -> _destroy() while the old worker still uses self.vm,
        # which can segfault the Python process inside libRandomX.
        with self._native_lock:
            self.init_seed(seed_hash)
            blob = bytearray.fromhex(blob_hex)
            off = self.nonce_offset(blob)
            target32, target64 = self.decode_target_xmrig(target_hex)
            todo = min(nonce_count, max_batch)
            started = time.monotonic()
            for i in range(todo):
                nonce = (nonce_start + i) & 0xffffffff
                nonce_bytes = nonce.to_bytes(4, "little")
                blob[off:off + 4] = nonce_bytes
                h = self.calculate(bytes(blob))
                self.hashes += 1
                hash_tail64 = self.hash_value64_xmrig(h)
                if self.meets_target(h, target64):
                    return {
                        "found": True,
                        "nonce_int": nonce,
                        "nonce": nonce_bytes.hex(),
                        "result": h.hex(),
                        "hashes": i + 1,
                        "target32": target32,
                        "target64": target64,
                        "hash_tail32": int.from_bytes(h[28:32], "little"),
                        "hash_tail64": hash_tail64,
                        "elapsed": time.monotonic() - started,
                    }
            # Export one real hash sample from every native batch for the live
            # verification dashboard. This does not perform any extra hash and
            # does not affect share detection or TEP traffic.
            sample_nonce = (nonce_start + todo - 1) & 0xffffffff
            sample_nonce_bytes = sample_nonce.to_bytes(4, "little")
            return {
                "found": False, "hashes": todo, "elapsed": time.monotonic() - started,
                "sample_nonce_int": sample_nonce,
                "sample_nonce": sample_nonce_bytes.hex(),
                "sample_result": h.hex(),
                "sample_target32": target32,
                "sample_target64": target64,
                "sample_hash_tail32": int.from_bytes(h[28:32], "little"),
                "sample_hash_tail64": hash_tail64,
                "sample_meets_target": hash_tail64 < target64,
            }

    def record_rate(self, count: int):
        now = time.monotonic()
        self._rate_samples.append((now, count))
        cutoff = now - self._rate_window
        while self._rate_samples and self._rate_samples[0][0] < cutoff:
            self._rate_samples.popleft()

    @property
    def hashrate(self) -> float:
        now = time.monotonic()
        cutoff = now - self._rate_window
        while self._rate_samples and self._rate_samples[0][0] < cutoff:
            self._rate_samples.popleft()
        return sum(n for _, n in self._rate_samples) / self._rate_window

    def close(self):
        with self._native_lock:
            self._destroy()


class BoardEmulator:
    BASE_SRC_PORT = 54200

    def __init__(self, index: int, master_host: str, master_port: int, api_port: int, rx_mode: str):
        self.index = index
        self.board_id = f"poc-board-{index + 1}"
        self.master_host = master_host
        self.master_port = master_port
        self.api_port = api_port
        self.src_port = self.BASE_SRC_PORT + index
        self.rx = RandomXEngine(self.board_id, rx_mode)
        self.sock = None
        self.current_job = None
        self.shares = 0
        self.held_shares = 0
        self.rejected_local = 0
        self.mining = False
        self.mining_task = None
        self.boot_ts = time.time()
        self.event_log = []
        self.last_nonce = None
        self.last_result = None
        self.last_target32 = None
        self.last_target64 = None
        self.last_hash_tail32 = None
        self.last_hash_tail64 = None
        self.sample_job_id = None
        self.sample_nonce = None
        self.sample_result = None
        self.sample_target32 = None
        self.sample_target64 = None
        self.sample_hash_tail32 = None
        self.sample_hash_tail64 = None
        self.sample_meets_target = False
        self.sample_ts = None

    def _record(self, direction, msg_type, payload, raw):
        e = {"ts": time.time(), "dir": direction,
             "type": TYPE_NAMES.get(msg_type, f"0x{msg_type:02x}"),
             "board": self.board_id, "payload": payload, "hex": raw.hex()}
        self.event_log.append(e)
        self.event_log = self.event_log[-500:]
        log_packet(direction, self.board_id, msg_type, payload, raw)

    async def _send(self, loop, msg_type, payload):
        raw = tep_encode(msg_type, payload)
        await loop.sock_sendto(self.sock, raw, (self.master_host, self.master_port))
        self._record("TX", msg_type, payload, raw)

    async def boot(self, loop):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", self.src_port))
        self.sock.settimeout(1.0)
        log.info("[%s] UDP %d -> %s:%d", self.board_id, self.src_port,
                 self.master_host, self.master_port)
        await self._send(loop, TEP_REGISTER, {
            "node_id": self.board_id, "version": "1.4-private-randomx-qualified-stream",
            "firmware": "tep-miner-poc-randomx-private-v3.2", "network": "stagenet",
            "src_port": self.src_port,
            "caps": {"cpu": True, "fpga": False, "gpu": False, "asic": False,
                     "randomx": True, "segmented_nonce": True},
            "hw": {"mcu": "STM32F103C8T6-emulated", "fpga": "iCE40HX4K-TQ144-emulated",
                   "eth": "W5500-emulated", "pow_engine": "RandomX CPU reference"},
        })

    async def heartbeat(self, loop):
        await self._send(loop, TEP_HEARTBEAT, {
            "node_id": self.board_id, "hashrate": round(self.rx.hashrate, 2),
            "hashes": self.rx.hashes, "shares": self.shares,
            "uptime": int(time.time() - self.boot_ts), "mining": self.mining,
            "coin": "XMR", "pow": "RandomX", "randomx_mode": self.rx.mode,
            "qualification_gate_open": pool_gate_open(), "held_shares": self.held_shares,
        })

    async def share(self, loop, job_id, found):
        self.shares += 1
        self.last_nonce = found["nonce"]
        self.last_result = found["result"]
        self.last_target32 = found["target32"]
        self.last_target64 = found["target64"]
        self.last_hash_tail32 = found["hash_tail32"]
        self.last_hash_tail64 = found["hash_tail64"]
        payload = {
            "node_id": self.board_id, "job_id": job_id,
            "nonce": found["nonce"], "result": found["result"], "coin": "XMR",
            "hashrate": round(self.rx.hashrate, 2), "pow": "RandomX",
            "randomx_mode": self.rx.mode,
            "target32": found["target32"], "target64": found["target64"],
            "hash_tail32": found["hash_tail32"], "hash_tail64": found["hash_tail64"],
        }
        log.info("[%s] VALID TARGET SHARE job=%s nonce=%s tail64=%d target64=%d",
                 self.board_id, job_id, found["nonce"], found["hash_tail64"], found["target64"])
        await self._send(loop, TEP_SHARE, payload)

    async def segment_done(self, loop, job_id, hashes_tried):
        await self._send(loop, TEP_STATS, {
            "node_id": self.board_id, "event": "segment_done", "job_id": job_id,
            "hashes_tried": hashes_tried, "hashrate": round(self.rx.hashrate, 2),
            "coin": "XMR", "pow": "RandomX", "randomx_mode": self.rx.mode,
        })

    async def _handle(self, data, addr, loop):
        t, p = tep_decode(data)
        if t is None:
            return
        self._record("RX", t, p, data)
        if t == TEP_JOB:
            if self.mining_task and not self.mining_task.done():
                self.mining_task.cancel()
            self.current_job = p
            self.mining = True
            self.mining_task = asyncio.create_task(self._mine_job(p, loop))

    async def _mine_job(self, job, loop):
        job_id = str(job.get("job_id", ""))
        blob = str(job.get("blob", ""))
        seed = str(job.get("seed_hash", ""))
        target = str(job.get("target", ""))
        start = int(str(job.get("nonce_start", "0x0")), 16)
        count = int(job.get("nonce_count", 500000))
        done = 0
        batch = int(os.environ.get("HASHBURST_RANDOMX_BATCH", "8"))
        log.info("[%s] RandomX job=%s start=%s count=%d target=%s mode=%s",
                 self.board_id, job_id, hex(start), count, target, self.rx.mode)
        try:
            while done < count:
                n = min(batch, count - done)
                result = await asyncio.to_thread(
                    self.rx.scan_batch, seed, blob, start + done, n, target, n
                )
                hashed = int(result.get("hashes", 0))
                done += hashed
                self.rx.record_rate(hashed)
                if result.get("sample_result"):
                    self.sample_job_id = job_id
                    self.sample_nonce = result.get("sample_nonce")
                    self.sample_result = result.get("sample_result")
                    self.sample_target32 = result.get("sample_target32")
                    self.sample_target64 = result.get("sample_target64")
                    self.sample_hash_tail32 = result.get("sample_hash_tail32")
                    self.sample_hash_tail64 = result.get("sample_hash_tail64")
                    self.sample_meets_target = bool(result.get("sample_meets_target"))
                    self.sample_ts = time.time()
                if result.get("found"):
                    # A qualifying hash is also the strongest possible sample.
                    self.sample_job_id = job_id
                    self.sample_nonce = result.get("nonce")
                    self.sample_result = result.get("result")
                    self.sample_target32 = result.get("target32")
                    self.sample_target64 = result.get("target64")
                    self.sample_hash_tail32 = result.get("hash_tail32")
                    self.sample_hash_tail64 = result.get("hash_tail64")
                    self.sample_meets_target = True
                    self.sample_ts = time.time()
                    if pool_gate_open():
                        await self.share(loop, job_id, result)
                    else:
                        self.held_shares += 1
                        log.warning("[%s] QUALIFICATION HOLD job=%s nonce=%s: pool control lane not yet qualified", self.board_id, job_id, result.get("nonce"))
                        await self._send(loop, TEP_STATS, {
                            "node_id": self.board_id, "event": "qualification_hold", "job_id": job_id,
                            "nonce": result.get("nonce"), "result": result.get("result"),
                            "target64": result.get("target64"), "hash_tail64": result.get("hash_tail64"),
                            "coin": "XMR", "forward_to_pool": False, "qualification_gate_open": False,
                        })
                    # A qualifying hash ends this assigned quantum. Master/new pool job supplies more work.
                    return
                await asyncio.sleep(0)
            await self.segment_done(loop, job_id, done)
        except asyncio.CancelledError:
            log.info("[%s] segment cancelled by newer pool job", self.board_id)
            raise
        except Exception as e:
            self.rejected_local += 1
            log.exception("[%s] RandomX mining error: %s", self.board_id, e)
            await self._send(loop, TEP_STATS, {
                "node_id": self.board_id, "event": "randomx_error", "job_id": job_id,
                "error": str(e), "coin": "XMR",
            })
        finally:
            if asyncio.current_task() is self.mining_task:
                self.mining = False

    async def run(self):
        loop = asyncio.get_running_loop()
        await self.boot(loop)
        last_hb = 0.0
        while True:
            now = time.time()
            try:
                data, addr = await asyncio.wait_for(
                    loop.run_in_executor(None, self.sock.recvfrom, 4096), timeout=1.0
                )
                await self._handle(data, addr, loop)
            except asyncio.TimeoutError:
                pass
            except OSError as e:
                log.warning("[%s] socket: %s", self.board_id, e)
                await asyncio.sleep(1)
            if now - last_hb >= 10.0:
                last_hb = now
                await self.heartbeat(loop)


class EmulatorAPI:
    def __init__(self, boards, port):
        self.boards = boards
        self.port = port

    def status(self, b):
        return {
            "board_id": b.board_id, "src_port": b.src_port, "worker_type": "cpu-randomx",
            "hw": {"mcu": "STM32F103C8T6-emulated", "fpga": "iCE40HX4K-TQ144-emulated",
                   "eth": "W5500-emulated", "pow_engine": "RandomX"},
            "active": True, "hashrate": round(b.rx.hashrate, 2), "hashes": b.rx.hashes,
            "shares": b.shares, "held_shares": b.held_shares, "rejected_local": b.rejected_local,
            "qualification_gate_open": pool_gate_open(),
            "safe_poc": False, "pow": "RandomX", "randomx_mode": b.rx.mode,
            "mining": b.mining, "uptime": int(time.time() - b.boot_ts),
            "last_nonce": b.last_nonce, "last_result": b.last_result,
            "last_target32": b.last_target32, "last_target64": b.last_target64,
            "last_hash_tail32": b.last_hash_tail32, "last_hash_tail64": b.last_hash_tail64,
            "sample_job_id": b.sample_job_id, "sample_nonce": b.sample_nonce,
            "sample_result": b.sample_result, "sample_target32": b.sample_target32,
            "sample_target64": b.sample_target64,
            "sample_hash_tail32": b.sample_hash_tail32,
            "sample_hash_tail64": b.sample_hash_tail64,
            "sample_meets_target": b.sample_meets_target, "sample_ts": b.sample_ts,
            "log": b.event_log[-60:],
        }

    async def start(self):
        from aiohttp import web
        async def boards(req):
            return web.json_response([self.status(b) for b in self.boards],
                                     headers={"Access-Control-Allow-Origin": "*"})
        async def eventlog(req):
            x = [e for b in self.boards for e in b.event_log]
            x.sort(key=lambda e: e["ts"])
            return web.json_response(x[-250:], headers={"Access-Control-Allow-Origin": "*"})
        async def qualification(req):
            return web.json_response(read_qualification(), headers={"Access-Control-Allow-Origin": "*", "Cache-Control":"no-store"})
        async def evidence(req):
            return web.json_response(read_evidence_tail(), headers={"Access-Control-Allow-Origin": "*", "Cache-Control":"no-store"})
        async def ws_handler(req):
            ws = web.WebSocketResponse()
            await ws.prepare(req)
            while not ws.closed:
                try:
                    await ws.send_json({"boards": [self.status(b) for b in self.boards], "qualification": read_qualification(), "evidence_tail": read_evidence_tail(20), "ts": time.time()})
                    await asyncio.sleep(1)
                except Exception:
                    break
            return ws
        app = web.Application()
        app.router.add_get("/api/boards", boards)
        app.router.add_get("/api/log", eventlog)
        app.router.add_get("/api/qualification", qualification)
        app.router.add_get("/api/evidence", evidence)
        app.router.add_get("/ws", ws_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", self.port).start()
        log.info("Emulator API listening on 0.0.0.0:%d", self.port)


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--master", required=True)
    p.add_argument("--tep-port", type=int, default=8765)
    p.add_argument("--api-port", type=int, default=9100)
    p.add_argument("--boards", type=int, default=2)
    p.add_argument("--randomx-mode", choices=["auto", "fast", "light"],
                   default=os.environ.get("HASHBURST_RANDOMX_MODE", "auto"))
    a = p.parse_args()
    log.info("HashBurst PRIVATE RandomX qualification-gated proof-stream v3.2 starting; master=%s:%d boards=%d mode=%s",
             a.master, a.tep_port, a.boards, a.randomx_mode)
    boards = [BoardEmulator(i, a.master, a.tep_port, a.api_port, a.randomx_mode)
              for i in range(a.boards)]
    api = EmulatorAPI(boards, a.api_port)
    await asyncio.gather(*(b.run() for b in boards), api.start())


if __name__ == "__main__":
    asyncio.run(main())
