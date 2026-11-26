"""Synthetic, no-hardware checks for the timing refinement."""
import unittest

import numpy as np

from communication.clock_estimator_diagnostics import (
    causal_slope_interval, causal_host_envelope,
)
from oscilloscope.evaluate_clock_models import blocked_holdout, predict
from oscilloscope.refine_timing_investigation import compare_models


class TimingRefinementTests(unittest.TestCase):
    def test_piecewise_interpolates_but_does_not_extrapolate(self):
        x = np.arange(20.) * .1
        y = 400 + x * 1.0002
        q = (x[:-1] + x[1:]) / 2
        np.testing.assert_allclose(
            predict("piecewise_linear", x, y, q), 400 + q * 1.0002,
            atol=1e-10, rtol=0,
        )
        with self.assertRaises(ValueError):
            predict("piecewise_linear", x, y, [-.01])

    def test_single_frame_holdout_never_scores_training_anchors(self):
        x = np.arange(40.) * .1
        y = 500 + x
        y[20] += .0003
        scores, rows, folds = blocked_holdout(
            x, y, models=("piecewise_linear",), single_frame=True
        )
        self.assertEqual(folds, len(x) - 2)
        self.assertEqual(scores["piecewise_linear"]["n"], len(x) - 2)
        held = [r for r in rows if r["block"] == 20]
        self.assertEqual(len(held), 1)
        self.assertAlmostEqual(held[0]["heldout_error_us"], -300, places=5)

    def test_composition_uses_product_of_clock_rates(self):
        x = np.arange(200.) * .1
        y = 400 + x * 1.0005
        models = compare_models(x, y, 1000 / 1.0005)
        self.assertAlmostEqual(models["affine"]["combined_average_drift_ppm"], 0, places=5)

    def test_causal_envelope_contains_true_clock(self):
        # Affine physical clock with intentionally asymmetric transport.
        slope = 1000.4
        offset = 2000000000
        rows = []
        for nano in range(0, 10000001, 1000000):
            tx = nano + 100
            rows.append({
                "t2_nano_us": nano, "t3_nano_us": tx,
                "t1_host_ns": round(offset + slope*nano - 800000),
                "t4_host_ns": round(offset + slope*tx + 2200000),
            })
        interval = causal_slope_interval(rows)
        self.assertTrue(interval["feasible"])
        self.assertLessEqual(interval["slope_lower_ns_per_us"], slope)
        self.assertGreaterEqual(interval["slope_upper_ns_per_us"], slope)
        fit = {"anchor_host_monotonic_ns": offset,
               "anchor_nano_us": 0, "relative_offset_ns": 0,
               "slope_host_ns_per_nano_us": slope}
        for t in (0, 5000000, 10000000):
            envelope = causal_host_envelope(rows, t, fit, interval)
            self.assertLessEqual(envelope["lower_relative_to_midpoint_fit_us"], 0)
            self.assertGreaterEqual(envelope["upper_relative_to_midpoint_fit_us"], 0)
            self.assertGreater(envelope["width_us"], 0)

    def test_infeasible_exchanges_fail_closed(self):
        rows = [{"t2_nano_us": 0, "t3_nano_us": 0,
                 "t1_host_ns": 10, "t4_host_ns": 5}]
        self.assertFalse(causal_slope_interval(rows)["feasible"])


if __name__ == "__main__":
    unittest.main()
