"""Device-free tests of per-record Nano SYNC epochs and safe bench plan."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "oscilloscope"))
from analyze_gray_epoch_experiment import analyze
from test_gray_timecode import parser, plan
from test_gray_epoch_pause import main as epoch_main


def make_recording(root, *, broken_post=False):
    root = Path(root)
    metadata = {
        "run_id": "synthetic_epochs",
        "plan": {"experiment": "GRAY_TC_V1", "per_record_epochs": True},
        "records": [],
    }
    lines = []
    scope_records = []
    anchor_host = 10**13
    seq = 1
    for idx in (1, 2, 3):
        # 30 pre requests (7.5 s), nominal 30 s capture, 30 post requests
        # after a deliberate pause in the monotonically increasing Nano clock.
        epoch_start_us = idx * 110_000_000
        nano_times = (
            [epoch_start_us + k*250_000 for k in range(30)]
            + [epoch_start_us + 38_000_000 + k*250_000 for k in range(30)]
        )
        pre_ids, post_ids = [], []
        for k, rx_us in enumerate(nano_times):
            tx_us = rx_us + 110
            t2 = anchor_host + round(rx_us*999.4)
            t3 = anchor_host + round(tx_us*999.4)
            t1 = t2 - 1_500_000
            t4 = t3 + 1_500_000
            row = {
                "type": "sync_exchange", "transport": "binary_crc32_v1",
                "sequence_id": seq,
                "host_send_monotonic_ns": t1,
                "host_receive_monotonic_ns": t4,
                "nano_receive_us": rx_us,
                "nano_transmit_us": tx_us,
            }
            lines.append(json.dumps(row))
            (pre_ids if k < 30 else post_ids).append(seq)
            seq += 1
        arm = anchor_host + round((epoch_start_us + 7_500_000)*999.4)
        done = anchor_host + round((epoch_start_us + 37_900_000)*999.4)
        if broken_post and idx == 2:
            done += 2_000_000_000
        metadata["records"].append({
            "index": idx, "status": "captured",
            "clock_epoch": {
                "epoch_id": idx,
                "pre_sync_sequence_ids": pre_ids,
                "post_sync_sequence_ids": post_ids,
            },
            "arm_request_monotonic_ns": arm,
            "acquisition_completed_monotonic_ns": done,
        })
        scope_records.append({
            "record_index": idx,
            "clock_fit": {"edge_fit_rms_us": 20.0},
            "nano_time_at_first_sample_s": (epoch_start_us+8_000_000)/1e6,
            "nano_time_at_last_sample_s": (epoch_start_us+38_000_000)/1e6,
        })
    (root / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (root / "nano_serial.jsonl").write_text("\n".join(lines)+"\n",
                                             encoding="utf-8")
    return {
        "experiment": "GRAY_TC_V1", "records": scope_records,
        "decoded_frames_total": 850, "source_record_count": 3,
        "all_fit_rms_below_threshold": True,
    }


class EpochExperimentTests(unittest.TestCase):
    def test_three_independent_clock_fits(self):
        with tempfile.TemporaryDirectory() as td:
            scope_alignment = make_recording(td)
            with patch("analyze_gray_epoch_experiment.read_nano_frames",
                       return_value={}), patch(
                           "analyze_gray_epoch_experiment.build_alignment",
                           return_value=scope_alignment):
                alignment, report = analyze(td)
            self.assertEqual(alignment["source_record_count"], 3)
            self.assertEqual(len(report["records"]), 3)
            self.assertEqual(report["qualified_provisional_records"], 3)
            seen = set()
            for row in report["records"]:
                self.assertEqual(row["pre_sync_count"], 30)
                self.assertEqual(row["post_sync_count"], 30)
                self.assertEqual(row["status"], "provisional_local_fit")
                self.assertLess(row["fit"]["rms_residual_us"], 100)
                self.assertTrue(seen.isdisjoint(row["sync_sequence_ids"]))
                seen.update(row["sync_sequence_ids"])
                self.assertLess(
                    row["first_sample_host_monotonic_ns_est"],
                    row["last_sample_host_monotonic_ns_est"],
                )
            self.assertEqual(len(seen), 180)
            self.assertFalse(report["host_clock_synchronization_verified"])

    def test_reject_sync_overlap_with_capture(self):
        with tempfile.TemporaryDirectory() as td:
            scope_alignment = make_recording(td, broken_post=True)
            with patch("analyze_gray_epoch_experiment.read_nano_frames",
                       return_value={}), patch(
                           "analyze_gray_epoch_experiment.build_alignment",
                           return_value=scope_alignment):
                with self.assertRaisesRegex(ValueError, "post-SYNC overlaps"):
                    analyze(td)

    def test_default_bench_plan_never_opens_hardware(self):
        with patch("test_gray_epoch_pause.gray_main") as wrapped:
            epoch_main([])
        argv = wrapped.call_args.args[0]
        self.assertEqual(argv[argv.index("--count")+1], "3")
        self.assertEqual(argv[argv.index("--pause-between-s")+1], "60")
        self.assertIn("--per-record-epochs", argv)
        self.assertNotIn("--run", argv)

    def test_epoch_scope_plan_validated(self):
        p = parser()
        a = p.parse_args([
            "--count", "3", "--points", "1000000",
            "--time-div-us", "3000000", "--acquire-timeout", "90",
            "--per-record-epochs", "--epoch-sync-count", "30",
            "--pause-between-s", "60",
        ])
        requested = plan(a, p)
        self.assertAlmostEqual(requested["nominal_window_s"], 30)
        self.assertEqual(requested["count"], 3)
        self.assertTrue(requested["per_record_epochs"])
        self.assertEqual(requested["pause_between_s"], 60)


if __name__ == "__main__":
    unittest.main()
