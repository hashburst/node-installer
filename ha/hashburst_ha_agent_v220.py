#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import hashburst_ha_agent as base

LOG = logging.getLogger("hashburst-ha-v220")


class EligibilityChecker(base.EligibilityChecker):
    def __init__(self, config: base.Config, raw_config: dict[str, Any]):
        super().__init__(config)
        self.raw_config = raw_config
        self.monero_checks = tuple(
            item for item in raw_config.get("monero_checks", []) if isinstance(item, dict)
        )

    @staticmethod
    def _loopback_http_url(url: str) -> bool:
        try:
            parsed = urllib.parse.urlsplit(url)
        except Exception:
            return False
        return (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
            and parsed.username is None
            and parsed.password is None
        )

    @staticmethod
    def _json_request(url: str, payload: dict[str, Any] | None, timeout: float = 2.0) -> Any:
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
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(262145)
        if len(raw) > 262144:
            raise ValueError("response_too_large")
        return json.loads(raw.decode("utf-8"))

    def _check_monero(self, reasons: list[str]) -> None:
        for item in self.monero_checks:
            name = str(item.get("name") or "monero").strip()
            url = str(item.get("url") or "").strip()
            expected_nettype = str(item.get("nettype") or "").strip()
            if not self._loopback_http_url(url):
                reasons.append(f"monero_bad_url:{name}")
                continue
            try:
                data = self._json_request(
                    url,
                    {"jsonrpc": "2.0", "id": "hashburst-ha", "method": "get_info"},
                    timeout=float(item.get("timeout_seconds", 2.0)),
                )
                result = data.get("result") if isinstance(data, dict) else None
                if not isinstance(result, dict):
                    raise ValueError("missing_result")
                nettype = str(result.get("nettype") or "").strip()
                if expected_nettype and nettype != expected_nettype:
                    reasons.append(f"monero_nettype:{name}:{nettype or 'missing'}")
                if result.get("synchronized") is not True:
                    reasons.append(f"monero_not_synchronized:{name}")
                if result.get("busy_syncing") is True:
                    reasons.append(f"monero_busy_syncing:{name}")
                if result.get("offline") is True:
                    reasons.append(f"monero_offline:{name}")
                height = int(result.get("height") or 0)
                target = int(result.get("target_height") or 0)
                max_height_lag = max(0, int(item.get("max_height_lag", 2)))
                if target > 0 and height + max_height_lag < target:
                    reasons.append(f"monero_height_lag:{name}:{target - height}")
            except Exception:
                reasons.append(f"monero_unreachable:{name}")

    def _check_replication(self, reasons: list[str]) -> None:
        if not self.config.replication_state_file:
            return
        path = Path(self.config.replication_state_file)
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                raise ValueError("state_not_object")
            ready = bool(data.get("ready", False))
            updated_at = float(data.get("updated_at", 0.0))
            age = max(0.0, time.time() - updated_at)
            max_lag = self.config.max_replication_lag_seconds
            mode = str(data.get("mode") or "continuous").strip().lower()
            if not ready:
                reasons.append("replication_not_ready")
            if updated_at <= 0 or age > max(max_lag * 2.0, 30.0):
                reasons.append(f"replication_state_stale:{age:.3f}")
            if mode == "continuous":
                lag = float(data.get("lag_seconds", 10**9))
                if lag > max_lag:
                    reasons.append(f"replication_lag:{lag:.3f}")
            elif mode != "reconstructable":
                reasons.append(f"replication_mode_invalid:{mode or 'missing'}")
        except Exception:
            reasons.append("replication_state_unavailable")

    def check(self) -> tuple[bool, list[str]]:
        if not self.config.is_candidate:
            return False, ["not_candidate"]
        reasons: list[str] = []
        for service in self.config.required_services:
            if not self._service_active(service):
                reasons.append(f"service_inactive:{service}")
        for url in self.config.health_urls:
            try:
                if not self._loopback_http_url(url):
                    reasons.append(f"health_bad_url:{url}")
                    continue
                with urllib.request.urlopen(url, timeout=1.0) as response:
                    if int(getattr(response, "status", 200)) // 100 != 2:
                        reasons.append(f"health_http:{url}")
            except Exception:
                reasons.append(f"health_unreachable:{url}")
        self._check_monero(reasons)
        self._check_replication(reasons)
        return not reasons, reasons


class _CachingEligibility:
    def __init__(self, delegate, publish):
        self.delegate = delegate
        self.publish = publish

    def check(self) -> tuple[bool, list[str]]:
        eligible, reasons = self.delegate.check()
        reasons = list(reasons)
        self.publish(bool(eligible), reasons)
        return bool(eligible), reasons


