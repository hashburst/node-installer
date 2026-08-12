#!/usr/bin/env python3
"""Storage aggregator adapter for HB-TEP-APP/1.

Step 3 does not wire to the production TEP daemon. A TepRpcClient must be injected
by staging/tests. The default path fails closed with tep_unavailable.
"""
from __future__ import annotations

from typing import Any

try:
    from tep.hb_tep_app import Identity
    from tep.hb_tep_client import TepClientError, TepRpcClient
except ImportError:  # pragma: no cover - supports direct execution layouts
    from hb_tep_app import Identity  # type: ignore
    from hb_tep_client import TepClientError, TepRpcClient  # type: ignore

STORAGE_SUMMARY_SERVICE = "storage.summary"


class TepAdapterError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


_DEFAULT_CLIENT: TepRpcClient | None = None


def configure_client(client: TepRpcClient | None) -> None:
    """Set the process-local TEP client. Production wiring is deferred to Step 5."""
    global _DEFAULT_CLIENT
    if client is not None and not isinstance(client, TepRpcClient):
        raise TypeError("client must be TepRpcClient or None")
    _DEFAULT_CLIENT = client


def fetch_summary(node: dict[str, Any], timeout: float, *, client: TepRpcClient | None = None) -> dict[str, Any]:
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

    rpc = client or _DEFAULT_CLIENT
    if rpc is None:
        raise TepAdapterError("tep_unavailable", "TEP RPC client is not configured")

    try:
        summary = rpc.request(
            destination=Identity(node_id=node_id, peer_id=peer_id),
            service=STORAGE_SUMMARY_SERVICE,
            payload={},
            timeout_sec=timeout,
        )
    except TepClientError as exc:
        raise TepAdapterError(exc.code, exc.message) from exc
    if not isinstance(summary, dict):
        raise TepAdapterError("bad_response", "TEP storage summary must be an object")
    return summary
