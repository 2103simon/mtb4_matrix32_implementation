"""No-hardware tests for blocked-holdout Gray-code clock model comparison."""
import unittest

import numpy as np

from oscilloscope.evaluate_clock_models import (
    _metrics, _xy, assess_models, blocked_holdout, model_stability, predict,
)


class ClockModelCVTests(unittest.TestCase):
    def test_affine_recovered_from_offset_nano_clock(self):
        x = np.arange(200)*.1
        y = 320. + 1.00002*x
        a = predict("affine", x[::2], y[::2], x[1::2])
        np.testing.assert_allclose(a, y[1::2], atol=1e-9)
        result, _, folds = blocked_holdout(x, y)
        self.assertGreater(folds, 5)
        self.assertLess(result["affine"]["rms_us"], 1e-5)

    def test_smooth_drift_benefits_from_cross_validated_local_model(self):
        x = np.arange(200)*.1
        y = 1000.+x+0.0003*np.sin(2*np.pi*x/18)
        report, points = assess_models(x, y)
        self.assertGreater(report["models"]["affine"]["holdout"]["rms_us"], 100)
        self.assertLess(report["models"]["local_linear_1p5s"]["holdout"]["rms_us"], 100)
        self.assertTrue(report["models"]["local_linear_1p5s"]["clock_rate_stability"]["monotonic"])
        self.assertIsNotNone(report["candidate_for_independent_validation"])
        self.assertTrue(report["selection_is_exploratory_only"])
        self.assertFalse(report["can_replace_official_clock_mapping"])
        self.assertTrue(points)

    def test_unpredictable_jitter_not_reclassified_by_full_fit(self):
        x = np.arange(200)*.1
        rng = np.random.default_rng(11)
        y = 200.+x+rng.normal(0, .0003, len(x))
        report, _ = assess_models(x, y)
        self.assertIsNone(report["candidate_for_independent_validation"])
        self.assertGreater(report["models"]["affine"]["holdout"]["rms_us"], 100)

    def test_input_monotonicity_and_sample_count(self):
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            _xy([(float(i), float(12-i), i) for i in range(12)])
        with self.assertRaises(ValueError):
            blocked_holdout([0, 1], [1], block_s=1)
        with self.assertRaises(ValueError):
            predict("arbitrary_model", np.arange(12.), np.arange(12.), [4.])

    def test_metrics_keep_bias_and_total_test_frames(self):
        m = _metrics([1, -2, 3])
        self.assertEqual(m["n"], 3)
        self.assertEqual(m["max_abs_us"], 3)
        self.assertAlmostEqual(m["bias_us"], 2/3)

    def test_clock_reversal_stability_check(self):
        x = np.arange(200)*.1
        y = 500.+x
        stable = model_stability("affine", x, y)
        self.assertTrue(stable["monotonic"])
        self.assertLess(stable["max_local_slope_deviation_from_median_ppm"], 1e-3)


if __name__ == "__main__":
    unittest.main()
