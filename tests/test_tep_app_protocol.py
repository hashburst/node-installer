#!/usr/bin/env python3
from __future__ import annotations

import json
import unittest

from tep.hb_tep_app import (
    Identity,
    MAX_REQUEST_BYTES,
    ProtocolError,
    ReplayCache,
    decode_message,
    encode_message,
    new_error,
    new_request,
    new_response,
)

NOW = 1_786_500_000_000
SRC = Identity("blockchainapi.one", "peer-aggregator")
DST = Identity("node-7", "peer-node7")


def valid_request(**changes):
    msg = new_request(
        source=SRC,
        destination=DST,
        service="storage.summary",
        payload={},
        ttl_ms=3000,
        timestamp_ms=NOW,
    )
    msg.update(changes)
    return msg


class ProtocolRoundTripTests(unittest.TestCase):
    def test_request_roundtrip(self):
        msg = valid_request()
        env = decode_message(encode_message(msg), now_ms=NOW)
        self.assertEqual(env.msg_type, "req")
        self.assertEqual(env.service, "storage.summary")
        self.assertEqual(env.source.peer_id, "peer-aggregator")
        self.assertEqual(env.destination.peer_id, "peer-node7")

    def test_response_roundtrip(self):
        req = valid_request()
        msg = new_response(req, source=DST, destination=SRC,
                           payload={"available": True}, timestamp_ms=NOW)
        env = decode_message(encode_message(msg), now_ms=NOW)
        self.assertEqual(env.msg_type, "res")
        self.assertEqual(env.request_id, req["request_id"])
        self.assertEqual(env.raw["status"], 200)

    def test_error_roundtrip(self):
        req = valid_request()
        msg = new_error(req, source=DST, destination=SRC,
                        code="local_service_unavailable", status=503,
                        message_text="summary unavailable", timestamp_ms=NOW)
        env = decode_message(encode_message(msg), now_ms=NOW)
        self.assertEqual(env.msg_type, "err")
        self.assertEqual(env.raw["error"]["code"], "local_service_unavailable")


