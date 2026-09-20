#!/usr/bin/env python3
from __future__ import annotations

import json
import unittest

from ha.hashburst_master_ingress import (
    IngressError,
    MasterResolver,
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


def healthy_ha(
    *,
    voters=3,
    quorum=2,
    lease_ms=12000,
):
    return {
        "status": {
            "armed": True,
            "eligible": True,
            "holder": "hashburst-dr1",
            "term": 7,
            "lease_remaining_ms": lease_ms,
            "cluster_view": {
                "holder": "hashburst-dr1",
                "holder_term": 7,
                "voters_reachable": voters,
                "voters_total": 3,
                "quorum": quorum,
            },
        },
    }


def healthy_tep():
    return {
        "node_id": "blockchainapi.one",
        "app_ready": True,
        "peers": [
            {
                "id": "hashburst-dr1",
                "online": True,
                "ip": "192.0.2.153",
                "peer_id": "peer-dr1",
            },
        ],
    }


class ResolverOpener:
    def __init__(
        self,
        *,
        ha=None,
        tep=None,
        master=None,
    ):
        self.ha = ha or healthy_ha()
        self.tep = tep or healthy_tep()
        self.master = master or {
            "ok": True,
            "path": "direct",
            "relay_peer_id": None,
            "result": {
                "network": "testnet",
                "master_live": True,
                "active_workers": 0,
            },
        }
        self.calls = []

    def __call__(
        self,
        request,
        timeout,
    ):
        self.calls.append({
            "url": request.full_url,
            "method": request.get_method(),
            "body": request.data,
            "timeout": timeout,
        })

        if request.full_url.endswith(
            ":47780/v1/status"
        ):
            return FakeResponse(self.ha)

        if request.full_url.endswith(
            ":47778/"
        ):
            return FakeResponse(self.tep)

        if request.full_url.endswith(
            ":47781/app/master-status"
        ):
            return FakeResponse(self.master)

        raise AssertionError(
            "unexpected URL "
            + request.full_url
        )


class MasterIngressTests(unittest.TestCase):
    def test_discovery_uses_current_ha_holder(self):
        opener = ResolverOpener()
        resolver = MasterResolver(
            opener=opener,
        )

        data = resolver.discovery()

        self.assertTrue(data["available"])
        self.assertEqual(
            data["master"]["node_id"],
            "hashburst-dr1",
        )
        self.assertEqual(
            data["master"]["tep_host"],
            "192.0.2.153",
        )
        self.assertEqual(
            data["master"]["tep_port"],
            8765,
        )
        self.assertEqual(
            data["ha"]["voters_reachable"],
            3,
        )
        self.assertEqual(
            data["ha"]["quorum"],
            2,
        )

    def test_no_quorum_fails_closed(self):
        opener = ResolverOpener(
            ha=healthy_ha(
                voters=1,
                quorum=2,
            ),
        )
        resolver = MasterResolver(
            opener=opener,
        )

        with self.assertRaises(
            IngressError,
        ) as raised:
            resolver.discovery()

        self.assertEqual(
            raised.exception.code,
            "ha_no_quorum",
        )

    def test_expiring_lease_fails_closed(self):
        opener = ResolverOpener(
            ha=healthy_ha(
                lease_ms=500,
            ),
        )
        resolver = MasterResolver(
            opener=opener,
        )

        with self.assertRaises(
            IngressError,
        ) as raised:
            resolver.discovery()

        self.assertEqual(
            raised.exception.code,
            "ha_lease_expiring",
        )

    def test_master_status_uses_local_tep_ipc(self):
        opener = ResolverOpener()
        resolver = MasterResolver(
            opener=opener,
        )

        (
            result,
            route,
            transport_path,
            relay_peer_id,
        ) = resolver.master_status()

        self.assertTrue(
            result["master_live"]
        )
        self.assertEqual(
            result["network"],
            "testnet",
        )
        self.assertEqual(
            route.node_id,
            "hashburst-dr1",
        )
        self.assertEqual(
            transport_path,
            "direct",
        )
        self.assertIsNone(
            relay_peer_id,
        )

        ipc_calls = [
            call
            for call in opener.calls
            if call["url"].endswith(
                ":47781/app/master-status"
            )
        ]

        self.assertEqual(
            len(ipc_calls),
            1,
        )
        self.assertEqual(
            ipc_calls[0]["method"],
            "POST",
        )

        payload = json.loads(
            ipc_calls[0]["body"].decode(
                "utf-8"
            )
        )

        self.assertEqual(
            payload,
            {
                "node_id": "hashburst-dr1",
                "peer_id": "peer-dr1",
            },
        )

    def test_offline_holder_fails_closed(self):
        tep = healthy_tep()
        tep["peers"][0]["online"] = False

        resolver = MasterResolver(
            opener=ResolverOpener(
                tep=tep,
            ),
        )

        with self.assertRaises(
            IngressError,
        ) as raised:
            resolver.discovery()

        self.assertEqual(
            raised.exception.code,
            "master_offline",
        )


if __name__ == "__main__":
    unittest.main()
