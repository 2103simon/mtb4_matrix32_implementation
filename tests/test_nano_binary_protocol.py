"""Offline CRC32 Nano SYNC framing and text/binary interleaving tests."""
import struct
import unittest
from communication.nano_binary_protocol import (
    ACK_SIZE, MAGIC, REQUEST_SIZE, BinaryProtocolError,
    MixedNanoStreamDecoder, crc32, pack_sync_ack, pack_sync_request,
    unpack_sync_ack,
)


class NanoBinaryProtocolTests(unittest.TestCase):
    def test_wire_layout_and_crc32(self):
        request = pack_sync_request(0x12345678)
        self.assertEqual(len(request), REQUEST_SIZE)
        self.assertEqual(request[:8], MAGIC + bytes([1, 1, 0x78, 0x56, 0x34, 0x12]))
        self.assertEqual(struct.unpack("<I", request[-4:])[0], crc32(request[2:-4]))
        ack = pack_sync_ack(0x12345678, 123456, 123567)
        self.assertEqual(len(ack), ACK_SIZE)
        self.assertEqual(unpack_sync_ack(ack), (0x12345678, 123456, 123567))
        self.assertEqual(struct.unpack("<I", ack[-4:])[0], crc32(ack[2:-4]))

    def test_bad_crc_never_yields_ack(self):
        bad = bytearray(pack_sync_ack(2, 3, 4))
        bad[12] ^= 0x40
        with self.assertRaisesRegex(BinaryProtocolError, "CRC32"):
            unpack_sync_ack(bytes(bad))
        stream = MixedNanoStreamDecoder()
        self.assertEqual(stream.feed(bad), [])
        self.assertEqual(stream.bad_crc_count, 1)

    def test_fragmented_binary_and_text_preservation(self):
        stream = MixedNanoStreamDecoder()
        ack = pack_sync_ack(6, 100, 135)
        events = []
        wire = b"FRAME 2 GRAY 3 EDGE_US 1000 OVERRUNS 0\n" + ack + b"ACK STOP 7\n"
        for byte in wire:
            events.extend(stream.feed(bytes([byte])))
        self.assertEqual(events, [
            ("text", "FRAME 2 GRAY 3 EDGE_US 1000 OVERRUNS 0"),
            ("binary_sync_ack", (6, 100, 135)),
            ("text", "ACK STOP 7"),
        ])

    def test_corrupt_ack_does_not_consume_next_valid_ack(self):
        stream = MixedNanoStreamDecoder()
        first = bytearray(pack_sync_ack(1, 1, 2))
        first[-1] ^= 1
        good = pack_sync_ack(2, 4, 5)
        self.assertEqual(stream.feed(bytes(first) + good),
                         [("binary_sync_ack", (2, 4, 5))])
        self.assertEqual(stream.bad_crc_count, 1)

    def test_invalid_version_and_frame_type(self):
        ack = bytearray(pack_sync_ack(1, 2, 3))
        ack[2] = 2
        ack[-4:] = struct.pack("<I", crc32(ack[2:-4]))
        with self.assertRaisesRegex(BinaryProtocolError, "Unsupported"):
            unpack_sync_ack(bytes(ack))

    def test_field_range(self):
        with self.assertRaises(ValueError):
            pack_sync_request(-1)
        with self.assertRaises(ValueError):
            pack_sync_ack(1, 2**32, 0)


if __name__ == "__main__":
    unittest.main()
