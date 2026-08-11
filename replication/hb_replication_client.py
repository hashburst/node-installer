#!/usr/bin/env python3
"""Minimal HB-Files -> Replication Controller client (stdlib only).

This helper is ready for integration into hb_files.py after the controller is
rolled out. It does not alter HB-Files automatically in this code phase.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class ReplicationClientError(Exception):
    pass


class ReplicationClient:
    def __init__(self, base_url: str, admin_token: str, timeout: int = 5):
        self.base = base_url.rstrip("/")
        self.token = admin_token
        self.timeout = timeout

    def _post(self, path: str, payload: dict) -> dict:
        raw = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + path,
            data=raw,
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
            raise ReplicationClientError(str(e)) from e

    def register(self, cid: str, size_bytes: int, source_node: str, reference_id: str | None = None,
                 replication_n: int | None = None, committable_m: int | None = None) -> dict:
        payload = {"cid": cid, "size_bytes": int(size_bytes), "source_node": source_node}
        if reference_id:
            payload["reference_id"] = reference_id
        if replication_n is not None:
            payload["replication_n"] = int(replication_n)
        if committable_m is not None:
            payload["committable_m"] = int(committable_m)
        return self._post("/v1/objects/register", payload)

    def release(self, cid: str, reference_id: str | None = None) -> dict:
        payload = {"cid": cid}
        if reference_id:
            payload["reference_id"] = reference_id
        return self._post("/v1/objects/release", payload)
