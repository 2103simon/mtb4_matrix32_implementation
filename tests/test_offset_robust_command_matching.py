"""No-hardware checks for uncertainty-aware approved CH1 command matching."""
import unittest

import numpy as np

from oscilloscope.offset_robust_command_matching import (
    offset_scenarios, select_clock_model, classify_interval,
)


class OffsetRobustCommandMatchingTests(unittest.TestCase):
    def setUp(self):
        self.fit = {
            "host_clock_mapping_estimated": True,
            "anchor_nano_us": 100000.,
            "anchor_host_monotonic_ns": 1_000_000_000_000,
            "relative_offset_ns": 1_000_000.,
            "slope_host_ns_per_nano_us": 1000.,
        }
        self.epoch = {
            "fit": self.fit,
            "offset_constraint": {
                "feasible": True,
                "anchor_nano_us": 100000.,
                "anchor_host_reference_ns": 1_000_000_000_000,
                "intersection_lower_relative_ns": 0.,
                "intersection_upper_relative_ns": 3_000_000.,
            },
        }

    def test_offset_endpoints_account_for_fit_relative_offset(self):
        offset, problem = offset_scenarios(self.epoch, 100.)
        self.assertIsNone(problem)
        self.assertEqual(offset["deltas_ns"],
                         [-1100000., 500000., 2100000.])
        self.assertAlmostEqual(offset["causal_interval_width_ms"], 3.)
        self.assertFalse(offset["not_guaranteed_absolute_error_bound"] is False)

    def test_unavailable_causal_offset_never_invents_bounds(self):
        self.epoch["offset_constraint"]["feasible"] = False
        row, reason = offset_scenarios(self.epoch, 100.)
        self.assertIsNone(row)
        self.assertEqual(reason, "NO_FEASIBLE_CAUSAL_OFFSET_INTERVAL")

    def test_clock_selection_marks_record4_no_pass(self):
        cv = {
            "models": {
                "affine": {
                    "holdout": {"rms_us": 250.},
                    "holdout_2x": {"rms_us": 290.},
                    "clock_rate_stability": {"monotonic": True},
                },
                "local_linear_0p75s": {
                    "holdout": {"rms_us": 51.},
                    "holdout_2x": {"rms_us": 107.},
                    "clock_rate_stability": {"monotonic": True},
                },
            }
        }
        name, status = select_clock_model(cv)
        self.assertEqual(name, "local_linear_0p75s")
        self.assertEqual(status, "EXPLORATORY_BEST_NO_MODEL_PASSED")

    def test_stable_ack_and_mcu_across_uncertainty_envelope(self):
        mcu_pc = np.array([1_000_000_000, 1_100_000_000,
                           1_200_000_000], dtype=np.int64)
        mcu_rates = np.array([100., 100., 100.])
        ack_pc = np.array([1_000_000_000, 1_100_000_000], dtype=np.int64)
        states = [
            {"expected_hz": 80, "output_active_assumed": True},
            {"expected_hz": 100, "output_active_assumed": True},
        ]
        r = classify_interval(
            np.array([1_150_000_000., 1_151_000_000.,
                      1_152_000_000.]),
            np.array([1_160_000_000., 1_161_000_000.,
                      1_162_000_000.]),
            mcu_pc=mcu_pc, mcu_rates=mcu_rates, ack_pc=ack_pc,
            ack_states=states, measured_hz=101.)
        self.assertEqual(r["mcu_assessment"], "STABLE_ACROSS_TESTED_OFFSETS")
        self.assertEqual(r["ack_assessment"], "STABLE_ACROSS_TESTED_OFFSETS")
        self.assertTrue(r["all_scenarios_agree"])
        self.assertEqual(r["ack_matching_diagnostic"],
                         "WITHIN_EXPLORATORY_TOLERANCE")

    def test_hidden_short_lived_ack_and_mcu_change_are_detected(self):
        mcu_pc = np.array([1_000_000_000, 1_150_000_000,
                           1_151_000_000, 1_200_000_000], dtype=np.int64)
        mcu_rates = np.array([100., 200., 100., 100.])
        ack_pc = np.array([1_000_000_000, 1_150_000_000,
                           1_151_000_000], dtype=np.int64)
        states = [{"expected_hz": hz, "output_active_assumed": True}
                  for hz in (100, 200, 100)]
        r = classify_interval(
            np.array([1_140_000_000., 1_160_000_000.]),
            np.array([1_142_000_000., 1_162_000_000.]),
            mcu_pc=mcu_pc, mcu_rates=mcu_rates,
            ack_pc=ack_pc, ack_states=states, measured_hz=100.)
        self.assertEqual(r["mcu_assessment"],
                         "MCU_FREQUENCY_CHANGES_WITHIN_OFFSET_RANGE")
        self.assertEqual(r["ack_assessment"],
                         "TRANSITION_WITHIN_OFFSET_ENVELOPE")
        self.assertFalse(r["all_scenarios_agree"])

    def test_guard_blocks_early_cycle_even_if_state_stable(self):
        mcu_pc = np.array([1_000_000_000, 1_100_000_000,
                           1_200_000_000], dtype=np.int64)
        ack_pc = np.array([1_000_000_000, 1_100_000_000], dtype=np.int64)
        states = [{"expected_hz": 2, "output_active_assumed": True},
                  {"expected_hz": 100, "output_active_assumed": True}]
        r = classify_interval(
            np.array([1_110_000_000., 1_111_000_000.]),
            np.array([1_120_000_000., 1_121_000_000.]),
            mcu_pc=mcu_pc, mcu_rates=np.array([100., 100., 100.]),
            ack_pc=ack_pc, ack_states=states, measured_hz=100.)
        self.assertEqual(r["ack_assessment"],
                         "ACK_PRIOR_CYCLE_OR_UNKNOWN_GUARD")

    def test_muted_transport_is_not_physical_ack(self):
        mcu_pc = np.array([1_000_000_000, 1_100_000_000], dtype=np.int64)
        states = [{"expected_hz": 100, "output_active_assumed": True},
                  {"expected_hz": 0, "output_active_assumed": False}]
        r = classify_interval(
            np.array([1_150_000_000., 1_151_000_000.]),
            np.array([1_155_000_000., 1_156_000_000.]),
            mcu_pc=mcu_pc, mcu_rates=np.array([0., 0.]),
            ack_pc=np.array([1_000_000_000, 1_100_000_000]),
            ack_states=states, measured_hz=200.)
        self.assertEqual(r["ack_assessment"], "ACK_MUTED_OR_UNKNOWN")
        self.assertFalse(r["all_scenarios_agree"])


if __name__ == "__main__":
    unittest.main()
