import json
import unittest

from tep.hb_tep_app import Identity, decode_message, encode_message, new_error, new_response
from tep.hb_tep_client import PendingRequestTable, TepClientError, TepRpcClient


LOCAL = Identity("aggregator", "peer-aggregator")
REMOTE = Identity("node-7", "peer-node7")


class FakeClock:
    def __init__(self): self.now = 100.0
    def __call__(self): return self.now


class TepClientTests(unittest.TestCase):
    def test_request_response_correlation(self):
        def transport(peer_id, raw, timeout):
            self.assertEqual(peer_id, REMOTE.peer_id)
            req = decode_message(raw, check_time=False).raw
            return encode_message(new_response(req, source=REMOTE, destination=LOCAL, payload={"ok": True}))
        client = TepRpcClient(local_identity=LOCAL, transport=transport)
        self.assertEqual(client.request(destination=REMOTE, service="storage.summary"), {"ok": True})
        self.assertEqual(len(client.pending), 0)

    def test_remote_error_propagates_stable_code(self):
        def transport(peer_id, raw, timeout):
            req = decode_message(raw, check_time=False).raw
            return encode_message(new_error(req, source=REMOTE, destination=LOCAL,
                                            code="peer_offline", message_text="offline", status=503))
        client = TepRpcClient(local_identity=LOCAL, transport=transport)
        with self.assertRaises(TepClientError) as cm:
            client.request(destination=REMOTE, service="storage.summary")
        self.assertEqual(cm.exception.code, "peer_offline")

    def test_wrong_response_peer_rejected(self):
        attacker = Identity("attacker", "peer-attacker")
        def transport(peer_id, raw, timeout):
            req = decode_message(raw, check_time=False).raw
            return new_response(req, source=attacker, destination=LOCAL, payload={"ok": True})
        client = TepRpcClient(local_identity=LOCAL, transport=transport)
        with self.assertRaises(TepClientError) as cm:
            client.request(destination=REMOTE, service="storage.summary")
        self.assertEqual(cm.exception.code, "response_peer_mismatch")

    def test_wrong_destination_rejected(self):
        other = Identity("other", "peer-other")
        def transport(peer_id, raw, timeout):
            req = decode_message(raw, check_time=False).raw
            return new_response(req, source=REMOTE, destination=other, payload={"ok": True})
        client = TepRpcClient(local_identity=LOCAL, transport=transport)
        with self.assertRaises(TepClientError) as cm:
            client.request(destination=REMOTE, service="storage.summary")
        self.assertEqual(cm.exception.code, "response_destination_mismatch")

    def test_unexpected_response_type_rejected(self):
        def transport(peer_id, raw, timeout):
            return raw
        client = TepRpcClient(local_identity=LOCAL, transport=transport)
        with self.assertRaises(TepClientError) as cm:
            client.request(destination=REMOTE, service="storage.summary")
        self.assertEqual(cm.exception.code, "unexpected_response_type")

    def test_timeout_wrapped(self):
        def transport(peer_id, raw, timeout):
            raise TimeoutError()
        client = TepRpcClient(local_identity=LOCAL, transport=transport)
        with self.assertRaises(TepClientError) as cm:
            client.request(destination=REMOTE, service="storage.summary")
        self.assertEqual(cm.exception.code, "request_timeout")
        self.assertEqual(len(client.pending), 0)

    def test_generic_transport_failure_does_not_leak_details(self):
        def transport(peer_id, raw, timeout):
            raise RuntimeError("SECRET internal path /etc/hashburst/key")
        client = TepRpcClient(local_identity=LOCAL, transport=transport)
        with self.assertRaises(TepClientError) as cm:
            client.request(destination=REMOTE, service="storage.summary")
        self.assertEqual(cm.exception.code, "transport_error")
        self.assertNotIn("SECRET", str(cm.exception))

    def test_pending_table_bounded(self):
        clock = FakeClock()
        table = PendingRequestTable(max_pending=1, clock=clock)
        table.add("0" * 32, REMOTE.peer_id, "storage.summary", 3)
        with self.assertRaises(TepClientError) as cm:
            table.add("1" * 32, REMOTE.peer_id, "storage.summary", 3)
        self.assertEqual(cm.exception.code, "client_overloaded")

    def test_pending_expiry(self):
        clock = FakeClock()
        table = PendingRequestTable(max_pending=2, clock=clock)
        table.add("0" * 32, REMOTE.peer_id, "storage.summary", 1)
        clock.now += 2
        self.assertEqual(table.expire(), 1)
        self.assertEqual(len(table), 0)


if __name__ == "__main__": unittest.main()
