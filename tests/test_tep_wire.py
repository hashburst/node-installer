import os
import socket
import unittest

from tep.hb_tep_app import Identity, new_request, new_response
from tep.hb_tep_wire import (
    PacketTypes,
    WireIntegrationError,
    UdpAppEndpoint,
    allocate_packet_types,
    merge_status_capabilities,
    validate_frozen_packet_types,
)

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    HAVE_CRYPTO = True
except Exception:
    HAVE_CRYPTO = False


class TestAesGcmCodec:
    """Test-only encrypted codec. Not a replacement for production hb_tep.py crypto."""
    def __init__(self, key: bytes):
        self._aes = AESGCM(key)

    def encode_packet(self, packet_type: int, plaintext: bytes) -> bytes:
        nonce = os.urandom(12)
        header = bytes([packet_type])
        return header + nonce + self._aes.encrypt(nonce, plaintext, b"hb-tep-step5-test")

    def decode_packet(self, datagram: bytes) -> tuple[int, bytes]:
        if len(datagram) < 14:
            raise ValueError("short datagram")
        packet_type = datagram[0]
        nonce = datagram[1:13]
        plaintext = self._aes.decrypt(nonce, datagram[13:], b"hb-tep-step5-test")
        return packet_type, plaintext


class PacketTypeTests(unittest.TestCase):
    def test_allocator_avoids_known_and_declared_types(self):
        types = allocate_packet_types({0x01, 0x20, 0x21})
        self.assertEqual([0x22, 0x23, 0x24, 0x25, 0x26], list(types.as_dict().values()))

    def test_allocator_fails_when_candidates_exhausted(self):
        with self.assertRaises(WireIntegrationError) as ctx:
            allocate_packet_types({1, 2}, candidates=[1, 2, 3])
        self.assertEqual("packet_type_exhausted", ctx.exception.code)

    def test_validate_frozen_detects_daemon_collision(self):
        frozen = PacketTypes(0x20, 0x21, 0x22, 0x23, 0x24)
        with self.assertRaises(WireIntegrationError) as ctx:
            validate_frozen_packet_types(frozen, {0x01, 0x22})
        self.assertEqual("packet_type_collision", ctx.exception.code)

    def test_status_capabilities_are_additive(self):
        out = merge_status_capabilities({"node_id": "n1", "peers": []}, relay_enabled=False)
        self.assertEqual("n1", out["node_id"])
        self.assertEqual(["HB-TEP-APP/1"], out["app_protocols"])
        self.assertEqual(["storage.summary"], out["services"])
        self.assertFalse(out["relay"])


@unittest.skipUnless(HAVE_CRYPTO, "cryptography is required for encrypted localhost test")
class LocalhostEncryptedUdpTests(unittest.TestCase):
    def setUp(self):
        self.types = allocate_packet_types({0x01})
        self.codec = TestAesGcmCodec(bytes(range(32)))
        self.client_id = Identity("aggregator", "peer-aggregator")
        self.server_id = Identity("node-7", "peer-node7")
        self.server = None
        self.client = None

    def tearDown(self):
        if self.client:
            self.client.close()
        if self.server:
            self.server.close()

    def _make_endpoints(self):
        def handler(req):
            return new_response(
                req,
                source=self.server_id,
                destination=self.client_id,
                payload={"node_id": "node-7", "role": "edge", "available": True,
                         "capacity_total_gb": 200, "used_gb": 10, "timestamp": 1786500000},
            )
        self.server = UdpAppEndpoint(bind_host="127.0.0.1", bind_port=0, codec=self.codec,
                                     packet_types=self.types, request_handler=handler)
        self.client = UdpAppEndpoint(bind_host="127.0.0.1", bind_port=0, codec=self.codec,
                                     packet_types=self.types)
        self.server.start_request_server()

    def test_encrypted_request_response_over_udp_loopback(self):
        self._make_endpoints()
        req = new_request(source=self.client_id, destination=self.server_id,
                          service="storage.summary", payload={})
        datagram = self.client.send_message(req, self.server.address)
        self.assertNotIn(b"storage.summary", datagram)
        self.assertNotIn(b"peer-node7", datagram)
        response, _, response_datagram = self.client.recv_message()
        self.assertEqual(req["request_id"], response["request_id"])
        self.assertEqual("res", response["type"])
        self.assertEqual("edge", response["payload"]["role"])
        self.assertNotIn(b"storage.summary", response_datagram)
        self.assertNotIn(b"node-7", response_datagram)

    def test_packet_type_envelope_mismatch_rejected(self):
        self.client = UdpAppEndpoint(bind_host="127.0.0.1", bind_port=0, codec=self.codec,
                                     packet_types=self.types)
        req = new_request(source=self.client_id, destination=self.server_id,
                          service="storage.summary", payload={})
        from tep.hb_tep_app import encode_message
        bad = self.codec.encode_packet(self.types.app_response, encode_message(req))
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sender.sendto(bad, self.client.address)
            with self.assertRaises(WireIntegrationError) as ctx:
                self.client.recv_message()
            self.assertEqual("packet_envelope_mismatch", ctx.exception.code)
        finally:
            sender.close()

    def test_non_loopback_bind_rejected(self):
        with self.assertRaises(WireIntegrationError) as ctx:
            UdpAppEndpoint(bind_host="0.0.0.0", bind_port=0, codec=self.codec, packet_types=self.types)
        self.assertEqual("unsafe_bind", ctx.exception.code)


if __name__ == "__main__":
    unittest.main()