class LeaseEngine(base.LeaseEngine):
    def __init__(self, config: base.Config, *, raw_config: dict[str, Any], **kwargs):
        eligibility = kwargs.pop("eligibility", None) or EligibilityChecker(config, raw_config)
        super().__init__(config, eligibility=eligibility, **kwargs)
        self._eligibility_state_lock = threading.Lock()
        self._eligibility_state = (
            (False, ["eligibility_not_checked"])
            if self.config.is_candidate
            else (False, ["not_candidate"])
        )
        self._eligibility_delegate = self.eligibility
        self.eligibility = _CachingEligibility(self._eligibility_delegate, self._publish_eligibility)
        term, voted_for = self.vote_state.snapshot()
        self._restart_guard_until = (
            base.boot_seconds() + self.config.lease_seconds
            if self.config.is_voter and term > 0 and voted_for
            else 0.0
        )

    def _publish_eligibility(self, eligible: bool, reasons: list[str]) -> None:
        with self._eligibility_state_lock:
            self._eligibility_state = (bool(eligible), list(reasons))

    def _cached_eligibility(self) -> tuple[bool, list[str]]:
        with self._eligibility_state_lock:
            eligible, reasons = self._eligibility_state
            return bool(eligible), list(reasons)

    def local_status(self) -> dict[str, Any]:
        eligible, reasons = self._cached_eligibility()
        with self._lock:
            status = self._voter_status()
            status.update({
                "roles": sorted(self.config.roles),
                "candidate_priority": self.config.priority if self.config.is_candidate else None,
                "eligible": eligible,
                "eligibility_reasons": reasons,
                "armed": self.config.armed,
                "local_role": "primary" if self._leader_deadline > base.boot_seconds() else self.controller.desired,
                "leader_term": self._leader_term,
                "leader_remaining_ms": max(
                    0, int((self._leader_deadline - base.boot_seconds()) * 1000)
                ),
                "cluster_view": self._cluster_view,
                "last_error": self._last_error,
            })
            return status

    def handle_tep(self, source: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        if isinstance(payload, dict) and str(payload.get("op") or "") == "vote_request":
            source_node = str(source.get("node_id") or "").strip() if isinstance(source, dict) else ""
            current_term, voted_for = self.vote_state.snapshot()
            if (
                self.config.is_voter
                and self._restart_guard_until > base.boot_seconds()
                and voted_for
                and source_node
                and source_node != voted_for
            ):
                return {
                    "granted": False,
                    "term": current_term,
                    "holder": voted_for,
                    "lease_remaining_ms": max(
                        0, int((self._restart_guard_until - base.boot_seconds()) * 1000)
                    ),
                    "reason": "restart_lease_guard",
                }
        return super().handle_tep(source, payload)

    def _renew(self) -> bool:
        eligible, reasons = self.eligibility.check()
        if not eligible:
            self._demote("ineligible:" + ",".join(reasons))
            return False

        started = base.boot_seconds()
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
        conservative_deadline = started + self.config.lease_seconds
        if grants >= self.config.quorum and conservative_deadline > base.boot_seconds():
            self._leader_deadline = conservative_deadline
            self._write_guard(self._leader_term)
            self.controller.set_primary()
            return True
        return False

    def _campaign(self, term: int) -> bool:
        started = base.boot_seconds()
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
        conservative_deadline = started + self.config.lease_seconds
        if grants >= self.config.quorum and conservative_deadline > base.boot_seconds():
            self._leader_term = term
            self._leader_deadline = conservative_deadline
            self._write_guard(term)
            self.controller.set_primary()
            LOG.warning("HA PRIMARY acquired term=%d grants=%d/%d", term, grants, len(self.config.voters))
            return True
        if highest_term > term:
            self._leader_term = highest_term
        return False


class AgentHttpServer:
    def __init__(self, engine: LeaseEngine):
        self.engine = engine
        self.server: ThreadingHTTPServer | None = None

    def start(self) -> ThreadingHTTPServer:
        engine = self.engine

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _send(self, status: int, payload: dict[str, Any]) -> bool:
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return True
                except (BrokenPipeError, ConnectionResetError):
                    LOG.debug("HA local HTTP client disconnected before response completed")
                    return False

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
                        raise base.HaError("bad_request", "invalid TEP forwarding envelope")
                    result = engine.handle_tep(source, payload)
                    self._send(200, {"ok": True, "result": result})
                except base.HaError as exc:
                    self._send(400, {"ok": False, "error": {"code": exc.code}})
                except (BrokenPipeError, ConnectionResetError):
                    LOG.debug("HA local HTTP client disconnected while request was completing")
                except Exception:
                    LOG.exception("Local HA TEP handler failed")
                    self._send(500, {"ok": False, "error": {"code": "internal_error"}})

        self.server = ThreadingHTTPServer((engine.config.bind_host, engine.config.bind_port), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="ha-http")
        thread.start()
        return self.server


def watchdog(config: base.Config) -> None:
    if not config.is_candidate or not config.armed:
        while True:
            time.sleep(60)
    controller = base.ServiceController(config)
    last_fence = 0.0
    while True:
        now = base.boot_seconds()
        try:
            guard = json.loads(config.guard_file.read_text())
            valid = (
                str(guard.get("cluster_id") or "") == config.cluster_id
                and str(guard.get("holder") or "") == config.node_id
                and float(guard.get("boot_deadline", 0.0)) > now
            )
        except Exception:
            valid = False
        if not valid and now - last_fence >= 1.0:
            try:
                controller.set_standby()
            except Exception as exc:
                LOG.error("Watchdog fencing failed: %s", exc)
            last_fence = now
        time.sleep(0.5)


def main() -> None:
    parser = argparse.ArgumentParser(description="HashBurst TEP-HA v2.2 hardened agent")
    parser.add_argument("--config", default=str(base.DEFAULT_CONFIG))
    parser.add_argument("--watchdog", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config_path = Path(args.config)
    raw_config = json.loads(config_path.read_text())
    config = base.Config.load(config_path)
    if args.watchdog:
        watchdog(config)
        return
    transport = base.TepIpcClient(config)
    base.validate_local_tep_identity(config, transport)
    engine = LeaseEngine(config, raw_config=raw_config, transport=transport)
    AgentHttpServer(engine).start()
    if args.check:
        print(json.dumps(engine.local_status(), indent=2, sort_keys=True))
        return
    LOG.warning(
        "TEP-HA v2.2 started node=%s roles=%s armed=%s voters=%d quorum=%d",
        config.node_id, ",".join(sorted(config.roles)), config.armed, len(config.voters), config.quorum,
    )
    engine.run()


if __name__ == "__main__":
    main()
