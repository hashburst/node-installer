import threading
import unittest
from unittest.mock import patch

from tep.hb_tep import (
    PEER_ONLINE_TTL_SEC,
    Peer,
    PeerManager,
)


class PeerFreshnessTests(unittest.TestCase):
    @staticmethod
    def make_manager(*peers):
        manager = object.__new__(PeerManager)
        manager._lock = threading.Lock()
        manager._peers = {
            peer.id: peer
            for peer in peers
        }
        manager._rpc_port = 8009
        manager._dns_source = "test"
        return manager

    def test_online_count_expires_stale_invalid_and_future_peers(self):
        now = 1000.0

        recent = Peer(
            id="recent",
            ip="192.0.2.10",
            last_seen=now - 1.0,
            online=True,
        )
        boundary = Peer(
            id="boundary",
            ip="192.0.2.11",
            last_seen=now - PEER_ONLINE_TTL_SEC,
            online=True,
        )
        stale = Peer(
            id="stale",
            ip="192.0.2.12",
            last_seen=now - PEER_ONLINE_TTL_SEC - 0.001,
            online=True,
        )
        never_seen = Peer(
            id="never-seen",
            ip="192.0.2.13",
            last_seen=0.0,
            online=True,
        )
        future = Peer(
            id="future",
            ip="192.0.2.14",
            last_seen=now + 1.0,
            online=True,
        )
        already_offline = Peer(
            id="already-offline",
            ip="192.0.2.15",
            last_seen=now - 1.0,
            online=False,
        )

        manager = self.make_manager(
            recent,
            boundary,
            stale,
            never_seen,
            future,
            already_offline,
        )

        with patch("tep.hb_tep.time.time", return_value=now):
            self.assertEqual(manager.online_count(), 2)

        self.assertTrue(recent.online)
        self.assertTrue(boundary.online)
        self.assertFalse(stale.online)
        self.assertFalse(never_seen.online)
        self.assertFalse(future.online)
        self.assertFalse(already_offline.online)

    def test_to_json_never_exports_stale_peer_as_online(self):
        now = 2000.0

        fresh = Peer(
            id="fresh",
            ip="192.0.2.20",
            last_seen=now - 5.0,
            online=True,
        )
        stale = Peer(
            id="stale",
            ip="192.0.2.21",
            last_seen=now - PEER_ONLINE_TTL_SEC - 1.0,
            online=True,
        )

        manager = self.make_manager(fresh, stale)

        with patch("tep.hb_tep.time.time", return_value=now):
            exported = {
                peer["id"]: peer
                for peer in manager.to_json()
            }

        self.assertTrue(exported["fresh"]["online"])
        self.assertFalse(exported["stale"]["online"])
        self.assertFalse(stale.online)

    def test_mark_seen_restores_peer_freshness(self):
        now = 3000.0

        peer = Peer(
            id="recovered",
            ip="192.0.2.30",
            last_seen=now - PEER_ONLINE_TTL_SEC - 1.0,
            online=True,
        )
        manager = self.make_manager(peer)

        with patch("tep.hb_tep.time.time", return_value=now):
            self.assertEqual(manager.online_count(), 0)

            manager.mark_seen(
                "recovered",
                latency_ms=12.5,
                pubkey="test-public-key",
            )

            self.assertEqual(manager.online_count(), 1)

        self.assertTrue(peer.online)
        self.assertEqual(peer.last_seen, now)
        self.assertEqual(peer.latency_ms, 12.5)
        self.assertEqual(peer.pubkey, "test-public-key")


if __name__ == "__main__":
    unittest.main()
