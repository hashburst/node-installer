#!/usr/bin/env python3
"""HashBurst storage network aggregator v2.1.2.

Capacity classes:
  primary/secondary -> committable (eligible for sellable capacity)
  edge              -> best-effort replica capacity only

Safety invariants:
  * no authenticated/current primary => accounting unavailable, sellable=None
  * duplicate node_id entries are counted once
  * node summary with available=false or stale timestamp is not online/accountable
  * sellable = min(commitment headroom, physical free headroom)
  * edge capacity never increases sellable capacity
"""
from __future__ import annotations
import json, os, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

NODES_FILE = os.environ.get("HB_STORAGE_NODES", "/etc/hashburst/storage-nodes.json")
TIMEOUT = float(os.environ.get("HB_AGGREGATOR_TIMEOUT", "3"))
MAX_STALE_SEC = int(os.environ.get("HB_AGGREGATOR_MAX_STALE_SEC", "120"))
MAX_RESPONSE = int(os.environ.get("HB_AGGREGATOR_MAX_RESPONSE", "65536"))


def discover_nodes() -> list[dict]:
    try:
        data = json.loads(Path(NODES_FILE).read_text())
        nodes = data.get("nodes", [])
        return [n for n in nodes if isinstance(n, dict) and n.get("url") and n.get("enabled", True) is not False]
    except Exception:
        return []


def _fetch_summary(node: dict) -> dict:
    url = node["url"].rstrip("/") + "/api/public/storage-summary"
    out = {"name": node.get("name", "?"), "url": node["url"], "online": False,
           "configured_class": node.get("capacity_class"),
           "configured_role": node.get("role")}
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "HashBurst-Aggregator/2.1"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError("summary too large")
            summary = json.loads(raw.decode("utf-8", "strict"))
        if summary.get("available") is False:
            raise ValueError("node summary unavailable")
        node_id = str(summary.get("node_id") or "").strip()
        role = str(summary.get("role") or "").strip()
        if not node_id or role not in {"primary", "secondary", "edge"}:
            raise ValueError("invalid node_id/role")
        total = float(summary.get("capacity_total_gb"))
        used = float(summary.get("used_gb") or 0)
        if total < 0 or used < 0 or used > total * 1.05:
            raise ValueError("invalid capacity values")
        ts = int(summary.get("timestamp") or 0)
        if ts and abs(int(time.time()) - ts) > MAX_STALE_SEC:
            raise ValueError("stale summary")
        out.update(summary)
        out["online"] = True
    except Exception as e:
        out["error"] = str(e)[:120]
    return out



def _capacity_class(result: dict) -> str:
    """Return a fail-safe public capacity class.

    Online nodes are classified from their validated role so display and accounting
    cannot disagree. Offline nodes may use an explicit configured class or role.
    Unknown/unclassified nodes never default to committable.
    """
    role = result.get("role")
    if result.get("online"):
        if role in {"primary", "secondary"}:
            return "committable"
        if role == "edge":
            return "best-effort"
        return "unknown"

    configured_class = result.get("configured_class")
    if configured_class in {"committable", "best-effort"}:
        return configured_class

    configured_role = result.get("configured_role")
    if configured_role in {"primary", "secondary"}:
        return "committable"
    if configured_role == "edge":
        return "best-effort"
    return "unknown"

def aggregate() -> dict:
    nodes = discover_nodes()
    if not nodes:
        return {"available": False, "accounting_status": "no-nodes", "network": {"free_sellable_gb": None}, "nodes": []}

    with ThreadPoolExecutor(max_workers=min(max(len(nodes), 1), 16)) as pool:
        results = list(pool.map(_fetch_summary, nodes))

    # Deduplicate by stable node_id; duplicates are visible but only first counts.
    seen = set()
    counted = []
    for r in results:
        if not r.get("online"):
            continue
        nid = r.get("node_id")
        if nid in seen:
            r["duplicate"] = True
            r["online"] = False
            r["error"] = "duplicate node_id"
            continue
        seen.add(nid); counted.append(r)

    primary_nodes = [r for r in counted if r.get("role") == "primary"]
    primary = primary_nodes[0] if len(primary_nodes) == 1 else None

    committable = [r for r in counted if r.get("role") in {"primary", "secondary"}]
    edge = [r for r in counted if r.get("role") == "edge"]
    comm_total = sum(float(r.get("capacity_total_gb") or 0) for r in committable)
    comm_used = sum(float(r.get("used_gb") or 0) for r in committable)
    edge_total = sum(float(r.get("capacity_total_gb") or 0) for r in edge)
    edge_used = sum(float(r.get("used_gb") or 0) for r in edge)

    accounting_ok = primary is not None
    if accounting_ok:
        reserved = float(primary.get("reserved_stakeholders_gb") or 0)
        sold = float(primary.get("sold_active_gb") or 0)
        stakeholders = int(primary.get("stakeholders") or 0)
        commitment_headroom = comm_total - reserved - sold
        physical_headroom = comm_total - comm_used
        free_sellable = max(0.0, min(commitment_headroom, physical_headroom))
        oversubscribed = commitment_headroom < 0 or physical_headroom < 0
        status = "ok"
    else:
        reserved = sold = 0.0; stakeholders = 0; free_sellable = None; oversubscribed = None
        status = "primary-unavailable" if not primary_nodes else "multiple-primary"

    online_count = sum(1 for r in results if r.get("online"))
    return {
        "available": accounting_ok,
        "accounting_status": status,
        "timestamp": int(time.time()),
        "network": {
            "storage_nodes_total": len(nodes),
            "storage_nodes_online": online_count,
            "storage_nodes_offline": len(nodes) - online_count,
            "committable_nodes_online": len(committable),
            "edge_nodes_online": len(edge),
            "capacity_committable_gb": round(comm_total, 2),
            "capacity_committable_tb": round(comm_total / 1024, 3),
            "used_committable_gb": round(comm_used, 2),
            "capacity_best_effort_gb": round(edge_total, 2),
            "capacity_best_effort_tb": round(edge_total / 1024, 3),
            "used_best_effort_gb": round(edge_used, 2),
            "reserved_stakeholders_gb": round(reserved, 2) if accounting_ok else None,
            "sold_active_gb": round(sold, 2) if accounting_ok else None,
            "free_sellable_gb": round(free_sellable, 2) if free_sellable is not None else None,
            "stakeholders": stakeholders if accounting_ok else None,
            "oversubscribed": oversubscribed,
        },
        "nodes": [{
            "name": r.get("name"), "node_id": r.get("node_id"), "role": r.get("role", r.get("configured_role") or "?"),
            "capacity_class": _capacity_class(r),
            "online": r.get("online", False), "capacity_gb": r.get("capacity_total_gb"),
            "used_gb": r.get("used_gb"), "source": r.get("capacity_source"), "error": r.get("error")
        } for r in results],
    }

if __name__ == "__main__":
    print(json.dumps(aggregate(), indent=2))