class DecodeValidationTests(unittest.TestCase):
    def assertCode(self, expected, func, *args, **kwargs):
        with self.assertRaises(ProtocolError) as ctx:
            func(*args, **kwargs)
        self.assertEqual(ctx.exception.code, expected)

    def test_invalid_json(self):
        self.assertCode("bad_request", decode_message, b"{", now_ms=NOW)

    def test_invalid_utf8(self):
        self.assertCode("bad_request", decode_message, b"\xff", now_ms=NOW)

    def test_invalid_root(self):
        self.assertCode("bad_request", decode_message, b"[]", now_ms=NOW)

    def test_empty(self):
        self.assertCode("bad_request", decode_message, b"", now_ms=NOW)

    def test_missing_version(self):
        msg = valid_request(); msg.pop("v")
        self.assertCode("bad_request", decode_message, json.dumps(msg), now_ms=NOW)

    def test_unsupported_version(self):
        msg = valid_request(v=2)
        self.assertCode("unsupported_version", decode_message, json.dumps(msg), now_ms=NOW)

    def test_unsupported_type(self):
        msg = valid_request(type="ping")
        self.assertCode("unsupported_type", decode_message, json.dumps(msg), now_ms=NOW)

    def test_invalid_request_id(self):
        msg = valid_request(request_id="abc")
        self.assertCode("bad_request", decode_message, json.dumps(msg), now_ms=NOW)

    def test_uppercase_request_id_rejected(self):
        msg = valid_request(request_id="A" * 32)
        self.assertCode("bad_request", decode_message, json.dumps(msg), now_ms=NOW)

    def test_missing_source(self):
        msg = valid_request(); msg.pop("source")
        self.assertCode("bad_request", decode_message, json.dumps(msg), now_ms=NOW)

    def test_empty_peer_id(self):
        msg = valid_request(); msg["source"]["peer_id"] = ""
        self.assertCode("bad_request", decode_message, json.dumps(msg), now_ms=NOW)

    def test_invalid_nonce(self):
        msg = valid_request(nonce="00")
        self.assertCode("bad_request", decode_message, json.dumps(msg), now_ms=NOW)

    def test_invalid_timestamp(self):
        msg = valid_request(timestamp_ms="bad")
        self.assertCode("bad_request", decode_message, json.dumps(msg), now_ms=NOW)

    def test_old_timestamp(self):
        msg = valid_request(timestamp_ms=NOW - 30_001)
        self.assertCode("request_expired", decode_message, json.dumps(msg), now_ms=NOW)

    def test_future_timestamp(self):
        msg = valid_request(timestamp_ms=NOW + 10_001)
        self.assertCode("request_expired", decode_message, json.dumps(msg), now_ms=NOW)

    def test_invalid_ttl_zero(self):
        msg = valid_request(ttl_ms=0)
        self.assertCode("bad_request", decode_message, json.dumps(msg), now_ms=NOW)

    def test_invalid_ttl_too_large(self):
        msg = valid_request(ttl_ms=5001)
        self.assertCode("bad_request", decode_message, json.dumps(msg), now_ms=NOW)

    def test_unknown_service(self):
        msg = valid_request(service="admin.shell")
        self.assertCode("unsupported_service", decode_message, json.dumps(msg), now_ms=NOW)

    def test_payload_must_be_object(self):
        msg = valid_request(payload=[])
        self.assertCode("bad_request", decode_message, json.dumps(msg), now_ms=NOW)

    def test_request_too_large(self):
        msg = valid_request(payload={"x": "a" * MAX_REQUEST_BYTES})
        raw = json.dumps(msg).encode()
        self.assertGreater(len(raw), MAX_REQUEST_BYTES)
        self.assertCode("bad_request", decode_message, raw, now_ms=NOW)

    def test_response_status_must_be_success_range(self):
        req = valid_request()
        msg = new_response(req, source=DST, destination=SRC,
                           payload={}, timestamp_ms=NOW)
        msg["status"] = 500
        self.assertCode("bad_request", decode_message, json.dumps(msg), now_ms=NOW)

    def test_error_status_must_be_error_range(self):
        req = valid_request()
        msg = new_error(req, source=DST, destination=SRC,
                        code="bad_request", timestamp_ms=NOW)
        msg["status"] = 200
        self.assertCode("bad_request", decode_message, json.dumps(msg), now_ms=NOW)


class ReplayCacheTests(unittest.TestCase):
    class Clock:
        def __init__(self): self.value = 100.0
        def __call__(self): return self.value

    def test_first_seen_accepted_second_rejected(self):
        clock = self.Clock()
        cache = ReplayCache(window_sec=120, max_entries=10, clock=clock)
        rid = "01" * 16
        cache.check_and_add("peer-a", rid)
        with self.assertRaises(ProtocolError) as ctx:
            cache.check_and_add("peer-a", rid)
        self.assertEqual(ctx.exception.code, "replay_detected")

    def test_same_request_id_different_peer_is_distinct(self):
        cache = ReplayCache(window_sec=120, max_entries=10, clock=lambda: 1.0)
        rid = "02" * 16
        cache.check_and_add("peer-a", rid)
        cache.check_and_add("peer-b", rid)
        self.assertEqual(len(cache), 2)

    def test_entry_expires(self):
        clock = self.Clock()
        cache = ReplayCache(window_sec=10, max_entries=10, clock=clock)
        rid = "03" * 16
        cache.check_and_add("peer-a", rid)
        clock.value += 10.1
        cache.check_and_add("peer-a", rid)
        self.assertEqual(len(cache), 1)

    def test_cache_is_bounded(self):
        clock = self.Clock()
        cache = ReplayCache(window_sec=120, max_entries=3, clock=clock)
        for i in range(5):
            cache.check_and_add("peer-a", f"{i+1:032x}")
            clock.value += 0.1
        self.assertEqual(len(cache), 3)

    def test_clear(self):
        cache = ReplayCache(clock=lambda: 1.0)
        cache.check_and_add("peer-a", "04" * 16)
        cache.clear()
        self.assertEqual(len(cache), 0)

    def test_invalid_constructor(self):
        with self.assertRaises(ValueError): ReplayCache(window_sec=0)
        with self.assertRaises(ValueError): ReplayCache(max_entries=0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
