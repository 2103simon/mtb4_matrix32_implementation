"""Device-free buffered 10Hz Nano acquisition protocol tests."""
import unittest

from oscilloscope.test_gray_timecode import NanoLog, parser, plan


class FakeNano:
    """Minimal command stub: no serial port or real hardware."""
    buffered_capture_begin = NanoLog.buffered_capture_begin
    buffered_capture_end_and_dump = NanoLog.buffered_capture_end_and_dump

    def __init__(self, count=201, lost=0, deliver=None):
        self.frame_lines = []
        self.commands = []
        self.count = count
        self.lost = lost
        self.deliver = count if deliver is None else deliver

    def command(self, op, *, timeout=5.0):
        self.commands.append(op)
        if op == "BUFFER_BEGIN":
            return "ACK BUFFER_BEGIN 1 CAP 256"
        if op == "BUFFER_END":
            return f"ACK BUFFER_END 2 COUNT {self.count} LOST {self.lost} CAP 256"
        if op == "BUFFER_DUMP":
            self.frame_lines.extend(["FRAME simulated"]*self.deliver)
            return f"ACK BUFFER_DUMP 3 COUNT {self.count} LOST {self.lost}"
        raise ValueError(op)


class BufferedTimingTests(unittest.TestCase):
    def test_profile_is_explicit_10hz_and_non_stimulating(self):
        p = parser()
        args = p.parse_args([
            "--expected-frame-us", "100000",
            "--expected-frame-mode", "10HZ_BUFFERED",
            "--buffered-frames", "--points", "1000000",
            "--time-div-us", "2000000",
        ])
        details = plan(args, p)
        self.assertEqual(details["expected_frame_mode"], "10HZ_BUFFERED")
        self.assertEqual(details["nominal_frame_s"], .1)
        self.assertTrue(details["buffered_frames"])
        self.assertTrue(details["no_stimulation"])

    def test_buffer_flag_cannot_be_used_on_other_profile(self):
        p = parser()
        with self.assertRaises(SystemExit):
            plan(p.parse_args(["--buffered-frames"]), p)
        with self.assertRaises(SystemExit):
            plan(p.parse_args([
                "--expected-frame-mode", "10HZ_BUFFERED",
            ]), p)

    def test_dump_verifies_all_frames_and_no_lost(self):
        nano = FakeNano()
        begin = nano.buffered_capture_begin()
        end = nano.buffered_capture_end_and_dump(begin)
        self.assertEqual(end["frames_received"], 201)
        self.assertEqual(end["frames_lost"], 0)
        self.assertEqual(nano.commands, [
            "BUFFER_BEGIN", "BUFFER_END", "BUFFER_DUMP",
        ])

    def test_missing_dumped_frames_fails_closed(self):
        nano = FakeNano(count=201, deliver=200)
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            nano.buffered_capture_end_and_dump(nano.buffered_capture_begin())

    def test_overflow_fails_closed(self):
        nano = FakeNano(count=256, lost=1)
        with self.assertRaisesRegex(RuntimeError, "overflow"):
            nano.buffered_capture_end_and_dump(nano.buffered_capture_begin())

    def test_corrupt_count_fails_closed(self):
        nano = FakeNano(count=300)
        with self.assertRaisesRegex(RuntimeError, "exceeds capacity"):
            nano.buffered_capture_end_and_dump(nano.buffered_capture_begin())


if __name__ == "__main__":
    unittest.main()
