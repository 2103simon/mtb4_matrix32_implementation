"""Device-free buffered 10Hz Nano acquisition protocol tests."""
import unittest

from oscilloscope.test_gray_timecode import NanoLog, parser, plan


def frame(counter, edge_us, *, gray=None):
    binary = counter & 0xFFFF
    gray = binary ^ (binary >> 1) if gray is None else gray
    return f"FRAME {counter} GRAY {gray:X} EDGE_US {edge_us} OVERRUNS 0"


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
        self.dump_frames = [frame(i, 1_000_000 + i * 100_000)
                            for i in range(self.deliver)]
        self.premature_frames = []
        self.replies = {}

    def command(self, op, *, timeout=5.0):
        self.commands.append(op)
        if op == "BUFFER_BEGIN":
            return self.replies.get(op, "ACK BUFFER_BEGIN 1 CAP 256")
        if op == "BUFFER_END":
            self.frame_lines.extend(self.premature_frames)
            return self.replies.get(
                op, f"ACK BUFFER_END 2 COUNT {self.count} LOST {self.lost} CAP 256")
        if op == "BUFFER_DUMP":
            self.frame_lines.extend(self.dump_frames)
            return self.replies.get(
                op, f"ACK BUFFER_DUMP 3 COUNT {self.count} LOST {self.lost}")
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
        self.assertTrue(end["frame_payloads_validated"])
        self.assertTrue(end["frame_counters_consecutive"])
        self.assertTrue(end["frame_timestamps_ordered"])
        self.assertEqual(end["first_frame_counter"], 0)
        self.assertEqual(end["last_frame_counter"], 200)
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

    def test_zero_loss_end_ack_cannot_hide_dump_loss(self):
        nano = FakeNano(count=3)
        nano.replies["BUFFER_DUMP"] = "ACK BUFFER_DUMP 3 COUNT 3 LOST 1"
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            nano.buffered_capture_end_and_dump(nano.buffered_capture_begin())

    def test_matching_counts_do_not_accept_bad_frame_evidence(self):
        cases = (
            ("FRAME simulated", "Malformed"),
            (frame(1, 1_100_000, gray=0), "Gray/counter mismatch"),
            (frame(0, 1_100_000), "not consecutive"),
            (frame(2, 1_100_000), "not consecutive"),
            (frame(1, 1_000_000), "timestamps are not increasing"),
            (frame(1, 900_000), "timestamps are not increasing"),
            (frame(1, 1 << 32), "out of range"),
        )
        for corrupt, reason in cases:
            with self.subTest(frame=corrupt):
                nano = FakeNano(count=3)
                nano.dump_frames[1] = corrupt
                with self.assertRaisesRegex(RuntimeError, reason):
                    nano.buffered_capture_end_and_dump(nano.buffered_capture_begin())
                # Validation must retain the received evidence unchanged.
                self.assertEqual(nano.frame_lines[1], corrupt)

    def test_actual_micros_rollover_is_accepted_without_rewriting(self):
        nano = FakeNano(count=3)
        nano.dump_frames = [
            frame(41, 4_294_900_000), frame(42, 32704), frame(43, 132704),
        ]
        original = list(nano.dump_frames)
        result = nano.buffered_capture_end_and_dump(nano.buffered_capture_begin())
        self.assertEqual(result["timestamp_wraps"], 1)
        self.assertEqual(nano.frame_lines, original)

    def test_routine_frame_during_acquisition_is_rejected_after_dump(self):
        nano = FakeNano(count=3)
        nano.premature_frames = [frame(42, 123456)]
        with self.assertRaisesRegex(RuntimeError, "during buffered acquisition"):
            nano.buffered_capture_end_and_dump(nano.buffered_capture_begin())
        self.assertIn("BUFFER_DUMP", nano.commands)
        self.assertEqual(nano.frame_lines, nano.premature_frames + nano.dump_frames)

    def test_malformed_or_negative_buffer_ack_is_rejected(self):
        for op, reply in (
            ("BUFFER_BEGIN", "ACK BUFFER_BEGIN 1 CAP"),
            ("BUFFER_BEGIN", "ACK BUFFER_BEGIN 1 CAP -1"),
            ("BUFFER_END", "ACK BUFFER_END 2 COUNT -1 LOST 0 CAP 256"),
            ("BUFFER_END", "ACK BUFFER_END 2 COUNT 3 LOST -1 CAP 256"),
            ("BUFFER_DUMP", "ACK BUFFER_DUMP 3 COUNT bad LOST 0"),
        ):
            with self.subTest(op=op, reply=reply):
                nano = FakeNano(count=3)
                nano.replies[op] = reply
                with self.assertRaises(RuntimeError):
                    nano.buffered_capture_end_and_dump(nano.buffered_capture_begin())

    def test_observed_lifecycle_acks_survive_validation_failure(self):
        for op, reply in (
            ("BUFFER_END", "ACK BUFFER_END 2 COUNT malformed"),
            ("BUFFER_DUMP", "ACK BUFFER_DUMP 3 COUNT malformed"),
        ):
            with self.subTest(op=op):
                nano = FakeNano(count=3)
                nano.replies[op] = reply
                begun = nano.buffered_capture_begin()
                with self.assertRaises(RuntimeError):
                    nano.buffered_capture_end_and_dump(begun)
                self.assertIn("end_ack", begun)
                self.assertEqual(begun["end_ack" if op == "BUFFER_END" else "dump_ack"],
                                 reply)
                self.assertEqual(nano.commands.count("BUFFER_END"), 1)

    def test_end_ack_survives_dump_transport_failure(self):
        nano = FakeNano(count=3)
        command = nano.command

        def fail_dump(op, *, timeout=5.0):
            if op == "BUFFER_DUMP":
                raise TimeoutError("dump transport failed")
            return command(op, timeout=timeout)

        nano.command = fail_dump
        begun = nano.buffered_capture_begin()
        with self.assertRaisesRegex(TimeoutError, "dump transport failed"):
            nano.buffered_capture_end_and_dump(begun)
        self.assertEqual(begun["end_ack"],
                         "ACK BUFFER_END 2 COUNT 3 LOST 0 CAP 256")
        self.assertNotIn("dump_ack", begun)


if __name__ == "__main__":
    unittest.main()
