"""Offline clock-qualification diagnostic tests (no scope/serial hardware)."""
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from oscilloscope.clock_qualification_diagnostics import (
    _frame_pairs, _residual_fit, _robust_candidate, diagnose,
)
from experiment_analysis.paths import use_results_root


class ClockQualificationTests(unittest.TestCase):
    def test_affine_fit_recovers_steady_clock(self):
        x = np.arange(30.) / 10.
        y = 101. + 1.0003*x
        fit = _residual_fit(x, y)
        self.assertLess(fit["rms_all_us"], 1e-6)
        self.assertAlmostEqual(fit["slope_nano_s_per_scope_s"], 1.0003,
                               places=8)

    def test_outlier_diagnostic_does_not_change_official_fit(self):
        x = np.arange(50.) / 10.
        y = 100. + x
        y[20] += .002
        baseline, robust, status = _robust_candidate(x, y)
        self.assertGreater(baseline["rms_all_us"], 100.)
        self.assertIsNotNone(robust)
        self.assertGreaterEqual(status["outliers"], 1)
        self.assertLess(robust["rms_inliers_us"], baseline["rms_all_us"])
        self.assertGreater(robust["rms_all_us"], 100.)

    def test_ambiguous_counter_fails_closed(self):
        record = {"decoded_gray": {"frames": [{
            "counter_mod_65536": 2, "gray_hex": "0003",
            "scope_start_s": .1,
        }]}}
        pairs, warning = _frame_pairs(
            None, record, {2: {"gray_hex": "0003", "nano_unwrapped_us": 1000},
                           65538: {"gray_hex": "0003",
                                   "nano_unwrapped_us": 65538000}})
        self.assertEqual(pairs, [])
        self.assertIn("ambiguous", warning)

    def test_full_report_exposes_failure_without_requalification(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "raw"
            root.mkdir()
            frames = []
            serial = []
            for i in range(30):
                t = .1*i
                frames.append({"scope_start_s": t,
                               "counter_mod_65536": i,
                               "gray_hex": f"{i^(i>>1):04X}"})
                serial.append(json.dumps({
                    "line": (f"FRAME {i} GRAY {i^(i>>1):04X} "
                             f"EDGE_US {100000+i*100000} OVERRUNS 0")
                }))
            (root / "metadata.json").write_text(json.dumps({
                "records": [{"index": 1, "decoded_gray": {"frames": frames}}]
            }))
            (root / "nano_serial.jsonl").write_text("\n".join(serial))
            entry = {
                "record_index": 1, "status": "unqualified",
                "scope_edge_fit_rms_us": 125.,
                "scope_edge_fit_rms_threshold_us": 100.,
                "fit": {"rms_residual_us": 220.},
                "diagnostics": {"absolute_slope_sensitivity_ppm": 10.},
                "offset_constraint": {"feasible": True},
                "warnings": ["Scope edge fit above 100 us"],
            }
            (root / "epoch_clock_analysis.json").write_text(json.dumps({
                "records": [entry],
            }))
            with use_results_root(Path(td) / "results"):
                report, rows = diagnose(root)
            self.assertEqual(rows[0]["original_status"], "unqualified")
            self.assertEqual(rows[0]["failed_checks"], [
                "Scope/Nano Gray edge RMS"])
            self.assertFalse(rows[0]["clock_map_updated"])
            self.assertTrue(report.exists())
            self.assertTrue((Path(td) / "results/raw/analysis/record_00001/"
                             "clock_residual_diagnostics.png").exists())
            self.assertFalse((root / "analysis").exists())
            self.assertIn("125.0", report.read_text())


if __name__ == "__main__":
    unittest.main()
