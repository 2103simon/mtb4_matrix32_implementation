"""Hardware-free tests for frequency robustness and Gray spacing audit."""
import unittest

import numpy as np

from oscilloscope.audit_frequency_tracking import (
    gray_spacing, summary_errors, _quantities, _period_flag,
    evaluate_scenarios,
)


class FrequencyAuditTests(unittest.TestCase):
    def test_period_error_sign_and_units(self):
        period_ms, err_us, err_hz, err_pct = _quantities(100., 50.)
        self.assertAlmostEqual(period_ms, 20.)
        self.assertAlmostEqual(err_us, -10000.)
        self.assertAlmostEqual(err_hz, 50.)
        self.assertAlmostEqual(err_pct, 100.)
        self.assertEqual(_quantities(100., 0), (None, None, None, None))

    def test_spacing_constant_affine_clock_has_no_error(self):
        times = np.arange(200)*.1
        pairs = [(float(t), float(500+1.00004*t), i)
                 for i, t in enumerate(times)]
        rows, stats = gray_spacing(pairs)
        self.assertEqual(len(rows), 199)
        self.assertLess(
            stats["relative_frame_spacing_error_us"]["p95_abs"], .001)
        self.assertFalse(stats["physical_GPIO_transition_independently_timestamped"])

    def test_spacing_detects_jitter(self):
        times = np.arange(100)*.1
        nano = 500.+times
        nano[40] += .0005
        _, result = gray_spacing([
            (float(t), float(n), i) for i, (t, n)
            in enumerate(zip(times, nano))
        ])
        self.assertGreater(
            result["relative_frame_spacing_error_us"]["max_abs"], 400.)

    def test_empty_error_summary(self):
        self.assertIsNone(summary_errors([])["median_abs"])
        self.assertEqual(_period_flag(.6), "LONGER_THAN_2HZ_PERIOD")
        self.assertEqual(_period_flag(.002), "SHORTER_THAN_400HZ_PERIOD")

    def test_sweep_preserves_missing_gray_support(self):
        epoch = {
            "fit": {
                "host_clock_mapping_estimated": True,
                "anchor_nano_us": 100000.,
                "anchor_host_monotonic_ns": 1_000_000_000,
                "relative_offset_ns": 0.,
            },
            "offset_constraint": {
                "feasible": True, "anchor_nano_us": 100000.,
                "anchor_host_reference_ns": 1_000_000_000,
                "intersection_lower_relative_ns": -1_000_000.,
                "intersection_upper_relative_ns": 1_000_000.,
            },
        }
        times = np.array([0., .01, .02])
        rows = [{"id": "p0"}, {"id": "p1"}, {"id": "p2"}]
        host = np.array([1_000_000_000., 1_010_000_000.,
                         1_020_000_000.])
        rec, grid, _ = evaluate_scenarios(
            times, rows, host, np.array([False, True, True]),
            record=1, model="affine", model_status="TEST",
            epoch=epoch, cv_model={
                "holdout": {"p95_abs_us": 100.},
                "holdout_2x": {"p95_abs_us": 150.},
            },
            mcu_pc=np.array([], dtype=np.int64),
            rates=np.array([]),
            ack_pc=np.array([], dtype=np.int64), ack_states=[],
        )
        self.assertEqual(len(rec), 2)
        self.assertEqual(len(grid), 9)
        self.assertEqual(rec[0]["baseline_ack_assessment"],
                         "OUTSIDE_GRAY_FRAME_SUPPORT")
        self.assertFalse(rec[1]["stable_ack_across_all_sensitivity_scenarios"])
        self.assertEqual(
            next(r for r in grid if r["offset_scale"] == 1.
                 and r["transition_guard_ms"] == 5.)["stable_ack"], 0)


if __name__ == "__main__":
    unittest.main()
