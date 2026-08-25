#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

LOG = logging.getLogger("hashburst-ha")
DEFAULT_CONFIG = Path("/etc/hashburst/ha.json")
DEFAULT_STATE = Path("/var/lib/hashburst/ha/state.json")
DEFAULT_GUARD = Path("/run/hashburst-ha/lease.json")
DEFAULT_TEP_STATUS = "http://127.0.0.1:47778/"
DEFAULT_TEP_RPC = "http://127.0.0.1:47781/app/ha-lease"
DEFAULT_AGENT_BIND = "127.0.0.1"
DEFAULT_AGENT_PORT = 47780


class HaError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Candidate:
    node_id: str
    priority: int


@dataclass(frozen=True)
class Config:
    cluster_id: str
    node_id: str
    roles: frozenset[str]
    voters: tuple[str, ...]
    candidates: tuple[Candidate, ...]
    lease_seconds: float
    loop_seconds: float
    rpc_timeout_seconds: float
    armed: bool
    primary_services: tuple[str, ...]
    required_services: tuple[str, ...]
    health_urls: tuple[str, ...]
    replication_state_file: str
    max_replication_lag_seconds: float
    state_file: Path
    guard_file: Path
    startup_grace_seconds: float
    tep_status_url: str
    tep_rpc_url: str
    bind_host: str
    bind_port: int

    @property
    def is_voter(self) -> bool:
        return "voter" in self.roles

    @property
    def is_candidate(self) -> bool:
        return "candidate" in self.roles

    @property
    def quorum(self) -> int:
        return len(self.voters) // 2 + 1

    @property
    def priority(self) -> int:
        for candidate in self.candidates:
            if candidate.node_id == self.node_id:
                return candidate.priority
        return 2**31 - 1

    @classmethod
    def load(cls, path: Path) -> "Config":
        raw = json.loads(path.read_text())
        cluster_id = str(raw.get("cluster_id") or "").strip()
        node_id = str(raw.get("node_id") or "").strip()
        roles = frozenset(str(x).strip() for x in raw.get("roles", []) if str(x).strip())
        voters = tuple(dict.fromkeys(str(x).strip() for x in raw.get("voters", []) if str(x).strip()))
        candidates = tuple(
            Candidate(str(item.get("node_id") or "").strip(), int(item.get("priority", 100)))
            for item in raw.get("candidates", [])
            if isinstance(item, dict) and str(item.get("node_id") or "").strip()
        )
        if not cluster_id or not node_id:
            raise ValueError("cluster_id and node_id are required")
        if not voters:
            raise ValueError("at least one voter is required")
        if len(set(voters)) != len(voters):
            raise ValueError("voters must be unique")
        if roles - {"voter", "candidate", "observer"}:
            raise ValueError("roles may contain voter, candidate, observer only")
        candidate_ids = [c.node_id for c in candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate node_id values must be unique")
        if "candidate" in roles and node_id not in candidate_ids:
            raise ValueError("candidate node must be present in candidates")
        lease_seconds = float(raw.get("lease_seconds", 12.0))
        loop_seconds = float(raw.get("loop_seconds", 2.0))
        rpc_timeout_seconds = float(raw.get("rpc_timeout_seconds", 2.5))
        if not (6.0 <= lease_seconds <= 120.0):
            raise ValueError("lease_seconds must be in 6..120")
        if not (0.5 <= loop_seconds < lease_seconds / 2):
            raise ValueError("loop_seconds must be >= 0.5 and < lease_seconds/2")
        if not (0.5 <= rpc_timeout_seconds <= 5.0):
            raise ValueError("rpc_timeout_seconds must be in 0.5..5")
        health_urls = tuple(str(x).strip() for x in raw.get("health_urls", []) if str(x).strip())
        for url in health_urls:
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
                raise ValueError("health_urls must use loopback HTTP only")
        return cls(
            cluster_id=cluster_id,
            node_id=node_id,
            roles=roles,
            voters=voters,
            candidates=candidates,
            lease_seconds=lease_seconds,
            loop_seconds=loop_seconds,
            rpc_timeout_seconds=rpc_timeout_seconds,
            armed=bool(raw.get("armed", False)),
            primary_services=tuple(str(x).strip() for x in raw.get("primary_services", []) if str(x).strip()),
            required_services=tuple(str(x).strip() for x in raw.get("required_services", []) if str(x).strip()),
            health_urls=health_urls,
            replication_state_file=str(raw.get("replication_state_file") or "").strip(),
            max_replication_lag_seconds=float(raw.get("max_replication_lag_seconds", 30.0)),
            state_file=Path(str(raw.get("state_file") or DEFAULT_STATE)),
            guard_file=Path(str(raw.get("guard_file") or DEFAULT_GUARD)),
            startup_grace_seconds=float(raw.get("startup_grace_seconds", 30.0)),
            tep_status_url=str(raw.get("tep_status_url") or DEFAULT_TEP_STATUS),
            tep_rpc_url=str(raw.get("tep_rpc_url") or DEFAULT_TEP_RPC),
            bind_host=str(raw.get("bind_host") or DEFAULT_AGENT_BIND),
            bind_port=int(raw.get("bind_port", DEFAULT_AGENT_PORT)),
        )


def boot_seconds() -> float:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except Exception:
        return time.monotonic()


def atomic_json_write(path: Path, payload: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    os.chmod(temp, mode)
    os.replace(temp, path)


class PersistentVoteState:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.term = 0
        self.voted_for = ""
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
            self.term = max(0, int(data.get("term", 0)))
            self.voted_for = str(data.get("voted_for") or "")
        except FileNotFoundError:
            return
        except Exception as exc:
            raise RuntimeError(f"invalid HA state file {self.path}: {exc}") from exc

    def snapshot(self) -> tuple[int, str]:
        with self._lock:
            return self.term, self.voted_for

    def advance(self, term: int, voted_for: str) -> None:
        with self._lock:
            if term < self.term:
                raise ValueError("term regression")
            self.term = int(term)
            self.voted_for = str(voted_for)
            atomic_json_write(self.path, {"term": self.term, "voted_for": self.voted_for})


class ServiceController:
    def __init__(self, config: Config):
        self.config = config
        self._lock = threading.Lock()
        self._desired = "standby"

    def _is_active(self, service: str) -> bool:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", service],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

    def _systemctl(self, action: str, service: str) -> None:
        result = subprocess.run(
            ["systemctl", action, service],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[:300]
            raise HaError("service_control_failed", f"systemctl {action} {service}: {detail}")

    def set_primary(self) -> None:
        with self._lock:
            self._desired = "primary"
            if not self.config.is_candidate or not self.config.armed:
                return
            for service in self.config.primary_services:
                if not self._is_active(service):
                    LOG.warning("Starting primary-only service %s", service)
                    self._systemctl("start", service)

    def set_standby(self) -> None:
        with self._lock:
            self._desired = "standby"
            if not self.config.is_candidate or not self.config.armed:
                return
            for service in reversed(self.config.primary_services):
                if self._is_active(service):
                    LOG.warning("Fencing primary-only service %s", service)
                    self._systemctl("stop", service)

    @property
    def desired(self) -> str:
        return self._desired


class EligibilityChecker:
    def __init__(self, config: Config):
        self.config = config

    def _service_active(self, service: str) -> bool:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", service],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

    def check(self) -> tuple[bool, list[str]]:
        if not self.config.is_candidate:
            return False, ["not_candidate"]
        reasons: list[str] = []
        for service in self.config.required_services:
            if not self._service_active(service):
                reasons.append(f"service_inactive:{service}")
        for url in self.config.health_urls:
            try:
                with urllib.request.urlopen(url, timeout=1.0) as response:
                    if int(getattr(response, "status", 200)) // 100 != 2:
                        reasons.append(f"health_http:{url}")
            except Exception:
                reasons.append(f"health_unreachable:{url}")
        if self.config.replication_state_file:
            path = Path(self.config.replication_state_file)
            try:
                data = json.loads(path.read_text())
                ready = bool(data.get("ready", False))
                lag = float(data.get("lag_seconds", 10**9))
                updated_at = float(data.get("updated_at", 0.0))
                age = max(0.0, time.time() - updated_at)
                max_lag = self.config.max_replication_lag_seconds
                if not ready:
                    reasons.append("replication_not_ready")
                if lag > max_lag:
                    reasons.append(f"replication_lag:{lag:.3f}")
                if updated_at <= 0 or age > max(max_lag * 2.0, 30.0):
                    reasons.append(f"replication_state_stale:{age:.3f}")
            except Exception:
                reasons.append("replication_state_unavailable")
        return not reasons, reasons


class TepIpcClient:
    def __init__(self, config: Config):
        self.config = config

    @staticmethod
    def _json_request(url: str, payload: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        if payload is None:
            request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
        else:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            request = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(65537)
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read(65537).decode("utf-8"))
                code = str((data.get("error") or {}).get("code") or "tep_http_error")
            except Exception:
                code = "tep_http_error"
            raise HaError(code, f"TEP IPC HTTP {exc.code}") from exc
        except Exception as exc:
            raise HaError("tep_unavailable", "local TEP IPC unavailable") from exc
        if len(raw) > 65536:
            raise HaError("tep_response_too_large", "TEP IPC response too large")
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise HaError("tep_bad_response", "invalid TEP IPC JSON") from exc
        if not isinstance(data, dict):
            raise HaError("tep_bad_response", "TEP IPC response must be an object")
        return data

    def status(self) -> dict[str, Any]:
        return self._json_request(self.config.tep_status_url, None, 1.5)

    def resolve(self, node_id: str) -> tuple[str, str]:
        status = self.status()
        if node_id == self.config.node_id:
            peer_id = str(status.get("peer_id") or status.get("identity", {}).get("peer_id") or "").strip()
            if not peer_id:
                peer_id = str(os.environ.get("HB_TEP_PEER_ID") or "").strip()
            if not peer_id:
                raise HaError("tep_identity_missing", "local TEP peer_id unavailable")
            return node_id, peer_id
        for peer in status.get("peers") or []:
            if isinstance(peer, dict) and str(peer.get("id") or "") == node_id:
                peer_id = str(peer.get("peer_id") or "").strip()
                if not peer_id:
                    raise HaError("tep_identity_missing", f"TEP peer_id missing for {node_id}")
                return node_id, peer_id
        raise HaError("tep_peer_unknown", f"TEP peer not found: {node_id}")

    def rpc(self, node_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        destination_node, peer_id = self.resolve(node_id)
        response = self._json_request(
            self.config.tep_rpc_url,
            {"node_id": destination_node, "peer_id": peer_id, "payload": payload},
            self.config.rpc_timeout_seconds + 1.0,
        )
        if not bool(response.get("ok", False)):
            error = response.get("error") or {}
            raise HaError(str(error.get("code") or "tep_rpc_failed"), "TEP HA RPC failed")
        result = response.get("result")
        if not isinstance(result, dict):
            raise HaError("tep_bad_response", "TEP HA result must be an object")
        return result


class LeaseEngine:
    def __init__(self, config: Config, *, transport: TepIpcClient | None = None,
                 controller: ServiceController | None = None,
                 eligibility: EligibilityChecker | None = None):
        self.config = config
        self.vote_state = PersistentVoteState(config.state_file)
        self.transport = transport or TepIpcClient(config)
        self.controller = controller or ServiceController(config)
        self.eligibility = eligibility or EligibilityChecker(config)
        self._lock = threading.RLock()
        self._voter_holder = ""
        self._voter_deadline = 0.0
        self._leader_term = 0
        self._leader_deadline = 0.0
        self._cluster_view: dict[str, Any] = {}
        self._last_error = ""
        self._started_boot = boot_seconds()

    def _lease_remaining_ms(self) -> int:
        return max(0, int((self._voter_deadline - boot_seconds()) * 1000))

    def _voter_status(self) -> dict[str, Any]:
        term, voted_for = self.vote_state.snapshot()
        if self._voter_deadline <= boot_seconds():
            holder = ""
            remaining = 0
        else:
            holder = self._voter_holder
            remaining = self._lease_remaining_ms()
        return {
            "cluster_id": self.config.cluster_id,
            "node_id": self.config.node_id,
            "term": term,
            "voted_for": voted_for,
            "holder": holder,
            "lease_remaining_ms": remaining,
            "quorum": self.config.quorum,
        }

    def local_status(self) -> dict[str, Any]:
        eligible, reasons = self.eligibility.check() if self.config.is_candidate else (False, ["not_candidate"])
        with self._lock:
            status = self._voter_status()
            status.update({
                "roles": sorted(self.config.roles),
                "candidate_priority": self.config.priority if self.config.is_candidate else None,
                "eligible": eligible,
                "eligibility_reasons": reasons,
                "armed": self.config.armed,
                "local_role": "primary" if self._leader_deadline > boot_seconds() else self.controller.desired,
                "leader_term": self._leader_term,
                "leader_remaining_ms": max(0, int((self._leader_deadline - boot_seconds()) * 1000)),
                "cluster_view": self._cluster_view,
                "last_error": self._last_error,
            })
            return status

    def _validate_cluster(self, payload: dict[str, Any]) -> None:
        if str(payload.get("cluster_id") or "") != self.config.cluster_id:
            raise HaError("cluster_mismatch", "HA cluster_id mismatch")

    def handle_tep(self, source: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(source, dict) or not isinstance(payload, dict):
            raise HaError("bad_request", "source and payload must be objects")
        source_node = str(source.get("node_id") or "").strip()
        source_peer = str(source.get("peer_id") or "").strip()
        if not source_node or not source_peer:
            raise HaError("bad_request", "authenticated TEP source identity is required")
        self._validate_cluster(payload)
        op = str(payload.get("op") or "").strip()
        if op == "status":
            return self.local_status()
        if not self.config.is_voter:
            raise HaError("not_voter", "node does not grant HA leases")
        if source_node not in {c.node_id for c in self.config.candidates}:
            raise HaError("candidate_unknown", "source is not an allowed HA candidate")
        if op == "vote_request":
            candidate = str(payload.get("candidate") or "")
            term = int(payload.get("term", -1))
            lease_ms = int(payload.get("lease_ms", int(self.config.lease_seconds * 1000)))
            if candidate != source_node:
                raise HaError("identity_mismatch", "candidate must equal authenticated TEP source")
            if lease_ms <= 0 or lease_ms > int(self.config.lease_seconds * 1000):
                raise HaError("bad_lease", "invalid requested lease duration")
            with self._lock:
                current_term, voted_for = self.vote_state.snapshot()
                if term < current_term:
                    return {"granted": False, "term": current_term, "reason": "stale_term"}
                if term > current_term:
                    self.vote_state.advance(term, candidate)
                    voted_for = candidate
                elif voted_for not in {"", candidate}:
                    return {"granted": False, "term": current_term, "reason": "already_voted"}
                elif not voted_for:
                    self.vote_state.advance(term, candidate)
                self._voter_holder = candidate
                self._voter_deadline = boot_seconds() + lease_ms / 1000.0
                return {"granted": True, "term": term, "holder": candidate, "lease_ms": lease_ms}
        if op == "renew":
            holder = str(payload.get("holder") or "")
            term = int(payload.get("term", -1))
            lease_ms = int(payload.get("lease_ms", int(self.config.lease_seconds * 1000)))
            if holder != source_node:
                raise HaError("identity_mismatch", "holder must equal authenticated TEP source")
            with self._lock:
                current_term, voted_for = self.vote_state.snapshot()
                if term != current_term or voted_for != holder:
                    return {"granted": False, "term": current_term, "reason": "not_current_holder"}
                if lease_ms <= 0 or lease_ms > int(self.config.lease_seconds * 1000):
                    raise HaError("bad_lease", "invalid requested lease duration")
                self._voter_holder = holder
                self._voter_deadline = boot_seconds() + lease_ms / 1000.0
                return {"granted": True, "term": term, "holder": holder, "lease_ms": lease_ms}
        raise HaError("unsupported_op", f"unsupported HA operation: {op or '<missing>'}")

    def _local_rpc(self, payload: dict[str, Any]) -> dict[str, Any]:
        source = {"node_id": self.config.node_id, "peer_id": "local-self"}
        return self.handle_tep(source, payload)

    def _rpc(self, target: str, payload: dict[str, Any]) -> dict[str, Any]:
        if target == self.config.node_id:
            return self._local_rpc(payload)
        return self.transport.rpc(target, payload)

    def _query_voters(self) -> list[tuple[str, dict[str, Any]]]:
        payload = {"op": "status", "cluster_id": self.config.cluster_id}
        responses: list[tuple[str, dict[str, Any]]] = []
        for voter in self.config.voters:
            try:
                responses.append((voter, self._rpc(voter, payload)))
            except Exception as exc:
                LOG.debug("Voter %s unavailable: %s", voter, exc)
        return responses

    def _majority_view(self, responses: list[tuple[str, dict[str, Any]]]) -> tuple[int, str, int]:
        max_term = 0
        counts: dict[tuple[int, str], int] = {}
        for _, status in responses:
            try:
                term = int(status.get("term", 0))
            except Exception:
                continue
            max_term = max(max_term, term)
            holder = str(status.get("holder") or "")
            remaining = int(status.get("lease_remaining_ms", 0) or 0)
            if holder and remaining > 0:
                counts[(term, holder)] = counts.get((term, holder), 0) + 1
        if not counts:
            return max_term, "", 0
        (term, holder), count = max(counts.items(), key=lambda item: (item[1], item[0][0]))
        if count >= self.config.quorum:
            return max_term, holder, term
        return max_term, "", 0

    def _higher_priority_candidate_ready(self) -> bool:
        if not self.config.is_candidate:
            return False
        for candidate in sorted(self.config.candidates, key=lambda c: (c.priority, c.node_id)):
            if candidate.priority >= self.config.priority:
                continue
            if candidate.node_id == self.config.node_id:
                continue
            try:
                status = self._rpc(candidate.node_id, {"op": "status", "cluster_id": self.config.cluster_id})
            except Exception:
                continue
            if bool(status.get("eligible", False)):
                return True
        return False

    def _write_guard(self, term: int) -> None:
        if not (self.config.is_candidate and self.config.armed):
            return
        atomic_json_write(
            self.config.guard_file,
            {
                "cluster_id": self.config.cluster_id,
                "holder": self.config.node_id,
                "term": term,
                "boot_deadline": self._leader_deadline,
            },
            mode=0o600,
        )

    def _clear_guard(self) -> None:
        try:
            self.config.guard_file.unlink()
        except FileNotFoundError:
            pass

    def _demote(self, reason: str) -> None:
        with self._lock:
            was_primary = self._leader_deadline > boot_seconds()
            self._leader_deadline = 0.0
            self._clear_guard()
        if was_primary:
            LOG.warning("HA demotion: %s", reason)
        try:
            self.controller.set_standby()
        except Exception as exc:
            LOG.error("HA fencing failed: %s", exc)
            self._last_error = f"fence:{exc}"

    def _renew(self) -> bool:
        payload = {
            "op": "renew",
            "cluster_id": self.config.cluster_id,
            "holder": self.config.node_id,
            "term": self._leader_term,
            "lease_ms": int(self.config.lease_seconds * 1000),
        }
        grants = 0
        for voter in self.config.voters:
            try:
                response = self._rpc(voter, payload)
                if bool(response.get("granted", False)) and int(response.get("term", -1)) == self._leader_term:
                    grants += 1
            except Exception as exc:
                LOG.debug("Lease renew via %s failed: %s", voter, exc)
        if grants >= self.config.quorum:
            self._leader_deadline = boot_seconds() + self.config.lease_seconds
            self._write_guard(self._leader_term)
            self.controller.set_primary()
            return True
        return False

    def _campaign(self, term: int) -> bool:
        payload = {
            "op": "vote_request",
            "cluster_id": self.config.cluster_id,
            "candidate": self.config.node_id,
            "priority": self.config.priority,
            "term": term,
            "lease_ms": int(self.config.lease_seconds * 1000),
        }
        grants = 0
        highest_term = term
        for voter in self.config.voters:
            try:
                response = self._rpc(voter, payload)
                highest_term = max(highest_term, int(response.get("term", 0)))
                if bool(response.get("granted", False)) and int(response.get("term", -1)) == term:
                    grants += 1
            except Exception as exc:
                LOG.debug("Vote request via %s failed: %s", voter, exc)
        if grants >= self.config.quorum:
            self._leader_term = term
            self._leader_deadline = boot_seconds() + self.config.lease_seconds
            self._write_guard(term)
            self.controller.set_primary()
            LOG.warning("HA PRIMARY acquired term=%d grants=%d/%d", term, grants, len(self.config.voters))
            return True
        if highest_term > term:
            self._leader_term = highest_term
        return False

    def run_once(self) -> None:
        try:
            responses = self._query_voters()
            max_term, holder, holder_term = self._majority_view(responses)
            self._cluster_view = {
                "voters_reachable": len(responses),
                "voters_total": len(self.config.voters),
                "quorum": self.config.quorum,
                "term": max_term,
                "holder": holder,
                "holder_term": holder_term,
            }
            if holder and holder != self.config.node_id:
                self._leader_term = max(self._leader_term, holder_term)
                self._demote(f"majority_holder:{holder}")
                return
            if self.config.is_candidate and self._leader_deadline > boot_seconds():
                if max_term > self._leader_term:
                    self._demote(f"higher_term:{max_term}")
                    return
                if self._renew():
                    return
                if self._leader_deadline <= boot_seconds():
                    self._demote("lease_expired")
                return
            if not self.config.is_candidate:
                return
            eligible, reasons = self.eligibility.check()
            if not eligible:
                self._demote("ineligible:" + ",".join(reasons))
                return
            if len(responses) < self.config.quorum:
                self._demote("no_voter_quorum")
                return
            if holder == self.config.node_id and holder_term > 0:
                self._leader_term = holder_term
                self._leader_deadline = boot_seconds() + self.config.lease_seconds
                self._renew()
                return
            if self._higher_priority_candidate_ready():
                self._demote("higher_priority_candidate_ready")
                return
            self._campaign(max_term + 1)
            if self._leader_deadline <= boot_seconds():
                self._demote("campaign_no_quorum")
        except Exception as exc:
            self._last_error = f"loop:{type(exc).__name__}:{exc}"
            LOG.exception("HA loop failed")
            if self._leader_deadline <= boot_seconds():
                self._demote("loop_error_without_valid_lease")

    def run(self) -> None:
        while True:
            started = time.monotonic()
            self.run_once()
            remaining = self.config.loop_seconds - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)


class AgentHttpServer:
    def __init__(self, engine: LeaseEngine):
        self.engine = engine
        self.server: ThreadingHTTPServer | None = None

    def start(self) -> ThreadingHTTPServer:
        engine = self.engine

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _send(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path != "/v1/status":
                    self._send(404, {"ok": False, "error": {"code": "not_found"}})
                    return
                self._send(200, {"ok": True, "status": engine.local_status()})

            def do_POST(self):
                if self.path != "/v1/tep":
                    self._send(404, {"ok": False, "error": {"code": "not_found"}})
                    return
                if (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower() != "application/json":
                    self._send(415, {"ok": False, "error": {"code": "unsupported_media_type"}})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or "0")
                except ValueError:
                    length = 0
                if length <= 0 or length > 16384:
                    self._send(413, {"ok": False, "error": {"code": "request_too_large"}})
                    return
                try:
                    data = json.loads(self.rfile.read(length).decode("utf-8"))
                    source = data.get("source")
                    payload = data.get("payload")
                    if not isinstance(data, dict) or not isinstance(source, dict) or not isinstance(payload, dict):
                        raise HaError("bad_request", "invalid TEP forwarding envelope")
                    result = engine.handle_tep(source, payload)
                    self._send(200, {"ok": True, "result": result})
                except HaError as exc:
                    self._send(400, {"ok": False, "error": {"code": exc.code}})
                except Exception:
                    LOG.exception("Local HA TEP handler failed")
                    self._send(500, {"ok": False, "error": {"code": "internal_error"}})

        self.server = ThreadingHTTPServer((engine.config.bind_host, engine.config.bind_port), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="ha-http")
        thread.start()
        return self.server


def watchdog(config: Config) -> None:
    if not config.is_candidate or not config.armed:
        while True:
            time.sleep(60)
    controller = ServiceController(config)
    grace_until = boot_seconds() + config.startup_grace_seconds
    last_fence = 0.0
    while True:
        now = boot_seconds()
        valid = False
        if now < grace_until:
            valid = True
        else:
            try:
                guard = json.loads(config.guard_file.read_text())
                valid = (
                    str(guard.get("cluster_id") or "") == config.cluster_id
                    and str(guard.get("holder") or "") == config.node_id
                    and float(guard.get("boot_deadline", 0.0)) > now
                )
            except Exception:
                valid = False
        if not valid and now - last_fence >= 5.0:
            try:
                controller.set_standby()
            except Exception as exc:
                LOG.error("Watchdog fencing failed: %s", exc)
            last_fence = now
        time.sleep(1.0)


def validate_local_tep_identity(config: Config, transport: TepIpcClient) -> None:
    status = transport.status()
    actual = str(status.get("node_id") or "").strip()
    if actual and actual != config.node_id:
        raise RuntimeError(f"HA node_id {config.node_id!r} does not match local TEP node_id {actual!r}")
    if not bool(status.get("app_ready", False)):
        raise RuntimeError("local TEP APP transport is not ready")
    services = set(status.get("services") or [])
    if "ha.lease" not in services:
        raise RuntimeError("local TEP daemon does not advertise ha.lease")


def main() -> None:
    parser = argparse.ArgumentParser(description="HashBurst TEP-HA distributed lease agent")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--watchdog", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = Config.load(Path(args.config))
    if args.watchdog:
        watchdog(config)
        return
    transport = TepIpcClient(config)
    validate_local_tep_identity(config, transport)
    engine = LeaseEngine(config, transport=transport)
    AgentHttpServer(engine).start()
    if args.check:
        print(json.dumps(engine.local_status(), indent=2, sort_keys=True))
        return
    LOG.warning(
        "TEP-HA started node=%s roles=%s armed=%s voters=%d quorum=%d",
        config.node_id, ",".join(sorted(config.roles)), config.armed, len(config.voters), config.quorum,
    )
    engine.run()


if __name__ == "__main__":
    main()
