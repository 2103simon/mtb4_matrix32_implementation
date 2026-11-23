"""Hardware-free regression tests for approved physical frequency calculations."""
import unittest
import numpy as np

from oscilloscope.approved_frequency_analysis import (
    compute_approved_intervals, _quantiles,
)


class ApprovedFrequencyTests(unittest.TestCase):
    def setUp(self):
        self.mcu = {
            "taxel_pc_ns": np.array(
                [1000000000, 1100000000, 1200000000, 1300000000,
                 1400000000, 1500000000, 1600000000, 1700000000,
                 1800000000, 1900000000], dtype=np.int64),
            "taxel_data": np.ones((10, 12), dtype=float) * 40,
            "sa_rate_hz": np.ones((10, 12), dtype=float) * 100,
            "ra_rate_hz": np.ones((10, 12), dtype=float) * 12,
        }
        self.map = {
            "first_sample_pc_monotonic_ns_est": 1000000000,
            "last_sample_pc_monotonic_ns_est": 2000000000,
            "status": "provisional_local_fit",
        }

    def _calculate(self, onsets, mapping=None, states=None):
        return compute_approved_intervals(
            index=1, approved_onsets=onsets, scope_start=0, scope_end=1,
            mapping=self.map if mapping is None else mapping,
            mcu=self.mcu, taxel=11, encoding="sa_ii",
            ack_states=states or [],
        )

    def test_approved_interval_period_and_frequency(self):
        onsets = [
            {"id": "P1", "time_s": .3},
            {"id": "P2", "time_s": .31},
            {"id": "P3", "time_s": .32},
        ]
        rows, summary, _ = self._calculate(onsets)
        self.assertEqual(summary["onset_count"], 3)
        self.assertEqual(summary["interval_count"], 2)
        self.assertAlmostEqual(summary["period_ms"]["median"], 10)
        self.assertAlmostEqual(summary["inverse_period_hz"]["median"], 100)
        self.assertEqual(rows[0]["start_onset_id"], "P1")
        self.assertEqual(rows[1]["end_onset_id"], "P3")
        self.assertFalse(summary["cross_clock_alignment_verified"])

    def test_empty_record_is_null_not_zero_hz(self):
        rows, summary, _ = self._calculate([])
        self.assertEqual(rows, [])
        self.assertEqual(summary["onset_count"], 0)
        self.assertEqual(summary["interval_count"], 0)
        self.assertIsNone(summary["inverse_period_hz"]["median"])
        self.assertIsNone(summary["diagnostic_compatibility_fraction"])

    def test_unknown_mapping_blocks_ack_attribution(self):
        onsets = [{"id": "P1", "time_s": .3},
                  {"id": "P2", "time_s": .31}]
        rows, summary, _ = compute_approved_intervals(
            index=1, approved_onsets=onsets, scope_start=0, scope_end=1,
            mapping=None, mcu=self.mcu, taxel=11, encoding="sa_ii",
            ack_states=[{"pc_monotonic_ns": 1100000000,
                         "expected_hz": 100, "output_active_assumed": True}],
        )
        self.assertEqual(rows[0]["ack_state_assessment"],
                         "UNKNOWN_NO_CLOCK_MAP_OR_ACK")
        self.assertIsNone(rows[0]["mcu_requested_hz_nearest_provisional"])
        self.assertEqual(summary["diagnostic_comparable_intervals"], 0)

    def test_steady_ack_compatible_not_physical_proof(self):
        onsets = [{"id": f"P{i}", "time_s": .30+i*.01}
                  for i in range(4)]
        states = [
            {"pc_monotonic_ns": 1050000000, "expected_hz": 50,
             "output_active_assumed": True},
            {"pc_monotonic_ns": 1200000000, "expected_hz": 100,
             "output_active_assumed": True},
        ]
        rows, summary, _ = self._calculate(onsets, states=states)
        self.assertEqual(summary["diagnostic_comparable_intervals"], 3)
        self.assertEqual(summary["diagnostic_compatible_intervals"], 3)
        self.assertEqual(rows[0]["ack_state_assessment"],
                         "COMPATIBLE_DIAGNOSTIC_ONLY")
        self.assertFalse(rows[0]["physical_application_latency_verified"])

    def test_transition_crossing_not_comparable(self):
        onsets = [{"id": "P1", "time_s": .29},
                  {"id": "P2", "time_s": .31}]
        states = [
            {"pc_monotonic_ns": 1050000000, "expected_hz": 50,
             "output_active_assumed": True},
            {"pc_monotonic_ns": 1300000000, "expected_hz": 100,
             "output_active_assumed": True},
        ]
        rows, summary, _ = self._calculate(onsets, states=states)
        self.assertEqual(summary["diagnostic_comparable_intervals"], 0)
        self.assertEqual(rows[0]["ack_state_assessment"],
                         "NOT_OBSERVABLE_SPANS_ACK_CHANGE")

    def test_stale_mcu_rate_unreported(self):
        self.mcu["taxel_pc_ns"] = np.array([1000000000], dtype=np.int64)
        self.mcu["taxel_data"] = self.mcu["taxel_data"][:1]
        self.mcu["sa_rate_hz"] = self.mcu["sa_rate_hz"][:1]
        self.mcu["ra_rate_hz"] = self.mcu["ra_rate_hz"][:1]
        rows, _, _ = self._calculate([
            {"id": "P1", "time_s": .8},
            {"id": "P2", "time_s": .81},
        ])
        self.assertIsNone(rows[0]["mcu_requested_hz_nearest_provisional"])


if __name__ == "__main__":
    unittest.main()
