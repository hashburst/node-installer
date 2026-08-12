from __future__ import annotations

import unittest

from tep.hb_tep_app import Identity, decode_message, encode_message, new_response
from tep.hb_tep_client import TepRpcClient
from tep.hb_tep_relay import FailoverTepTransport

SRC = Identity("blockchainapi.one", "peer-agg")
TARGET = Identity("node-7", "peer-node7")


class NatRelaySimulationTests(unittest.TestCase):
    def test_symmetric_nat_like_direct_failure_falls_back_to_relay(self):
        events = []
        def direct(peer, raw, timeout):
            events.append(("direct", peer))
            raise TimeoutError("simulated symmetric NAT")
        def relay(relay_peer, target_peer, raw, timeout):
            events.append(("relay", relay_peer, target_peer))
            req = decode_message(raw)
            return encode_message(new_response(req.raw, source=TARGET, destination=SRC,
                                                payload={"node_id":"node-7","role":"edge","available":True}))
        transport = FailoverTepTransport(direct=direct, relay=relay,
                                         relay_peer_ids=["peer-rendezvous-a", "peer-rendezvous-b"])
        client = TepRpcClient(local_identity=SRC, transport=transport)
        out = client.request(destination=TARGET, service="storage.summary", payload={}, timeout_sec=3.0)
        self.assertEqual(out["role"], "edge")
        self.assertEqual(events[0], ("direct", TARGET.peer_id))
        self.assertEqual(events[1][0], "relay")
        self.assertEqual(transport.last_path, "relay")

    def test_relay_failover_after_first_rendezvous_loss(self):
        events=[]
        def direct(*_): raise TimeoutError()
        def relay(rp, target, raw, timeout):
            events.append(rp)
            if rp == "r1": raise TimeoutError("r1 down")
            req=decode_message(raw)
            return encode_message(new_response(req.raw, source=TARGET, destination=SRC, payload={"ok":True}))
        transport=FailoverTepTransport(direct=direct, relay=relay, relay_peer_ids=["r1","r2"], max_relay_attempts=2)
        client=TepRpcClient(local_identity=SRC, transport=transport)
        self.assertEqual(client.request(destination=TARGET, service="storage.summary", timeout_sec=3.0), {"ok":True})
        self.assertEqual(events,["r1","r2"])
        self.assertEqual(transport.last_relay_peer_id,"r2")

    def test_all_paths_down_returns_timeout(self):
        def fail(*_): raise TimeoutError()
        transport=FailoverTepTransport(direct=fail, relay=lambda *a: (_ for _ in ()).throw(TimeoutError()), relay_peer_ids=["r1","r2"])
        client=TepRpcClient(local_identity=SRC, transport=transport)
        with self.assertRaises(Exception) as cm:
            client.request(destination=TARGET, service="storage.summary", timeout_sec=1.0)
        self.assertIn("request_timeout", str(cm.exception))


if __name__ == "__main__": unittest.main()
