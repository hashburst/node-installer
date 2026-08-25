import json
import unittest

from tep import hb_tep_app as app
from tep import hb_tep_runtime_ha  # noqa: F401 - enables ha.lease in this runtime process
from tep.hb_tep_ha_service import HA_LEASE_SERVICE, HaLeaseHandler
from tep.hb_tep_services import ServiceError


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size=-1):
        return self._raw if size < 0 else self._raw[:size]


class TepHaProtocolTests(unittest.TestCase):
    def request(self, payload):
        return app.new_request(
            source=app.Identity(node_id="master-node", peer_id="peer-master"),
            destination=app.Identity(node_id="blockchainapi.one", peer_id="peer-witness"),
            service=HA_LEASE_SERVICE,
            payload=payload,
            ttl_ms=2500,
        )

    def test_ha_runtime_extends_app_service_allowlist(self):
        raw = app.encode_message(
            self.request({"op": "status", "cluster_id": "hashburst-production"})
        )
        envelope = app.decode_message(raw, check_time=False)
        self.assertEqual(envelope.service, HA_LEASE_SERVICE)

    def test_handler_preserves_authenticated_source_identity(self):
        captured = {}

        def opener(request, timeout):
            captured.update(json.loads(request.data.decode("utf-8")))
            self.assertLessEqual(timeout, 5)
            return FakeResponse({"ok": True, "result": {"term": 4, "holder": "master-node"}})

        handler = HaLeaseHandler(opener=opener)
        envelope = app.validate_envelope(
            self.request({"op": "status", "cluster_id": "hashburst-production"}),
            check_time=False,
        )
        result = handler.dispatch_envelope(envelope)
        self.assertEqual(result["term"], 4)
        self.assertEqual(captured["source"], {
            "node_id": "master-node",
            "peer_id": "peer-master",
        })
        self.assertEqual(captured["payload"]["cluster_id"], "hashburst-production")

    def test_payload_only_handler_fails_closed(self):
        with self.assertRaises(ServiceError) as ctx:
            HaLeaseHandler()({"op": "status", "cluster_id": "hashburst-production"})
        self.assertEqual(ctx.exception.code, "identity_context_required")

    def test_unexpected_lease_field_is_rejected(self):
        envelope = app.validate_envelope(
            self.request({
                "op": "status",
                "cluster_id": "hashburst-production",
                "command": "systemctl restart anything",
            }),
            check_time=False,
        )
        with self.assertRaises(app.ProtocolError) as ctx:
            HaLeaseHandler().dispatch_envelope(envelope)
        self.assertEqual(ctx.exception.code, "bad_request")


if __name__ == "__main__":
    unittest.main()
