import struct
import unittest
from unittest import mock

from probe_transport import MAX_FRAME_BYTES, Client, TransportError, unmask


class FakeSock:
    """Minimal recv() stand-in; returns pre-split chunks, then EOF."""

    def __init__(self, chunks):
        self.chunks = list(chunks)

    def recv(self, size):
        if not self.chunks:
            return b''
        chunk = self.chunks.pop(0)
        if len(chunk) > size:
            self.chunks.insert(0, chunk[size:])
            chunk = chunk[:size]
        return chunk


def client_with(chunks):
    client = object.__new__(Client)
    client.buf = b''
    client.sock = FakeSock(chunks)
    return client


def server_frame(payload, opcode=0x1):
    n = len(payload)
    header = bytes([0x80 | opcode])
    if n < 126:
        header += bytes([n])
    elif n < 65536:
        header += bytes([126]) + struct.pack('!H', n)
    else:
        header += bytes([127]) + struct.pack('!Q', n)
    return header + payload


class UnmaskTests(unittest.TestCase):
    def test_matches_reference_implementation(self):
        payload = bytes(range(256)) * 3
        mask = b'\x01\x02\x03\x04'
        expected = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.assertEqual(unmask(payload, mask), expected)

    def test_handles_partial_mask_cycles(self):
        for size in (1, 2, 3, 5, 7, 4097):
            payload = bytes((i * 37) % 256 for i in range(size))
            mask = b'\xff\x00\xaa\x55'
            expected = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            self.assertEqual(unmask(payload, mask), expected)

    def test_empty_payload(self):
        self.assertEqual(unmask(b'', b'\x01\x02\x03\x04'), b'')


class FrameLimitTests(unittest.TestCase):
    def test_limit_is_large_enough_for_server_notifications(self):
        self.assertGreater(MAX_FRAME_BYTES, 16 * 1024 * 1024)


class TakeTests(unittest.TestCase):
    def test_joins_chunked_reads_linearly(self):
        client = client_with([b'ab', b'cd', b'efgh'])
        self.assertEqual(client.take(6), b'abcdef')
        self.assertEqual(client.buf, b'gh')

    def test_prefers_existing_buffer(self):
        client = client_with([b'zzz'])
        client.buf = b'12345'
        self.assertEqual(client.take(3), b'123')
        self.assertEqual(client.take(2), b'45')

    def test_raises_on_eof(self):
        client = client_with([])
        with self.assertRaises(TransportError):
            client.take(1)


class FrameTests(unittest.TestCase):
    def test_accepts_frame_larger_than_old_16mib_cap(self):
        payload = b'{"id":1,"result":{"blob":"' + b'x' * (17 * 1024 * 1024) + b'"}}'
        frame = server_frame(payload)
        chunks = [frame[i:i + 65536] for i in range(0, len(frame), 65536)]
        self.assertEqual(client_with(chunks).receive_message(), payload.decode())

    def test_rejects_frame_above_current_cap(self):
        frame = server_frame(b'x' * 4096)
        with mock.patch('probe_transport.MAX_FRAME_BYTES', 1024):
            with self.assertRaises(TransportError):
                client_with([frame]).receive_message()


if __name__ == '__main__':
    unittest.main()
