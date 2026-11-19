"""Offline verification of MCU frame 0x05 decoding and CSV recording.

Run from repository root: python -m unittest discover -s tests -v
Requires dependencies from requirements.txt (including pyserial).
"""
import csv
import struct
import tempfile
import unittest
import zlib

from communication.serial_manager import SerialManager
from recording.data_recorder import DataRecorder


def frame_05(timestamp=12345, taxels=None, ra_centi=None, sa_centi=None):
    taxels = list(range(12)) if taxels is None else taxels
    ra_centi = [0, 100, 1234, 10000, 25000, 100000, 500000, 655350, 500, 99, 7, 4294967295] if ra_centi is None else ra_centi
    sa_centi = [i * 125 for i in range(12)] if sa_centi is None else sa_centi
    payload = (struct.pack(">I", timestamp)
               + bytes(taxels)
               + struct.pack(">12I", *ra_centi)
               + struct.pack(">12I", *sa_centi))
    assert len(payload) == 112
    body = bytes([0x05]) + payload
    return struct.pack(">H", len(body)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


class TactileRateFrameTest(unittest.TestCase):
    def setUp(self):
        self.sm = SerialManager(config={"clock_compensation": {"enabled": False}})

    def feed(self, data):
        self.sm._rx_buffer.extend(data)
        self.sm._process_rx_buffer(987654321)

    def test_decode_rate_snapshot(self):
        self.feed(frame_05())
        kind, snapshot = self.sm.data_queue.get_nowait()
        self.assertEqual(kind, "tactile_rates")
        self.assertEqual(snapshot["raw_timestamp_ms"], 12345)
        self.assertEqual(snapshot["pc_arrival_monotonic_ns"], 987654321)
        self.assertEqual(snapshot["taxels"], list(range(12)))
        self.assertAlmostEqual(snapshot["ra_rates_hz"][2], 12.34)
        self.assertAlmostEqual(snapshot["ra_rates_hz"][11], 42949672.95)
        self.assertAlmostEqual(snapshot["sa_rates_hz"][11], 13.75)
        self.assertEqual(self.sm.data_queue.get_nowait(), ("link_ready",))
        self.assertEqual(self.sm.get_latest_rates_snapshot()["taxels"], list(range(12)))

    def test_fragmented_and_concatenated_frames_latest_overwrites(self):
        first = frame_05(100)
        second = frame_05(110, taxels=[255] * 12)
        self.feed(first[:22])
        self.assertTrue(self.sm.data_queue.empty())
        self.feed(first[22:] + second)
        self.assertEqual(self.sm.data_queue.get_nowait()[1]["raw_timestamp_ms"], 100)
        self.assertEqual(self.sm.data_queue.get_nowait(), ("link_ready",))
        self.assertEqual(self.sm.data_queue.get_nowait()[1]["raw_timestamp_ms"], 110)
        self.assertEqual(self.sm.get_latest_rates_snapshot()["taxels"], [255] * 12)

    def test_bad_crc_is_rejected_and_parser_resynchronizes(self):
        bad = bytearray(frame_05())
        bad[-1] ^= 1
        self.feed(bytes(bad) + frame_05(99))
        kind, sample = self.sm.data_queue.get_nowait()
        self.assertEqual(kind, "tactile_rates")
        self.assertEqual(sample["raw_timestamp_ms"], 99)

    def test_bad_crc_then_fragmented_valid_frame(self):
        bad = bytearray(frame_05(10))
        bad[-1] ^= 1
        valid = frame_05(20)
        # Corrupted frame, followed by only part of a valid one.
        self.feed(bytes(bad) + valid[:40])
        self.assertTrue(self.sm.data_queue.empty())
        self.feed(valid[40:])
        kind, sample = self.sm.data_queue.get_nowait()
        self.assertEqual(kind, "tactile_rates")
        self.assertEqual(sample["raw_timestamp_ms"], 20)

    def test_bad_crc_followed_by_noise_and_valid_frame(self):
        bad = bytearray(frame_05(10))
        bad[-1] ^= 1
        # A plausible but incomplete (257-byte) header inside corrupt data
        # must not block the following complete and valid frame.
        noise = bytes([0x01, 0x01, 0x55, 0xAA, 0x00])
        self.feed(bytes(bad) + noise + frame_05(33))
        kind, sample = self.sm.data_queue.get_nowait()
        self.assertEqual(kind, "tactile_rates")
        self.assertEqual(sample["raw_timestamp_ms"], 33)

    def test_wrong_size_is_rejected(self):
        body = bytes([0x05]) + bytes(111)
        malformed = struct.pack(">H", len(body)) + body + struct.pack(">I", zlib.crc32(body))
        self.feed(malformed + frame_05(456))
        kind, sample = self.sm.data_queue.get_nowait()
        self.assertEqual(kind, "tactile_rates")
        self.assertEqual(sample["raw_timestamp_ms"], 456)

    def test_latest_snapshot_is_copy(self):
        self.feed(frame_05())
        self.sm.data_queue.get_nowait()
        latest = self.sm.get_latest_rates_snapshot()
        latest["taxels"][0] = 255
        self.assertEqual(self.sm.get_latest_rates_snapshot()["taxels"][0], 0)

    def test_csv_recording(self):
        self.feed(frame_05())
        _, snapshot = self.sm.data_queue.get_nowait()
        with tempfile.TemporaryDirectory() as directory:
            recorder = DataRecorder(directory)
            recorder.start(timestamp="test")
            recorder.record_tactile_rates(snapshot)
            recorder.stop()
            with open(f"{directory}/test_tactile_rates.csv", newline="") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual(len(rows[0]), 39)
            self.assertEqual(rows[0][3], "taxel_0")
            self.assertEqual(rows[0][15], "ra_rate_hz_0")
            self.assertEqual(rows[0][27], "sa_rate_hz_0")
            self.assertEqual(rows[1][1], "12345")
            self.assertEqual(rows[1][3], "0")
            self.assertEqual(rows[1][5], "2")
            self.assertAlmostEqual(float(rows[1][17]), 12.34)


if __name__ == "__main__":
    unittest.main()
