"""Device-free tests of per-record Nano SYNC epochs and safe bench plan."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.append(str(Path(__file__).resolve().parents[1] / "oscilloscope"))
from analyze_gray_epoch_experiment import analyze
from align_gray_records import build_alignment, fit_affine
from oscilloscope.test_gray_timecode import parser, plan
from record_scope import configure, verify
from oscilloscope import test_gray_epoch_pause as _bench_module

epoch_main = _bench_module.main


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
            "edge_fit_rms_below_threshold": True,
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
    def test_local_fit_exploratory_when_scope_rms_above_original_limit(self):
        with tempfile.TemporaryDirectory() as td:
            scope_alignment = make_recording(td)
            scope_alignment["records"][0]["clock_fit"]["edge_fit_rms_us"] = 102.0
            with patch("analyze_gray_epoch_experiment.read_nano_frames",
                       return_value={}), patch(
                           "analyze_gray_epoch_experiment.build_alignment",
                           return_value=scope_alignment):
                _, report = analyze(td, max_scope_rms_us=150)
            self.assertEqual(report["qualified_provisional_records"], 2)
            self.assertEqual(report["exploratory_local_fit_records"], 1)
            self.assertFalse(
                report["scope_to_nano_alignment"]["original_100us_standard_met"]
            )
            row = report["records"][0]
            self.assertEqual(row["status"], "exploratory_local_fit")
            self.assertIn("fit", row)

    def test_scope_rms_gate_does_not_hide_clock_diagnostics(self):
        with tempfile.TemporaryDirectory() as td:
            scope_alignment = make_recording(td)
            scope_alignment["records"][0]["clock_fit"]["edge_fit_rms_us"] = 102.0
            scope_alignment["records"][0]["edge_fit_rms_below_threshold"] = False
            with patch("analyze_gray_epoch_experiment.read_nano_frames",
                       return_value={}), patch(
                           "analyze_gray_epoch_experiment.build_alignment",
                           return_value=scope_alignment):
                _, report = analyze(td)
            self.assertEqual(report["records"][0]["status"], "unqualified")
            self.assertIn("fit", report["records"][0])
            self.assertEqual(report["qualified_provisional_records"], 2)
            self.assertTrue(any("exceeds analysis threshold" in warning
                for warning in report["records"][0]["warnings"]))

    def test_left_edge_trigger_scpi_order_and_readback(self):
        p = parser()
        args = p.parse_args([])
        plan(args, p)
        self.assertEqual(args.trigger_position_pct, 0)
        with patch("record_scope.write") as write:
            configure(None, args)
        commands = [call.args[1] for call in write.call_args_list]
        self.assertLess(commands.index("HORizontal:RECOrdlength 100000"),
                        commands.index("HORizontal:POSition 0"))
        self.assertLess(commands.index("HORizontal:SCALe 0.02"),
                        commands.index("HORizontal:POSition 0"))
        self.assertEqual(
            commands[commands.index("HORizontal:POSition 0")-1],
            "HORizontal:DELay:MODe OFF",
        )
        settings = {
            "identity": "TEKTRONIX,MDO4104B-6",
            "time_div_s": "0.02",
            "record_length": "100000",
            "sample_rate": "500.0000E+3",
            "acquisition_mode": "SAMple",
            "acquisition_stopafter": "SEQuence",
            "fast_acquisition": "OFF",
            "trigger_mode": "NORM",
            "trigger_source": "CH2",
            "trigger_slope": "RIS",
            "trigger_level_v": "1.0",
            "horizontal_delay_mode": "OFF",
            "trigger_position_pct": "0",
            "ch2_v_div": "2.0",
            "ch2_enabled": "ON",
        }
        verify(settings, args)
        settings["trigger_position_pct"] = "50"
        with self.assertRaisesRegex(RuntimeError, "Horizontal trigger position"):
            verify(settings, args)
        settings["trigger_position_pct"] = "0"
        settings["horizontal_delay_mode"] = "ON"
        with self.assertRaisesRegex(RuntimeError, "Horizontal delay"):
            verify(settings, args)

    def test_trigger_position_override_and_bad_values(self):
        p = parser()
        args = p.parse_args(["--trigger-position-pct", "50"])
        plan(args, p)
        self.assertEqual(args.trigger_position_pct, 50)
        for invalid in ("-1", "101", "nan"):
            with self.subTest(value=invalid):
                args = p.parse_args(["--trigger-position-pct", invalid])
                with self.assertRaises(SystemExit):
                    plan(args, p)

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
        with patch.object(_bench_module, "gray_main") as wrapped:
            epoch_main([])
        argv = wrapped.call_args.args[0]
        self.assertEqual(argv[argv.index("--count")+1], "3")
        self.assertEqual(argv[argv.index("--pause-between-s")+1], "60")
        self.assertEqual(argv[argv.index("--time-div-us")+1], "2000000")
        self.assertEqual(argv[argv.index("--trigger-position-pct")+1], "0")
        self.assertIn("--per-record-epochs", argv)
        self.assertNotIn("--run", argv)

    def test_epoch_scope_plan_validated(self):
        p = parser()
        a = p.parse_args([
            "--count", "3", "--points", "1000000",
            "--time-div-us", "2000000", "--acquire-timeout", "90",
            "--per-record-epochs", "--epoch-sync-count", "30",
            "--pause-between-s", "60",
        ])
        requested = plan(a, p)
        self.assertAlmostEqual(requested["nominal_window_s"], 20)
        self.assertEqual(requested["count"], 3)
        self.assertEqual(requested["trigger_position_pct"], 0.0)
        self.assertEqual(a.trigger_position_pct, 0.0)
        self.assertTrue(requested["per_record_epochs"])
        self.assertEqual(requested["pause_between_s"], 60)


if __name__ == "__main__":
    unittest.main()
