"""Offline diagnostics for Nano/host clock estimator stability."""
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from communication.clock_estimator_diagnostics import (
    diagnose_active_clock, _clock_intersection, _stratified_low_latency,
    _holdout_checks,
)
from test_clock_sync import synthetic_exchanges


class ClockEstimatorDiagnosticsTests(unittest.TestCase):
    def test_stationary_synthetic_exchange(self):
        rows = synthetic_exchanges(n=72)
        result = diagnose_active_clock(rows)
        self.assertEqual(result["baseline"]["sample_count"], 72)
        self.assertEqual(result["stratified_low_latency"]["selection"]["bins"], 6)
        self.assertTrue(result["offset_constraints"][
            "all_exchanges_conditional_on_baseline_slope"]["feasible"])
        self.assertLess(result["absolute_slope_sensitivity_ppm"], 20)
        self.assertLess(result["window_drift_range_ppm"], 30)
        self.assertEqual(len(result["chronological_window_fits"]), 3)
        self.assertEqual(
            [x["status"] for x in result["chronological_window_fits"]],
            ["qualified"]*3,
        )

    def test_stratified_selection_preserves_early_and_late(self):
        rows = synthetic_exchanges(n=72)
        selected = _stratified_low_latency(rows, 1000.42,
                                           bins=6, keep_fraction=0.25)
        self.assertEqual(len(selected), 18)
        self.assertLess(selected[0]["seq"], 12)
        self.assertGreater(selected[-1]["seq"], 60)

    def test_jitter_spikes_are_reported_without_replacing_baseline(self):
        rows = synthetic_exchanges(n=72)
        for i in range(4, len(rows), 7):
            rows[i]["t4_host_ns"] += 8_000_000
        result = diagnose_active_clock(rows)
        self.assertEqual(result["baseline"]["sample_count"], 72)
        self.assertEqual(result["stratified_low_latency"]["sample_count"], 18)
        self.assertGreater(result["transport_delay_us"]["p90"],
                           result["transport_delay_us"]["p10"])
        self.assertGreater(
            result["baseline"]["rms_residual_us"],
            result["stratified_low_latency"]["rms_residual_us"],
        )

    def test_inconsistent_clock_offset_constraints_detected(self):
        rows = synthetic_exchanges(n=20)
        # Force an impossible common offset using a deliberately wrong
        # slope; causal intervals should not be silently widened.
        result = _clock_intersection(rows, 1002.0, rows[0]["t2_nano_us"])
        self.assertFalse(result["feasible"])
        self.assertGreater(result["infeasibility_gap_us"], 0)

    def test_blocked_holdout_on_stationary_clock(self):
        checks = _holdout_checks(synthetic_exchanges(n=72))
        self.assertEqual(checks["qualified_fold_count"], 3)
        self.assertEqual([x["test_count"] for x in checks["folds"]],
                         [24, 24, 24])
        self.assertLess(checks["worst_holdout_midpoint_rms_us"], 100)
        self.assertLess(checks["holdout_train_drift_range_ppm"], 30)

    def test_blocked_holdout_exposes_time_dependent_bias(self):
        rows = synthetic_exchanges(n=72)
        # First third receives a constant additional downstream delay;
        # ordinary training fit on the rest cannot predict that block.
        for row in rows[:24]:
            row["t4_host_ns"] += 5_000_000
        checks = _holdout_checks(rows)
        self.assertGreater(checks["max_abs_holdout_midpoint_bias_us"], 1000)
        self.assertEqual(checks["qualified_fold_count"], 3)

    def test_bad_configuration_rejected(self):
        rows = synthetic_exchanges(n=30)
        with self.assertRaisesRegex(ValueError, "keep_fraction"):
            diagnose_active_clock(rows, keep_fraction=0)
        with self.assertRaisesRegex(ValueError, "bins"):
            diagnose_active_clock(rows, bins=1)


if __name__ == "__main__":
    unittest.main()
