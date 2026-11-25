"""Offline Nano Gray clock mapping regressions; no physical hardware."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "oscilloscope"))
from align_gray_records import read_nano_frames, fit_affine, build_alignment


def fixtures():
    frames = {}
    records = []
    for i, start in enumerate((4, 98, 192), 1):
        series = []
        for j in range(19):
            n = start + j
            binary = n & 0xFFFF
            gray = binary ^ (binary >> 1)
            nano_us = 1000000 + n * 100000
            frames[n] = {
                "counter": n, "gray_hex": f"{gray:04X}",
                "nano_unwrapped_us": nano_us, "nano_edge_us": nano_us,
                "overruns": 0, "host_received_monotonic_ns": None,
            }
            # Known 500 ppm scope-vs-Nano clock skew.
            scope_time = ((nano_us / 1e6 - (1 + start * 0.1)) / 1.0005)
            series.append({"scope_start_s": scope_time,
                           "counter_mod_65536": n,
                           "gray_hex": f"{gray:04X}"})
        records.append({
            "status": "captured",
            "decoded_gray": {"frames": series},
            "channels": {"CH2": {
                "path": f"waveforms/record_{i:05d}_ch2.csv",
                "transfer": {"waveform_preamble": {
                    "xzero_s": -1., "xincr_s": 2e-6, "ptoff": 0.
                }},
                "waveform": {"samples": 1000000}
            }}
        })
    return {"plan": {"experiment": "GRAY_TC_V1"}, "records": records}, frames


class GrayAlignmentTests(unittest.TestCase):
    def test_fit_affine_known_ratio(self):
        fit = fit_affine([(0., 1.), (1., 2.0005), (2., 3.001)])
        self.assertAlmostEqual(fit["slope_nano_s_per_scope_s"], 1.0005)
        self.assertLess(fit["edge_fit_rms_us"], 1e-5)

    def test_align_three_discontinuous_records(self):
        metadata, frames = fixtures()
        result = build_alignment(metadata, frames)
        self.assertEqual(result["decoded_frames_total"], 57)
        self.assertEqual(
            [r["gap_from_previous"]["missing_complete_frame_ids_between"]
             for r in result["records"][1:]], [75, 75])
        self.assertTrue(all(abs(r["nano_seconds_per_scope_second"] - 1.0005) < 1e-8
                            for r in result["records"]))
        self.assertFalse(result["physical_sample_gaps_recovered"])
        self.assertFalse(result["host_clock_synchronized"])

    def test_missing_serial_counter_rejected(self):
        metadata, frames = fixtures()
        del frames[98]
        with self.assertRaisesRegex(ValueError, "serial matches"):
            build_alignment(metadata, frames)

    def test_duplicate_modulo_match_rejected(self):
        metadata, frames = fixtures()
        duplicate = dict(frames[4])
        duplicate["counter"] = 4 + 65536
        frames[4 + 65536] = duplicate
        with self.assertRaisesRegex(ValueError, "serial matches"):
            build_alignment(metadata, frames)

    def test_bad_serial_gray_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nano_serial.jsonl"
            path.write_text(json.dumps({
                "received_monotonic_ns": 1000,
                "line": "FRAME 4 GRAY 0000 EDGE_US 100000 OVERRUNS 0"
            }) + "\n")
            with self.assertRaisesRegex(ValueError, "mismatch"):
                read_nano_frames(path)

    def test_rollover_of_nano_micros(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nano_serial.jsonl"
            lines = []
            for n, t in [(41, 4294900000), (42, 32704), (43, 132704)]:
                gray = n ^ (n >> 1)
                lines.append(json.dumps({
                    "received_monotonic_ns": n * 100000000,
                    "line": f"FRAME {n} GRAY {gray:04X} EDGE_US {t} OVERRUNS 0"
                }))
            path.write_text("\n".join(lines) + "\n")
            frames = read_nano_frames(path)
            self.assertEqual(frames[42]["nano_unwrapped_us"], 4295000000)
            self.assertEqual(frames[43]["nano_unwrapped_us"], 4295100000)


if __name__ == "__main__":
    unittest.main()
