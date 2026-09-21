#!/usr/bin/env python3
"""HashBurst RandomX share-chain verifier.

Read-only/diagnostic utility for the private HashBurst TEP-MINER PoC.
It does NOT submit synthetic shares and does NOT modify master/emulator services.

Checks:
  1. Monero/XMRig nonce offset and nonce wire serialization.
  2. XMRig-compatible 4-byte Stratum target -> 64-bit target conversion.
  3. XMRig-compatible hash target comparison using bytes 24..31 as LE uint64.
  4. Independent RandomX hash of a selected live job/nonce via libRandomX.so.
  5. Live evidence chain: emulator TEP shares -> master pool accepted/rejected counters.
  6. Optional source audit of the deployed pcb_emulator.py comparator.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

U32_MAX = 0xFFFFFFFF
U64_MAX = 0xFFFFFFFFFFFFFFFF
MONERO_NONCE_OFFSET = 39
RANDOMX_FLAG_LARGE_PAGES = 1
RANDOMX_FLAG_FULL_MEM = 4


def http_json(url: str, timeout: float = 5.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "HashBurst-Share-Verifier/3.2.3"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def find_key(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            out = find_key(v, key, None)
            if out is not None:
                return out
    elif isinstance(obj, list):
        for v in obj:
            out = find_key(v, key, None)
            if out is not None:
                return out
    return default


def latest_job_from_boards(boards: List[Dict[str, Any]]) -> Dict[str, Any]:
    jobs: List[Tuple[float, Dict[str, Any]]] = []
    for b in boards:
        for e in b.get("log", []):
            if e.get("dir") == "RX" and e.get("type") == "JOB" and isinstance(e.get("payload"), dict):
                jobs.append((float(e.get("ts", 0.0)), e["payload"]))
    if not jobs:
        raise RuntimeError("No RX JOB found in emulator /api/boards logs")
    jobs.sort(key=lambda x: x[0])
    return jobs[-1][1]


def decode_target_xmrig(target_hex: str) -> Tuple[int, int]:
    raw = bytes.fromhex(target_hex)
    if len(raw) != 4:
        raise ValueError(f"Expected 4-byte Stratum target, got {len(raw)} bytes")
    target32_le = int.from_bytes(raw, "little")
    if target32_le == 0:
        raise ValueError("Target decodes to zero")
    divisor = U32_MAX // target32_le
    if divisor == 0:
        raise ValueError("Invalid target divisor")
    target64 = U64_MAX // divisor
    return target32_le, target64


def hash_value64_xmrig(hash_bytes: bytes) -> int:
    if len(hash_bytes) != 32:
        raise ValueError("RandomX hash must be exactly 32 bytes")
    return int.from_bytes(hash_bytes[24:32], "little")


def old_approx_value32(hash_bytes: bytes) -> int:
    return int.from_bytes(hash_bytes[28:32], "little")


def nonce_wire_hex(nonce: int) -> str:
    if not 0 <= nonce <= U32_MAX:
        raise ValueError("nonce must fit uint32")
    return nonce.to_bytes(4, "little").hex()


def inject_nonce(blob_hex: str, nonce: int) -> bytes:
    blob = bytearray.fromhex(blob_hex)
    if len(blob) < MONERO_NONCE_OFFSET + 4:
        raise ValueError(f"Hashing blob too short ({len(blob)} bytes)")
    blob[MONERO_NONCE_OFFSET:MONERO_NONCE_OFFSET + 4] = nonce.to_bytes(4, "little")
    return bytes(blob)


class RandomXLight:
    """Small independent ctypes wrapper using RandomX light mode."""

    def __init__(self, lib_path: Optional[str] = None):
        self.lib = self._load(lib_path)
        self._bind()
        flags = int(self.lib.randomx_get_flags())
        flags &= ~RANDOMX_FLAG_FULL_MEM
        flags &= ~RANDOMX_FLAG_LARGE_PAGES
        self.flags = flags
        self.cache = None
        self.vm = None
        self.seed: Optional[bytes] = None

    @staticmethod
    def _load(explicit: Optional[str]):
        candidates = [
            explicit,
            os.environ.get("HASHBURST_RANDOMX_LIB"),
            "/usr/local/lib/libRandomX.so",
            "/usr/local/lib/librandomx.so",
            "/usr/lib/libRandomX.so",
            "/usr/lib/librandomx.so",
            ctypes.util.find_library("randomx"),
            ctypes.util.find_library("RandomX"),
        ]
        for p in candidates:
            if not p:
                continue
            try:
                return ctypes.CDLL(p)
            except OSError:
                pass
        raise RuntimeError("libRandomX.so not found")

    def _bind(self):
        L = self.lib
        L.randomx_get_flags.restype = ctypes.c_uint32
        L.randomx_alloc_cache.argtypes = [ctypes.c_uint32]
        L.randomx_alloc_cache.restype = ctypes.c_void_p
        L.randomx_init_cache.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        L.randomx_init_cache.restype = None
        L.randomx_release_cache.argtypes = [ctypes.c_void_p]
        L.randomx_release_cache.restype = None
        L.randomx_create_vm.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
        L.randomx_create_vm.restype = ctypes.c_void_p
        L.randomx_destroy_vm.argtypes = [ctypes.c_void_p]
        L.randomx_destroy_vm.restype = None
        L.randomx_calculate_hash.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
        L.randomx_calculate_hash.restype = None

    def init(self, seed_hex: str):
        seed = bytes.fromhex(seed_hex)
        if len(seed) != 32:
            raise ValueError("seed_hash must be 32 bytes")
        if seed == self.seed and self.vm:
            return
        self.close()
        self.cache = self.lib.randomx_alloc_cache(self.flags)
        if not self.cache:
            raise RuntimeError("randomx_alloc_cache failed")
        seed_buf = ctypes.create_string_buffer(seed)
        self.lib.randomx_init_cache(self.cache, seed_buf, len(seed))
        self.vm = self.lib.randomx_create_vm(self.flags, self.cache, None)
        if not self.vm:
            raise RuntimeError("randomx_create_vm failed")
        self.seed = seed

    def hash(self, seed_hex: str, blob: bytes) -> bytes:
        self.init(seed_hex)
        inp = ctypes.create_string_buffer(blob)
        out = ctypes.create_string_buffer(32)
        self.lib.randomx_calculate_hash(self.vm, inp, len(blob), out)
        return out.raw

    def close(self):
        if self.vm:
            self.lib.randomx_destroy_vm(self.vm)
            self.vm = None
        if self.cache:
            self.lib.randomx_release_cache(self.cache)
            self.cache = None
        self.seed = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def audit_source(path: str) -> int:
    p = Path(path)
    if not p.exists():
        print(f"[SKIP] source audit: {path} not found")
        return 0
    s = p.read_text(errors="replace")
    ok = True
    print(f"Source audit: {path}")
    if re.search(r"return\s+39\b", s) or "nonce_offset" in s and "39" in s:
        print("  [PASS] nonce offset 39 present")
    else:
        print("  [WARN] could not confirm nonce offset 39")
    if "hash_bytes[28:32]" in s and "target32" in s:
        print("  [FAIL] comparator uses only hash[28:32] vs raw target32")
        print("         XMRig-compatible exact test uses LE uint64 hash[24:32] vs expanded target64")
        ok = False
    elif "hash_bytes[24:32]" in s and ("target64" in s or "xmrig" in s.lower()):
        print("  [PASS] exact 64-bit XMRig-style comparator appears present")
    else:
        print("  [WARN] comparator pattern not recognized; inspect manually")
    if 'nonce.to_bytes(4, "little")' in s or "nonce.to_bytes(4, 'little')" in s:
        print("  [PASS] nonce serialized little-endian in blob/wire helper")
    else:
        print("  [WARN] little-endian nonce serialization not recognized")
    return 0 if ok else 2


def print_target(target_hex: str):
    t32, t64 = decode_target_xmrig(target_hex)
    diff_approx = U64_MAX / t64
    print("Target analysis (XMRig-compatible):")
    print(f"  raw target          : {target_hex}")
    print(f"  target32 LE         : {t32} (0x{t32:08x})")
    print(f"  expanded target64   : {t64} (0x{t64:016x})")
    print(f"  expected hashes/share ~ {diff_approx:,.2f}")


def verify_job(job: Dict[str, Any], nonce: int, lib: Optional[str] = None,
               expected_result: Optional[str] = None) -> int:
    blob_hex = str(job["blob"])
    seed = str(job["seed_hash"])
    target_hex = str(job["target"])
    job_id = str(job.get("job_id", ""))
    blob = inject_nonce(blob_hex, nonce)
    t32, t64 = decode_target_xmrig(target_hex)

    rx = RandomXLight(lib)
    try:
        h = rx.hash(seed, blob)
    finally:
        rx.close()

    value64 = hash_value64_xmrig(h)
    approx32 = old_approx_value32(h)
    valid_exact = value64 < t64
    valid_old = approx32 < t32

    print("\nIndependent job/nonce verification")
    print(f"  job_id              : {job_id}")
    print(f"  blob bytes          : {len(blob)}")
    print(f"  nonce offset        : {MONERO_NONCE_OFFSET}")
    print(f"  nonce integer       : {nonce} (0x{nonce:08x})")
    print(f"  nonce wire hex (LE) : {nonce_wire_hex(nonce)}")
    print(f"  blob[39:43]         : {blob[39:43].hex()}")
    print(f"  seed_hash           : {seed}")
    print(f"  RandomX result      : {h.hex()}")
    print(f"  hash[24:32] LE u64  : {value64} (0x{value64:016x})")
    print(f"  XMRig target64      : {t64} (0x{t64:016x})")
    print(f"  exact share detect  : {'YES' if valid_exact else 'NO'}")
    print(f"  old approx32 detect : {'YES' if valid_old else 'NO'}")
    print(f"  criteria agree      : {'YES' if valid_exact == valid_old else 'NO <-- IMPORTANT'}")

    if expected_result:
        same = h.hex().lower() == expected_result.lower().removeprefix("0x")
        print(f"  expected result     : {expected_result}")
        print(f"  bit-for-bit match   : {'PASS' if same else 'FAIL'}")
        if not same:
            return 3

    print("\nExpected Stratum submit fields for THIS nonce/hash:")
    print(json.dumps({
        "job_id": job_id,
        "nonce": nonce_wire_hex(nonce),
        "result": h.hex(),
    }, indent=2))
    return 0


def snapshot(emulator_api: str, master_api: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
    boards = http_json(emulator_api)
    master = http_json(master_api)
    if not isinstance(boards, list):
        raise RuntimeError("Emulator API did not return a board list")
    if not isinstance(master, dict):
        raise RuntimeError("Master API did not return an object")
    job = latest_job_from_boards(boards)

    print("Live snapshot")
    for b in boards:
        print(
            f"  {b.get('board_id')}: active={b.get('active')} mining={b.get('mining')} "
            f"hashes={b.get('hashes')} H/s={b.get('hashrate')} TEP_shares={b.get('shares')}"
        )
    print(f"  master pool_accepted={find_key(master, 'pool_accepted', '?')} pool_rejected={find_key(master, 'pool_rejected', '?')}")
    print(f"  latest job={job.get('job_id')} target={job.get('target')} start={job.get('nonce_start')} count={job.get('nonce_count')}")
    print_target(str(job["target"]))
    return boards, master, job


def event_key(e: Dict[str, Any]) -> str:
    return json.dumps(e, sort_keys=True, separators=(",", ":"))


def recent_master_events(master: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    log = master.get("log")
    if not isinstance(log, list):
        return []
    wanted = {
        "share", "stratum_tx", "stratum_rx", "share_accepted", "share_rejected",
        "share_forward_error", "dispatch",
    }
    return [e for e in log if isinstance(e, dict) and e.get("type") in wanted]


def _safe_ratio(accepted: int, rejected: int) -> float:
    total = accepted + rejected
    return (accepted / total) if total else 0.0


def _atomic_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True))
    os.replace(tmp, path)


def watch(emulator_api: str, master_api: str, interval: float, evidence: str,
          qualification_file: str, baseline_file: str, control_worker: str,
          min_control_accepted: int, max_control_reject_pct: float,
          audited_evidence_file: str) -> int:
    out = Path(evidence)
    qout = Path(qualification_file)
    bout = Path(baseline_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    boards, master, _ = snapshot(emulator_api, master_api)
    last_board_shares = {str(b.get("board_id")): int(b.get("shares", 0) or 0) for b in boards}
    current_acc = int(find_key(master, "pool_accepted", 0) or 0)
    current_rej = int(find_key(master, "pool_rejected", 0) or 0)

    # v3.2.3: persistent experiment baseline. A verifier restart must not reset Δ.
    baseline = {}
    try:
        if bout.exists():
            baseline = json.loads(bout.read_text())
    except Exception:
        baseline = {}
    if not baseline:
        baseline = {"pool_accepted": current_acc, "pool_rejected": current_rej, "started_at": time.time(), "source": "auto-created-v3.2.3"}
        _atomic_json(bout, baseline)
    base_acc = int(baseline.get("pool_accepted", current_acc) or 0)
    base_rej = int(baseline.get("pool_rejected", current_rej) or 0)
    started = float(baseline.get("started_at", time.time()) or time.time())
    last_acc, last_rej = current_acc, current_rej
    seen = {event_key(e) for e in recent_master_events(master)}

    # Restore categorized counters across verifier restarts when available.
    control_acc = control_rej = pcb_acc = pcb_rej = other_acc = other_rej = 0
    try:
        if qout.exists():
            prev = json.loads(qout.read_text())
            pb = prev.get("baseline", {})
            if int(pb.get("pool_accepted", -1)) == base_acc and int(pb.get("pool_rejected", -1)) == base_rej:
                control_acc = int(prev.get("control", {}).get("accepted", 0) or 0)
                control_rej = int(prev.get("control", {}).get("rejected", 0) or 0)
                pcb_acc = int(prev.get("pcb", {}).get("accepted", 0) or 0)
                pcb_rej = int(prev.get("pcb", {}).get("rejected", 0) or 0)
                other_acc = int(prev.get("other", {}).get("accepted", 0) or 0)
                other_rej = int(prev.get("other", {}).get("rejected", 0) or 0)
    except Exception:
        pass
    audited = {}
    try:
        apath = Path(audited_evidence_file)
        if apath.exists():
            audited = json.loads(apath.read_text())
    except Exception as e:
        audited = {"load_error": str(e)}

    print("\nWatching end-to-end chain + qualification gate. Ctrl-C to stop.")
    print(f"Evidence JSONL: {out}")
    print(f"Qualification JSON: {qout}")
    print(f"Persistent baseline JSON: {bout}")
    print(f"Baseline lifetime counters: accepted={base_acc} rejected={base_rej}")
    print(f"Control worker: {control_worker}; minimum accepted={min_control_accepted}; max reject={max_control_reject_pct:.4f}%")
    print("No synthetic shares will be sent. Historical counters do not enter the qualification ratio.\n")

    def publish(master_obj, boards_obj):
        acc = int(find_key(master_obj, "pool_accepted", 0) or 0)
        rej = int(find_key(master_obj, "pool_rejected", 0) or 0)
        ctot = control_acc + control_rej
        crp = (100.0 * control_rej / ctot) if ctot else 0.0
        caccept = _safe_ratio(control_acc, control_rej)
        stot = max(0, acc-base_acc) + max(0, rej-base_rej)
        srej = max(0, rej-base_rej)
        session_reject_pct = (100.0*srej/stot) if stot else 0.0
        session_acc = max(0, acc-base_acc)
        # Accepted growth is measured against the persistent experiment baseline.
        # With baseline accepted=1 and lifetime accepted=5 this is +400%.
        accepted_growth_pct = (100.0 * session_acc / base_acc) if base_acc > 0 else (100.0 if session_acc > 0 else 0.0)
        # Reject trend is the experiment reject rate; the desired limit is 0%.
        rejected_rate_pct = session_reject_pct
        gate = control_acc >= min_control_accepted and crp <= max_control_reject_pct
        obj = {
            "version": "3.2.3",
            "updated_at": time.time(),
            "started_at": started,
            "baseline": {"pool_accepted": base_acc, "pool_rejected": base_rej, "persistent": True, "file": str(bout)},
            "lifetime": {"pool_accepted": acc, "pool_rejected": rej},
            "session": {
                "accepted": session_acc, "rejected": max(0, rej-base_rej),
                "accepted_ratio": (1.0-session_reject_pct/100.0) if stot else 0.0,
                "reject_pct": session_reject_pct,
            },
            "trends": {
                "accepted_growth_pct": accepted_growth_pct,
                "rejected_rate_pct": rejected_rate_pct,
                "rejected_target_pct": 0.0,
                "accepted_baseline": base_acc,
                "accepted_lifetime": acc,
            },
            "control": {
                "worker": control_worker, "accepted": control_acc, "rejected": control_rej,
                "accepted_ratio": caccept, "reject_pct": crp,
                "min_accepted": min_control_accepted, "max_reject_pct": max_control_reject_pct,
            },
            "pcb": {"accepted": pcb_acc, "rejected": pcb_rej},
            "other": {"accepted": other_acc, "rejected": other_rej},
            "audited_evidence": audited,
            "cluster_path_proven": bool(int(audited.get("pcb_exact_target64_accepted", 0) or 0) > 0),
            "gate_open": gate,
            "gate_reason": "CONTROL_POOL_PATH_QUALIFIED" if gate else "WAITING_FOR_REAL_CONTROL_ACCEPTED",
            "boards": [{"board_id": b.get("board_id"), "hashrate": b.get("hashrate"), "hashes": b.get("hashes"), "shares": b.get("shares")} for b in boards_obj],
        }
        _atomic_json(qout, obj)
        return obj

    publish(master, boards)
    with out.open("a", buffering=1) as f:
        try:
            while True:
                ts = time.time()
                try:
                    boards = http_json(emulator_api)
                    master = http_json(master_api)
                except Exception as e:
                    print(f"[{time.strftime('%H:%M:%S')}] API error: {e}")
                    time.sleep(interval)
                    continue

                bshares = {str(b.get("board_id")): int(b.get("shares", 0) or 0) for b in boards}
                acc = int(find_key(master, "pool_accepted", 0) or 0)
                rej = int(find_key(master, "pool_rejected", 0) or 0)

                new_events = []
                for e in recent_master_events(master):
                    k = event_key(e)
                    if k in seen:
                        continue
                    seen.add(k); new_events.append(e)
                    typ=e.get("type"); worker=str(e.get("worker") or e.get("node_id") or "")
                    if typ in {"share_accepted","share_rejected"}:
                        is_acc = typ == "share_accepted"
                        if worker == control_worker:
                            if is_acc: control_acc += 1
                            else: control_rej += 1
                        elif worker.startswith("poc-board-"):
                            if is_acc: pcb_acc += 1
                            else: pcb_rej += 1
                        else:
                            if is_acc: other_acc += 1
                            else: other_rej += 1
                    if typ in {"share", "stratum_tx", "stratum_rx", "share_accepted", "share_rejected", "share_forward_error"}:
                        print(f"[{time.strftime('%H:%M:%S')}] master event {typ}: {json.dumps(e, separators=(',', ':'))}")

                q = publish(master, boards)
                record = {"ts": ts, "qualification": q, "new_master_events": new_events}
                f.write(json.dumps(record, separators=(",", ":")) + "\n")

                for bid, n in bshares.items():
                    old = last_board_shares.get(bid, 0)
                    if n > old:
                        print(f"[{time.strftime('%H:%M:%S')}] PCB TEP SHARE DETECTED: {bid} {old} -> {n}")
                if acc > last_acc:
                    print(f"[{time.strftime('%H:%M:%S')}] POOL ACCEPTED lifetime {last_acc} -> {acc}; session +{q['session']['accepted']}")
                if rej > last_rej:
                    print(f"[{time.strftime('%H:%M:%S')}] POOL REJECTED lifetime {last_rej} -> {rej}; session +{q['session']['rejected']}")
                if q["gate_open"]:
                    print(f"[{time.strftime('%H:%M:%S')}] QUALIFICATION GATE OPEN: control accepted={control_acc} rejected={control_rej} reject={q['control']['reject_pct']:.6f}%")

                last_board_shares = bshares
                last_acc, last_rej = acc, rej
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0

def parse_nonce(v: str) -> int:
    return int(v, 0)


def main() -> int:
    ap = argparse.ArgumentParser(description="HashBurst RandomX share-chain verifier")
    ap.add_argument("--emulator-api", default="http://127.0.0.1:9100/api/boards")
    ap.add_argument("--master-api", default="https://blockchainapi.one/api/hashburst/master/status")
    ap.add_argument("--randomx-lib", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("audit-source", help="audit deployed emulator nonce/target code")
    p.add_argument("--source", default="/opt/hashburst/pcb_emulator.py")

    sub.add_parser("snapshot", help="show live boards/master/job and decode target")

    p = sub.add_parser("verify-live-job", help="independently hash a nonce from the latest live JOB")
    p.add_argument("--nonce", default=None, help="integer/0xHEX; default = live nonce_start")
    p.add_argument("--expected-result", default=None, help="optional 64-hex result for bit-for-bit comparison")

    p = sub.add_parser("verify-job", help="verify explicit blob/seed/target/job/nonce")
    p.add_argument("--blob", required=True)
    p.add_argument("--seed-hash", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--job-id", default="manual")
    p.add_argument("--nonce", required=True)
    p.add_argument("--expected-result", default=None)

    p = sub.add_parser("watch", help="watch TEP share -> pool evidence and maintain v3.2.2 persistent experiment qualification state")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--evidence", default="/var/lib/hashburst/share-chain-evidence.jsonl")
    p.add_argument("--qualification-file", default="/var/lib/hashburst/qualification-status.json")
    p.add_argument("--baseline-file", default="/var/lib/hashburst/qualification-baseline.json")
    p.add_argument("--control-worker", default="master-control-1")
    p.add_argument("--min-control-accepted", type=int, default=1)
    p.add_argument("--max-control-reject-pct", type=float, default=0.01, help="percent, e.g. 0.01 = 0.01%%")
    p.add_argument("--audited-evidence", default="/var/lib/hashburst/audited-evidence.json")

    args = ap.parse_args()

    try:
        if args.cmd == "audit-source":
            return audit_source(args.source)
        if args.cmd == "snapshot":
            snapshot(args.emulator_api, args.master_api)
            return 0
        if args.cmd == "verify-live-job":
            boards = http_json(args.emulator_api)
            job = latest_job_from_boards(boards)
            nonce = parse_nonce(args.nonce) if args.nonce else int(str(job.get("nonce_start", "0x0")), 16)
            print_target(str(job["target"]))
            return verify_job(job, nonce, args.randomx_lib, args.expected_result)
        if args.cmd == "verify-job":
            job = {
                "blob": args.blob,
                "seed_hash": args.seed_hash,
                "target": args.target,
                "job_id": args.job_id,
            }
            print_target(args.target)
            return verify_job(job, parse_nonce(args.nonce), args.randomx_lib, args.expected_result)
        if args.cmd == "watch":
            return watch(args.emulator_api, args.master_api, args.interval, args.evidence,
                         args.qualification_file, args.baseline_file, args.control_worker,
                         args.min_control_accepted, args.max_control_reject_pct,
                         args.audited_evidence)
    except (ValueError, RuntimeError, urllib.error.URLError, KeyError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
