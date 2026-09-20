#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

LOG = logging.getLogger("hashburst-ha-readiness")
DEFAULT_CONFIG = Path("/etc/hashburst/ha.json")
DEFAULT_OUTPUT = Path("/var/lib/hashburst/ha/replication.json")


def atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def loopback_url(url: str) -> bool:
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


def json_request(url: str, payload: dict[str, Any] | None = None, timeout: float = 2.0) -> Any:
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


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def check_required_files(config: dict[str, Any], checks: dict[str, Any], errors: list[str]) -> None:
    for item in config.get("readiness_files", []):
        if isinstance(item, str):
            item = {"path": item}
        if not isinstance(item, dict):
            continue
        path_text = str(item.get("path") or "").strip()
        if not path_text:
            continue
        path = Path(path_text)
        name = str(item.get("name") or path_text)
        row: dict[str, Any] = {"path": path_text, "exists": path.exists()}
        if not path.exists():
            errors.append(f"file_missing:{name}")
        elif path.is_file():
            row["size"] = path.stat().st_size
            if bool(item.get("sha256", False)):
                row["sha256"] = file_sha256(path)
        checks[f"file:{name}"] = row


def check_health(config: dict[str, Any], checks: dict[str, Any], errors: list[str]) -> None:
    for url in config.get("health_urls", []):
        url = str(url).strip()
        if not url:
            continue
        key = f"http:{url}"
        if not loopback_url(url):
            checks[key] = {"ok": False, "error": "non_loopback_url"}
            errors.append(f"health_bad_url:{url}")
            continue
        try:
            data = json_request(url, timeout=1.5)
            checks[key] = {"ok": True, "response": data}
        except Exception as exc:
            checks[key] = {"ok": False, "error": type(exc).__name__}
            errors.append(f"health_unreachable:{url}")


def check_monero(config: dict[str, Any], checks: dict[str, Any], errors: list[str]) -> None:
    for item in config.get("monero_checks", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "monero").strip()
        url = str(item.get("url") or "").strip()
        expected_nettype = str(item.get("nettype") or "").strip()
        row: dict[str, Any] = {"url": url, "expected_nettype": expected_nettype}
        if not loopback_url(url):
            row.update(ok=False, error="non_loopback_url")
            checks[f"monero:{name}"] = row
            errors.append(f"monero_bad_url:{name}")
            continue
        try:
            data = json_request(
                url,
                {"jsonrpc": "2.0", "id": "hashburst-ha-readiness", "method": "get_info"},
                timeout=float(item.get("timeout_seconds", 2.0)),
            )
            result = data.get("result") if isinstance(data, dict) else None
            if not isinstance(result, dict):
                raise ValueError("missing_result")
            nettype = str(result.get("nettype") or "")
            height = int(result.get("height") or 0)
            target = int(result.get("target_height") or 0)
            synchronized = result.get("synchronized") is True
            busy = result.get("busy_syncing") is True
            offline = result.get("offline") is True
            max_height_lag = max(0, int(item.get("max_height_lag", 2)))
            height_ok = target <= 0 or height + max_height_lag >= target
            ok = (
                (not expected_nettype or nettype == expected_nettype)
                and synchronized
                and not busy
                and not offline
                and height_ok
            )
            row.update(
                ok=ok,
                nettype=nettype,
                height=height,
                target_height=target,
                synchronized=synchronized,
                busy_syncing=busy,
                offline=offline,
            )
            if not ok:
                errors.append(f"monero_not_ready:{name}")
        except Exception as exc:
            row.update(ok=False, error=type(exc).__name__)
            errors.append(f"monero_unreachable:{name}")
        checks[f"monero:{name}"] = row


def check_continuous_sources(config: dict[str, Any], checks: dict[str, Any], errors: list[str]) -> tuple[str, float | None]:
    sources = [item for item in config.get("replication_sources", []) if isinstance(item, dict)]
    if not sources:
        return "reconstructable", None
    max_lag = 0.0
    now = time.time()
    for item in sources:
        name = str(item.get("name") or "source")
        path = Path(str(item.get("state_file") or ""))
        allowed = float(item.get("max_lag_seconds", config.get("max_replication_lag_seconds", 30.0)))
        row: dict[str, Any] = {"state_file": str(path), "max_lag_seconds": allowed}
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                raise ValueError("state_not_object")
            ready = bool(data.get("ready", False))
            updated_at = float(data.get("updated_at", 0.0))
            explicit_lag = data.get("lag_seconds")
            lag = float(explicit_lag) if explicit_lag is not None else max(0.0, now - updated_at)
            row.update(ready=ready, updated_at=updated_at, lag_seconds=lag)
            max_lag = max(max_lag, lag)
            if not ready:
                errors.append(f"source_not_ready:{name}")
            if updated_at <= 0 or lag > allowed:
                errors.append(f"source_lag:{name}:{lag:.3f}")
        except Exception as exc:
            row.update(ready=False, error=type(exc).__name__)
            errors.append(f"source_unavailable:{name}")
        checks[f"replication:{name}"] = row
    return "continuous", max_lag


def build_state(config: dict[str, Any]) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    errors: list[str] = []
    check_required_files(config, checks, errors)
    check_health(config, checks, errors)
    check_monero(config, checks, errors)
    mode, lag = check_continuous_sources(config, checks, errors)
    state: dict[str, Any] = {
        "version": 1,
        "mode": mode,
        "ready": not errors,
        "updated_at": time.time(),
        "checks": checks,
        "errors": errors,
    }
    if lag is not None:
        state["lag_seconds"] = lag
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description="HashBurst HA DR readiness producer")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config_path = Path(args.config)
    output_path = Path(args.output)
    while True:
        try:
            config = json.loads(config_path.read_text())
            if not isinstance(config, dict):
                raise ValueError("configuration root must be an object")
            state = build_state(config)
        except Exception as exc:
            state = {
                "version": 1,
                "mode": "reconstructable",
                "ready": False,
                "updated_at": time.time(),
                "checks": {},
                "errors": [f"producer_error:{type(exc).__name__}"],
            }
        atomic_json_write(output_path, state)
        if args.once:
            print(json.dumps(state, indent=2, sort_keys=True))
            return
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    main()
