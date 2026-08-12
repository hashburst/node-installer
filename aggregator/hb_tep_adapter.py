#!/usr/bin/env python3
"""Production loopback adapter from storage aggregator to the HB-TEP daemon.

The aggregator never performs TEP crypto, peer routing, or relay selection itself.
For nodes configured with transport=tep it sends one fixed JSON request to the
local HB-TEP daemon on 127.0.0.1. The daemon constructs storage.summary and owns
all authenticated UDP/direct/relay behavior.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

IPC_HOST = "127.0.0.1"
IPC_DEFAULT_PORT = 47778
IPC_PATH = "/app/storage-summary"
IPC_MAX_RESPONSE = 32 * 1024


class TepAdapterError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _ipc_port() -> int:
    raw = os.environ.get("HB_TEP_IPC_PORT", str(IPC_DEFAULT_PORT)).strip()
    try:
        port = int(raw)
    except ValueError as exc:
        raise TepAdapterError("bad_config", "HB_TEP_IPC_PORT must be an integer") from exc
    if port < 1 or port > 65535:
        raise TepAdapterError("bad_config", "HB_TEP_IPC_PORT must be in [1, 65535]")
    return port


def _ipc_url() -> str:
    # Host and path are intentionally non-configurable: this is local IPC only.
    return f"http://{IPC_HOST}:{_ipc_port()}{IPC_PATH}"


def _decode_response(stream) -> dict[str, Any]:
    raw = stream.read(IPC_MAX_RESPONSE + 1)
    if len(raw) > IPC_MAX_RESPONSE:
        raise TepAdapterError("bad_response", "TEP IPC response too large")
    try:
        body = json.loads(raw.decode("utf-8", "strict"))
    except Exception as exc:
        raise TepAdapterError("bad_response", "TEP IPC returned invalid JSON") from exc
    if not isinstance(body, dict):
        raise TepAdapterError("bad_response", "TEP IPC response must be an object")
    return body


def _raise_ipc_error(body: dict[str, Any], default_code: str = "tep_unavailable") -> None:
    error = body.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or default_code)[:64]
        message = str(error.get("message") or "TEP IPC request failed")[:160]
    else:
        code = default_code
        message = "TEP IPC request failed"
    raise TepAdapterError(code, message)


def fetch_summary(node: dict[str, Any], timeout: float) -> dict[str, Any]:
    if not isinstance(node, dict):
        raise TepAdapterError("bad_config", "node configuration must be an object")

    peer_id = str(node.get("tep_peer_id") or "").strip()
    if not peer_id:
        raise TepAdapterError("bad_config", "TEP node requires tep_peer_id")

    node_id = str(node.get("tep_node_id") or node.get("name") or "").strip()
    if not node_id:
        raise TepAdapterError("bad_config", "TEP node requires a stable node identifier")

    if timeout <= 0 or timeout > 5:
        raise TepAdapterError("bad_config", "timeout must be in (0, 5]")

    payload = json.dumps(
        {"node_id": node_id, "peer_id": peer_id},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    req = urllib.request.Request(
        _ipc_url(),
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "HashBurst-Storage-Aggregator/2.1",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=min(5.0, float(timeout) + 0.5)) as response:
            body = _decode_response(response)
    except urllib.error.HTTPError as exc:
        try:
            body = _decode_response(exc)
        except TepAdapterError:
            raise TepAdapterError("tep_unavailable", "TEP IPC request failed") from None
        finally:
            exc.close()
        _raise_ipc_error(body)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TepAdapterError("tep_unavailable", "TEP IPC is unavailable") from exc

    if body.get("ok") is not True:
        _raise_ipc_error(body, default_code="bad_response")

    summary = body.get("summary")
    if not isinstance(summary, dict):
        raise TepAdapterError("bad_response", "TEP IPC summary must be an object")

    # Transport metadata is local trust-domain information. A remote storage
    # summary must not be allowed to forge these fields.
    out = {k: v for k, v in summary.items() if not str(k).startswith("_tep_")}

    path = str(body.get("path") or "").strip()
    if path not in {"direct", "relay"}:
        raise TepAdapterError("bad_response", "TEP IPC returned invalid transport path")
    out["_tep_transport_path"] = path

    relay_peer_id = body.get("relay_peer_id")
    if path == "relay":
        if not isinstance(relay_peer_id, str) or not relay_peer_id.strip():
            raise TepAdapterError("bad_response", "TEP relay response is missing relay peer identity")
        out["_tep_relay_peer_id"] = relay_peer_id.strip()
    elif relay_peer_id not in (None, ""):
        raise TepAdapterError("bad_response", "direct TEP response cannot name a relay peer")

    rtt_ms = body.get("rtt_ms")
    if rtt_ms is not None:
        if not isinstance(rtt_ms, (int, float)) or isinstance(rtt_ms, bool) or rtt_ms < 0:
            raise TepAdapterError("bad_response", "TEP IPC returned invalid RTT")
        out["_tep_rtt_ms"] = float(rtt_ms)

    return out
