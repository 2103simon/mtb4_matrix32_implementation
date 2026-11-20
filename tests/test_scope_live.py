"""Offline tests for repeated finite scope acquisition; no USB access."""
import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCOPE = Path(__file__).resolve().parents[1] / "oscilloscope"
sys.path.insert(0, str(SCOPE))
import record_scope_live as scope


def fake_record(f, args, *, index, root, prev=None):
    filename = root / "waveforms" / f"record_{index:05d}_ch2.csv"
    filename.write_text("time_s,voltage_v,raw\n0,0,0\n0.000002,2,1\n")
    return {
        "index": index, "status": "captured",
        "waveform": str(filename.relative_to(root)),
        "scope_window_s": 0.000002,
        "cycle_begin_monotonic_ns": index * 100000000,
        "cycle_duration_s": 0.12,
        "transfer_duration_s": 0.05,
        "waveform_timing": {"samples": 2},
    }


class ScopeLiveTests(unittest.TestCase):
    def test_dry_run_has_no_hardware_or_files(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(scope, "resolve_scope_device",
                              side_effect=AssertionError("USB accessed")):
                scope.main(["--output-root", folder, "--count", "3"])
            self.assertFalse(list(Path(folder).iterdir()))

    def test_invalid_settings(self):
        for option in (["--count", "0"], ["--channel", "5"],
                       ["--trigger-level", "nan"], ["--points", "1"]):
            with self.subTest(option=option):
                with self.assertRaises(SystemExit):
                    scope.main(option)

    def test_successful_finite_records_and_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            args = scope.build_parser().parse_args(
                ["--output-root", folder, "--count", "3", "--run"])
            plan = scope.validate_args(args, scope.build_parser())
            with patch.object(scope, "resolve_scope_device", return_value=Path("/dev/null")), \
                 patch.object(scope, "query",
                              return_value="TEKTRONIX,MDO4104B-6,C010233,FW"), \
                 patch.object(scope, "configure"), \
                 patch.object(scope, "scope_settings", return_value={}), \
                 patch.object(scope, "verify"), \
                 patch.object(scope, "acquire_one", side_effect=fake_record):
                result = scope.run(args, plan)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["captured_count"], 3)
            self.assertFalse(result["gap_free_acquisition_verified"])
            files = list(Path(folder).glob("*/summary.json"))
            self.assertEqual(len(files), 1)
            saved = json.loads(files[0].read_text())
            self.assertEqual([r["index"] for r in saved["records"]], [1, 2, 3])
            self.assertEqual(len(list(files[0].parent.glob("waveforms/*.csv"))), 3)

    def test_trigger_diagnostics_retained_after_timeout(self):
        with tempfile.TemporaryDirectory() as folder:
            args = scope.build_parser().parse_args(
                ["--diagnose-trigger", "--output-root", folder])
            scope.validate_args(args, scope.build_parser())
            root = Path(folder)
            (root / "waveforms").mkdir()
            snapshot = {
                "elapsed_s": 15.1,
                "acquisition_state_observations": [{"value": "1"}],
                "trigger_state_observations": [{"value": "READY"}],
                "first_trigger_observation": None,
            }

            def timeout_acquire(f, timeout, on_progress=None):
                self.assertIsNotNone(on_progress)
                on_progress(snapshot)
                raise TimeoutError("scope still running")

            with patch.object(scope, "acquire", side_effect=timeout_acquire):
                with self.assertRaisesRegex(TimeoutError, "scope still running"):
                    scope.acquire_one(None, args, index=1, root=root)
            record = json.loads((root / "record_00001.json").read_text())
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["diagnostic_summary"]["last_trigger_state"], "READY")
            self.assertEqual(record["diagnostic_summary"]["last_acquisition_state"], "1")

    def test_failure_retains_summary_and_index(self):
        with tempfile.TemporaryDirectory() as folder:
            args = scope.build_parser().parse_args(
                ["--output-root", folder, "--count", "3", "--run"])
            plan = scope.validate_args(args, scope.build_parser())

            def fail_second(f, args, *, index, root, prev=None):
                if index == 2:
                    scope.atomic_json(root / "record_00002.json",
                                      {"index": 2, "status": "failed"})
                    raise RuntimeError("mock acquisition failed")
                return fake_record(f, args, index=index, root=root, prev=prev)

            with patch.object(scope, "resolve_scope_device", return_value=Path("/dev/null")), \
                 patch.object(scope, "query",
                              return_value="TEKTRONIX,MDO4104B-6,C010233,FW"), \
                 patch.object(scope, "configure"), \
                 patch.object(scope, "scope_settings", return_value={}), \
                 patch.object(scope, "verify"), \
                 patch.object(scope, "acquire_one", side_effect=fail_second):
                with self.assertRaisesRegex(RuntimeError, "mock acquisition failed"):
                    scope.run(args, plan)
            saved = json.loads(next(Path(folder).glob("*/summary.json")).read_text())
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(saved["captured_count"], 1)
            self.assertEqual(saved["records"][-1]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
