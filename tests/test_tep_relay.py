from __future__ import annotations

import unittest

from tep.hb_tep_app import Identity, encode_message, new_request, new_response
from tep.hb_tep_relay import (
    FailoverTepTransport,
    RelayDispatcher,
    RelayError,
    RelayPolicy,
    RelayTable,
    new_relay_request,
)


class FakeClock:
    def __init__(self, value=100.0): self.value = float(value)
    def __call__(self): return self.value
    def advance(self, seconds): self.value += float(seconds)


SRC = Identity("blockchainapi.one", "peer-agg")
RENDEZVOUS = Identity("node-6", "peer-relay")
TARGET = Identity("node-7", "peer-node7")


class RelayTableTests(unittest.TestCase):
    def test_authenticated_observation_and_lookup(self):
        c = FakeClock()
        t = RelayTable(clock=c)
        t.observe(peer_id=TARGET.peer_id, ip="198.51.100.7", port=45678, authenticated=True)
        r = t.get(TARGET.peer_id)
        self.assertEqual((r.ip, r.port), ("198.51.100.7", 45678))

    def test_unauthenticated_observation_rejected(self):
        t = RelayTable()
        with self.assertRaises(RelayError) as cm:
            t.observe(peer_id=TARGET.peer_id, ip="203.0.113.9", port=50000, authenticated=False)
        self.assertEqual(cm.exception.code, "authentication_failed")

    def test_dynamic_nat_port_update(self):
        c = FakeClock()
        t = RelayTable(clock=c)
        t.observe(peer_id=TARGET.peer_id, ip="198.51.100.7", port=40000, authenticated=True)
        c.advance(1)
        t.observe(peer_id=TARGET.peer_id, ip="198.51.100.7", port=54123, authenticated=True)
        self.assertEqual(t.get(TARGET.peer_id).port, 54123)

    def test_dynamic_ip_update_same_identity(self):
        c = FakeClock(); t = RelayTable(clock=c)
        t.observe(peer_id=TARGET.peer_id, ip="198.51.100.7", port=40000, authenticated=True)
        c.advance(1)
        t.observe(peer_id=TARGET.peer_id, ip="203.0.113.77", port=62001, authenticated=True)
        r = t.get(TARGET.peer_id)
        self.assertEqual(r.peer_id, TARGET.peer_id)
        self.assertEqual((r.ip, r.port), ("203.0.113.77", 62001))

    def test_stale_route_rejected(self):
        c = FakeClock(); t = RelayTable(clock=c)
        t.observe(peer_id=TARGET.peer_id, ip="198.51.100.7", port=40000, authenticated=True)
        c.advance(31)
        with self.assertRaises(RelayError) as cm:
            t.get(TARGET.peer_id, max_age_sec=30)
        self.assertEqual(cm.exception.code, "peer_offline")


class RelayPolicyTests(unittest.TestCase):
    def test_untrusted_source_rejected(self):
        p = RelayPolicy(trusted_sources={SRC.peer_id}, registered_targets={TARGET.peer_id})
        with self.assertRaises(RelayError) as cm:
            p.authorize(source_peer_id="rogue", target_peer_id=TARGET.peer_id, inner_service="storage.summary")
        self.assertEqual(cm.exception.code, "relay_unauthorized")

    def test_unregistered_target_rejected(self):
        p = RelayPolicy(trusted_sources={SRC.peer_id}, registered_targets={TARGET.peer_id})
        with self.assertRaises(RelayError) as cm:
            p.authorize(source_peer_id=SRC.peer_id, target_peer_id="peer-unknown", inner_service="storage.summary")
        self.assertEqual(cm.exception.code, "destination_unknown")

    def test_non_allowlisted_service_rejected(self):
        p = RelayPolicy(trusted_sources={SRC.peer_id}, registered_targets={TARGET.peer_id})
        with self.assertRaises(RelayError) as cm:
            p.authorize(source_peer_id=SRC.peer_id, target_peer_id=TARGET.peer_id, inner_service="admin")
        self.assertEqual(cm.exception.code, "unsupported_service")

    def test_pending_limit_is_bounded(self):
        p = RelayPolicy(trusted_sources={SRC.peer_id}, registered_targets={TARGET.peer_id}, max_pending=1, max_pending_per_source=1)
        p.acquire(SRC.peer_id)
        with self.assertRaises(RelayError) as cm:
            p.acquire(SRC.peer_id)
        self.assertEqual(cm.exception.code, "relay_overloaded")
        p.release(SRC.peer_id)
        self.assertEqual(p.pending_total, 0)


