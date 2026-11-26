"""No-hardware regression checks for the shared piecewise attribution path."""
import unittest

import numpy as np

from experiment_analysis.offset_robust_command_matching import (
    classify_interval, refined_onset_envelopes,
)
from experiment_analysis.run_piecewise_command_analysis import agreement_statistics


class PiecewiseAssociationTests(unittest.TestCase):
    def setUp(self):
        self.x = np.arange(101.) * .1
        self.y = 100 + self.x
        self.offset = 5000000000
        self.exchanges = []
        for nano in (99000000, 111000000):
            self.exchanges.append({
                "t2_nano_us": nano, "t3_nano_us": nano+100,
                "t1_host_ns": self.offset+1000*nano-1000000,
                "t4_host_ns": self.offset+1000*(nano+100)+2000000,
            })
        self.epoch = {"fit": {
            "anchor_nano_us": 100000000.,
            "anchor_host_monotonic_ns": self.offset+100000000000,
            "relative_offset_ns": 0.,
            "slope_host_ns_per_nano_us": 1000.,
            "host_clock_mapping_estimated": True,
        }}
        self.refinement = {"models": {"piecewise_linear": {
            "clock_rate_stability": {"monotonic": True},
            "holdouts": {k: {"n": 30, "p95_abs_us": v}
                         for k, v in (("single_frame", 10), ("1s", 25), ("2s", 50))},
        }}}

    def test_envelope_includes_true_times_and_rejects_extrapolation(self):
        times = np.array([-.01, .1, .2, 5., 10.01])
        out = refined_onset_envelopes(
            times, self.x, self.y, self.epoch, self.refinement,
            self.exchanges, scale=1.)
        np.testing.assert_array_equal(out["valid"], [False, True, True, True, False])
        np.testing.assert_array_equal(out["interval_valid"], [False, True, True, False])
        true = self.offset + (100+times[1:4])*1e9
        self.assertTrue(np.all(out["lower"][1:4] <= true))
        self.assertTrue(np.all(out["upper"][1:4] >= true))
        self.assertEqual(out["p95_us"], 50)
        self.assertTrue(np.isnan(out["nominal"][0]))

    def test_period_crossing_large_gray_gap_is_excluded(self):
        x = self.x[(self.x <= 2) | (self.x >= 6)]
        y = 100+x
        out = refined_onset_envelopes(
            np.array([1.9, 6.1]), x, y, self.epoch, self.refinement,
            self.exchanges, scale=1.)
        self.assertTrue(np.all(out["valid"]))
        self.assertFalse(out["interval_valid"][0])

    def test_ambiguous_mcu_transition_is_not_assigned(self):
        row = classify_interval(
            [1.10e9, 1.11e9], [1.15e9, 1.16e9],
            mcu_pc=np.array([1e9, 1.155e9]), mcu_rates=np.array([20., 30.]),
            ack_pc=np.array([], dtype=int), ack_states=[], measured_hz=20.)
        self.assertEqual(row["mcu_assessment"], "MCU_FREQUENCY_CHANGES_WITHIN_OFFSET_RANGE")

    def test_statistics_keep_unclassified_in_denominator(self):
        rows = [{"mcu_assessment": "STABLE_ACROSS_TESTED_OFFSETS",
                 "mcu_error_hz_if_stable": -.2},
                {"mcu_assessment": "OUTSIDE_GRAY_FRAME_SUPPORT"}]
        result = agreement_statistics(rows, "mcu")
        self.assertEqual(result["stable_fraction"], .5)
        self.assertAlmostEqual(result["median_abs_error_hz"], .2)


if __name__ == "__main__":
    unittest.main()
