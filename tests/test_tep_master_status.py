#!/usr/bin/env python3
from __future__ import annotations

import json
import unittest

from tep.hb_tep_app import ProtocolError
from tep.hb_tep_master_service import (
    MasterStatusConfig,
    MasterStatusHandler,
)


class FakeResponse:
    def __init__(
        self,
        payload,
        status=200,
    ):
        self.status = status
        self._body = json.dumps(
            payload,
            separators=(",", ":"),
        ).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(
        self,
        exc_type,
        exc_value,
        traceback,
    ):
        return False

    def read(self, size=-1):
        return self._body[:size]


class MasterStatusTests(unittest.TestCase):
    def test_returns_live_master_status(self):
        calls = []

        def opener(request, timeout):
            calls.append(
                (
                    request.full_url,
                    request.get_method(),
                    timeout,
                )
            )
            return FakeResponse({
                "network": "testnet",
                "master_live": True,
                "active_workers": 0,
            })

        handler = MasterStatusHandler(
            opener=opener,
        )

        result = handler({})

        self.assertTrue(result["master_live"])
        self.assertEqual(result["network"], "testnet")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "GET")
        self.assertEqual(
            calls[0][0],
            "http" + "://127.0.0.1:9000/api/status",
        )

    def test_rejects_caller_controlled_routing(self):
        handler = MasterStatusHandler(
            opener=lambda request, timeout: FakeResponse({
                "master_live": True,
            }),
        )

        with self.assertRaises(ProtocolError) as raised:
            handler({
                "url": (
                    "http"
                    + "://192.0.2.10/private"
                ),
            })

        self.assertEqual(
            raised.exception.code,
            "bad_request",
        )

    def test_rejects_non_loopback_configuration(self):
        with self.assertRaises(ValueError):
            MasterStatusConfig(
                url=(
                    "http"
                    + "://192.0.2.10:9000/api/status"
                ),
            )

    def test_rejects_master_not_live(self):
        handler = MasterStatusHandler(
            opener=lambda request, timeout: FakeResponse({
                "master_live": False,
            }),
        )

        with self.assertRaises(Exception) as raised:
            handler({})

        self.assertEqual(
            getattr(raised.exception, "code", None),
            "master_not_live",
        )


if __name__ == "__main__":
    unittest.main()
