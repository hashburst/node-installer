#!/usr/bin/env python3
"""HashBurst replication placement policy.

IMPLEMENTED:
- N total replicas, M committable replicas.
- Deterministic candidate scoring.
- Capacity filtering with safety margin and controller reservations.
- Edge nodes are eligible only for best-effort copies.
- Unknown nodes are never eligible.

TODO:
- Failure-domain awareness (provider/region/ASN/rack).
- Historical reliability scoring from a persisted SLO window.
- Erasure coding / coding-aware placement.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable


COMMITTABLE = "committable"
BEST_EFFORT = "best-effort"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class PlacementPolicy:
    replication_n: int = 3
    committable_m: int = 2
    prefer_edge_for_extra: bool = True
    safety_margin_bytes: int = 512 * 1024 * 1024

    def validate(self) -> None:
        if self.replication_n < 1:
            raise ValueError("replication_n must be >= 1")
        if self.committable_m < 0:
            raise ValueError("committable_m must be >= 0")
        if self.committable_m > self.replication_n:
            raise ValueError("committable_m cannot exceed replication_n")


def stable_tiebreak(cid: str, node_id: str) -> int:
    digest = hashlib.sha256((cid + "\0" + node_id).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def effective_free_bytes(node: dict, controller_reserved_bytes: int, safety_margin_bytes: int) -> int:
    total = int(node.get("total_bytes") or 0)
    used = int(node.get("used_bytes") or 0)
    if total <= 0:
        return 0
    free = max(0, total - used - max(0, controller_reserved_bytes) - max(0, safety_margin_bytes))
    return free


def node_score(cid: str, node: dict, controller_reserved_bytes: int, safety_margin_bytes: int,
               desired_replica_count: int = 0, assigned_bytes: int = 0) -> tuple:
    """Return a deterministic sort key; higher tuple values are preferred."""
    total = max(1, int(node.get("total_bytes") or 0))
    free = effective_free_bytes(node, controller_reserved_bytes, safety_margin_bytes)
    free_ratio_ppm = int((free * 1_000_000) / total)
    stability = int(node.get("stability_score") or 0)
    failure_penalty = int(node.get("failure_penalty") or 0)
    load_penalty = desired_replica_count * 1000 + int(assigned_bytes / (1024 * 1024))
    return (
        free_ratio_ppm,
        stability,
        -failure_penalty,
        -load_penalty,
        -stable_tiebreak(cid, str(node.get("node_id") or "")),
    )


def _eligible(node: dict, object_size: int, reserved_bytes: int, policy: PlacementPolicy) -> bool:
    if not node.get("enabled", True):
        return False
    if not node.get("online", False):
        return False
    if node.get("capacity_class") not in {COMMITTABLE, BEST_EFFORT}:
        return False
    return effective_free_bytes(node, reserved_bytes, policy.safety_margin_bytes) >= object_size


def choose_placements(
    cid: str,
    object_size: int,
    nodes: Iterable[dict],
    current_node_ids: set[str],
    current_committable_count: int,
    current_total_count: int,
    reserved_by_node: dict[str, int],
    desired_count_by_node: dict[str, int],
    assigned_bytes_by_node: dict[str, int],
    policy: PlacementPolicy,
) -> list[dict]:
    """Choose additional nodes needed to satisfy policy.

    The function is pure: it does not mutate state and does not claim success.
    Returned nodes are candidates for desired assignments only; they are not
    counted as confirmed replicas until an agent reports a verified local pin.
    """
    policy.validate()
    chosen: list[dict] = []
    excluded = set(current_node_ids)

    candidates = []
    for node in nodes:
        nid = str(node.get("node_id") or "")
        if not nid or nid in excluded:
            continue
        reserved = int(reserved_by_node.get(nid, 0))
        if not _eligible(node, object_size, reserved, policy):
            continue
        score = node_score(
            cid,
            node,
            reserved,
            policy.safety_margin_bytes,
            int(desired_count_by_node.get(nid, 0)),
            int(assigned_bytes_by_node.get(nid, 0)),
        )
        candidates.append((score, node))

    committable = sorted(
        [x for x in candidates if x[1].get("capacity_class") == COMMITTABLE],
        key=lambda x: x[0], reverse=True,
    )
    edge = sorted(
        [x for x in candidates if x[1].get("capacity_class") == BEST_EFFORT],
        key=lambda x: x[0], reverse=True,
    )

    need_committable = max(0, policy.committable_m - current_committable_count)
    need_total = max(0, policy.replication_n - current_total_count)

    while need_committable > 0 and committable:
        _, node = committable.pop(0)
        chosen.append(node)
        excluded.add(str(node["node_id"]))
        need_committable -= 1
        need_total = max(0, need_total - 1)

    if need_total <= 0:
        return chosen

    if policy.prefer_edge_for_extra:
        pools = [edge, committable]
    else:
        pools = [committable, edge]

    for pool in pools:
        while need_total > 0 and pool:
            _, node = pool.pop(0)
            nid = str(node["node_id"])
            if nid in excluded:
                continue
            chosen.append(node)
            excluded.add(nid)
            need_total -= 1
        if need_total <= 0:
            break

    return chosen
