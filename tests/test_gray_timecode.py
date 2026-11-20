"""Offline Gray timecode protocol/decoder regressions (no Arduino/scope)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "oscilloscope"))
from decode_gray_timecode import (
    binary_to_gray, gray_to_binary, decode_pulses, decode_csv,
)
import test_gray_timecode as experiment


def synthetic_frames(indices, *, start=0.020):
    pulses = []
    for frame_pos, count in enumerate(indices):
        t0 = start + frame_pos * 0.1
        pulses.append((t0, 0.004))
        gray = binary_to_gray(count & 0xFFFF)
        for slot in range(16):
            if (gray >> (15 - slot)) & 1:
                pulses.append((t0 + 0.007 + 0.005 * slot, 0.001))
        pulses.append((t0 + 0.090, 0.007))
    return sorted(pulses)


class TimecodeTests(unittest.TestCase):
    def test_gray_roundtrip_and_single_bit_transitions(self):
        for n in (0, 1, 2, 3, 42, 4095, 65534, 65535):
            self.assertEqual(gray_to_binary(binary_to_gray(n)), n)
        for n in range(500):
            difference = binary_to_gray(n) ^ binary_to_gray(n+1)
            self.assertEqual(difference.bit_count(), 1)

    def test_decode_consecutive_and_wrap(self):
        frames = decode_pulses(synthetic_frames([65534, 65535, 0, 1]))
        self.assertEqual([x["counter_mod_65536"] for x in frames],
                         [65534, 65535, 0, 1])
        self.assertEqual([x["increment_from_previous"] for x in frames[1:]],
                         [1, 1, 1])
        self.assertTrue(all(abs(x["spacing_residual_s"]) < 1e-6
                            for x in frames[1:]))

    def test_decode_frame_gap(self):
        pulses = synthetic_frames([34, 35])
        pulses += [(t + 3.0, width) for t, width in
                   synthetic_frames([60, 61])]
        frames = decode_pulses(sorted(pulses))
        self.assertEqual([x["counter_mod_65536"] for x in frames],
                         [34, 35, 60, 61])
        self.assertEqual(frames[2]["increment_from_previous"], 25)

    def test_reject_malformed_frame_and_partial(self):
        pulses = synthetic_frames([7, 8])
        # Lose the mandatory END of the first frame, and truncate the second.
        malformed = [entry for entry in pulses
                     if not abs(entry[0] - (0.020 + 0.090)) < 1e-9]
        malformed = [entry for entry in malformed
                     if entry[0] < 0.120 + 0.040]
        self.assertEqual(decode_pulses(malformed), [])

    def test_csv_edge_detection_and_decode(self):
        # Two complete frames including a Gray pattern.
        pulses = synthetic_frames([42, 43], start=0.005)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ch4.csv"
            with path.open("w") as out:
                out.write("time_s,voltage_v,raw\n")
                for k in range(105000):
                    t = k * 2e-6
                    high = any(a <= t < a + width for a, width in pulses)
                    out.write(f"{t:.9f},{5 if high else 0},0\n")
            result = decode_csv(path)
        self.assertEqual([x["counter_mod_65536"] for x in result["frames"]],
                         [42, 43])

    def test_ch2_only_capture_plan(self):
        p = experiment.parser()
        args = p.parse_args(["--count", "1"])
        requested = experiment.plan(args, p)
        self.assertEqual(args.channels, [2])
        self.assertEqual(args.scales, {2: 2.0})
        self.assertEqual(args.trigger_channel, 2)
        self.assertEqual(args.trigger_edge, "rising")
        self.assertEqual(args.trigger_level, 1.0)
        self.assertEqual(requested["probe_comp"], "disconnected")

    def test_dry_run_does_not_open_serial(self):
        with patch.object(experiment, "run", side_effect=AssertionError("hardware")):
            experiment.main(["--count", "1"])
        with self.assertRaises(SystemExit):
            experiment.main(["--count", "0"])


if __name__ == "__main__":
    unittest.main()
