#!/usr/bin/env python3
"""Regression vectors for the former rstrip(b'\\r\\n--') corruption bug."""
import unittest

def extract(content: bytes) -> bytes:
    if content.startswith(b'\r\n'): content=content[2:]
    if content.endswith(b'\r\n'): content=content[:-2]
    return content

class MultipartByteTests(unittest.TestCase):
    def test_trailing_bytes_preserved(self):
        for tail in [b'-', b'--', b'\r', b'\n', b'\r\n-', bytes([0]), bytes([255])]:
            blob=b'ciphertext'+tail
            framed=b'\r\n'+blob+b'\r\n'
            self.assertEqual(extract(framed), blob)

if __name__ == '__main__': unittest.main(verbosity=2)