class RelayDispatcherTests(unittest.TestCase):
    def make_dispatcher(self, forward):
        table = RelayTable()
        table.observe(peer_id=TARGET.peer_id, ip="198.51.100.7", port=41000, authenticated=True)
        policy = RelayPolicy(trusted_sources={SRC.peer_id}, registered_targets={TARGET.peer_id})
        return RelayDispatcher(local_identity=RENDEZVOUS, table=table, policy=policy, forward_target=forward), policy

    def make_inner(self):
        return new_request(source=SRC, destination=TARGET, service="storage.summary", payload={})

    def test_forward_success_one_hop(self):
        inner = self.make_inner()
        def forward(route, raw, timeout):
            self.assertEqual(route.peer_id, TARGET.peer_id)
            return encode_message(new_response(inner, source=TARGET, destination=SRC, payload={"available": True}))
        d, policy = self.make_dispatcher(forward)
        outer = new_relay_request(source=SRC, rendezvous=RENDEZVOUS, target_peer_id=TARGET.peer_id, inner_request=encode_message(inner))
        response = d.handle(outer, 2.0)
        self.assertEqual(response["type"], "relay_res")
        self.assertEqual(policy.pending_total, 0)

    def test_inner_target_mismatch_rejected(self):
        other = Identity("other", "peer-other")
        inner = new_request(source=SRC, destination=other, service="storage.summary", payload={})
        d, _ = self.make_dispatcher(lambda *a: b"")
        outer = new_relay_request(source=SRC, rendezvous=RENDEZVOUS, target_peer_id=TARGET.peer_id, inner_request=encode_message(inner))
        with self.assertRaises(RelayError) as cm:
            d.handle(outer, 2.0)
        self.assertEqual(cm.exception.code, "identity_mismatch")

    def test_wrong_rendezvous_rejected(self):
        inner = self.make_inner(); d, _ = self.make_dispatcher(lambda *a: b"")
        wrong = Identity("wrong", "peer-wrong")
        outer = new_relay_request(source=SRC, rendezvous=wrong, target_peer_id=TARGET.peer_id, inner_request=encode_message(inner))
        with self.assertRaises(RelayError) as cm:
            d.handle(outer, 2.0)
        self.assertEqual(cm.exception.code, "relay_wrong_destination")

    def test_target_timeout_is_relay_timeout(self):
        inner = self.make_inner()
        def forward(*_): raise TimeoutError()
        d, policy = self.make_dispatcher(forward)
        outer = new_relay_request(source=SRC, rendezvous=RENDEZVOUS, target_peer_id=TARGET.peer_id, inner_request=encode_message(inner))
        with self.assertRaises(RelayError) as cm:
            d.handle(outer, 2.0)
        self.assertEqual(cm.exception.code, "relay_timeout")
        self.assertEqual(policy.pending_total, 0)

    def test_malformed_inner_rejected(self):
        d, _ = self.make_dispatcher(lambda *a: b"")
        outer = new_relay_request(source=SRC, rendezvous=RENDEZVOUS, target_peer_id=TARGET.peer_id, inner_request=b"not json")
        with self.assertRaises(RelayError): d.handle(outer, 2.0)


class FailoverTransportTests(unittest.TestCase):
    def test_direct_success_uses_no_relay(self):
        calls = []
        f = FailoverTepTransport(
            direct=lambda peer, raw, timeout: b"direct-response",
            relay=lambda *args: calls.append(args),
            relay_peer_ids=["r1", "r2"],
        )
        self.assertEqual(f("target", b"req", 3.0), b"direct-response")
        self.assertEqual(calls, [])
        self.assertEqual(f.last_path, "direct")

    def test_direct_timeout_then_relay_success(self):
        def direct(*_): raise TimeoutError()
        def relay(relay_peer, target, raw, timeout):
            self.assertEqual(relay_peer, "r1")
            return b"relay-response"
        f = FailoverTepTransport(direct=direct, relay=relay, relay_peer_ids=["r1", "r2"])
        self.assertEqual(f("target", b"req", 3.0), b"relay-response")
        self.assertEqual(f.last_path, "relay")
        self.assertEqual(f.last_relay_peer_id, "r1")

    def test_first_relay_fails_second_succeeds(self):
        calls = []
        def direct(*_): raise TimeoutError()
        def relay(relay_peer, *_):
            calls.append(relay_peer)
            if relay_peer == "r1": raise TimeoutError()
            return b"ok"
        f = FailoverTepTransport(direct=direct, relay=relay, relay_peer_ids=["r1", "r2", "r3"], max_relay_attempts=2)
        self.assertEqual(f("target", b"req", 3.0), b"ok")
        self.assertEqual(calls, ["r1", "r2"])
        self.assertEqual(f.last_relay_peer_id, "r2")

    def test_bounded_attempts_no_infinite_fallback(self):
        calls = []
        def direct(*_): raise TimeoutError()
        def relay(relay_peer, *_): calls.append(relay_peer); raise TimeoutError()
        f = FailoverTepTransport(direct=direct, relay=relay, relay_peer_ids=["r1", "r2", "r3"], max_relay_attempts=2)
        with self.assertRaises(TimeoutError): f("target", b"req", 3.0)
        self.assertEqual(calls, ["r1", "r2"])
        self.assertEqual(f.last_path, "failed")


if __name__ == "__main__": unittest.main()
