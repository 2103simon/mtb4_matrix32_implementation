"""Offline tests for integrated Matrix32 analysis. No hardware calls."""
from pathlib import Path
import json
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "oscilloscope"))
from analyze_integrated_matrix32 import (
    choose_run, envelope, host_to_scope, pulse_statistics, rising_pulses,
)


class IntegratedAnalysisTests(unittest.TestCase):
    def test_positive_biphasic_pulse_one_event(self):
        t = np.arange(0, 0.1, 0.00005)
        v = np.zeros_like(t)
        for onset in (0.01, 0.03, 0.05, 0.07, 0.09):
            v[(t >= onset) & (t < onset + 0.0002)] = 8.0
            v[(t >= onset + 0.0002) & (t < onset + 0.0004)] = -8.0
        events = rising_pulses(t, v, threshold_v=2, min_interval_s=0.001)
        self.assertEqual(len(events), 5)
        stats = pulse_statistics(t, events, threshold_v=2)
        self.assertEqual(stats["positive_rising_events"], 5)
        self.assertAlmostEqual(stats["median_interevent_ms"], 20, places=1)
        self.assertAlmostEqual(stats["median_instantaneous_rate_hz"], 50, places=1)

    def test_deadtime_and_hysteresis(self):
        t = np.arange(0, 0.015, 0.0001)
        v = np.zeros_like(t)
        v[20:22] = 3
        v[22:24] = 0
        v[24:26] = 3
        v[100:103] = 3
        self.assertEqual(len(rising_pulses(t, v, 2, 0.001)), 2)

    def test_envelope_keeps_short_pulse(self):
        t = np.arange(10000, dtype=float) * .00002
        v = np.zeros(10000)
        v[1234] = 15
        v[5432] = -17
        _, low, high = envelope(t, v, max_bins=50)
        self.assertEqual(max(high), 15)
        self.assertEqual(min(low), -17)

    def test_host_map_subtracts_integer_anchor_first(self):
        mapping = {
            "first_sample_pc_monotonic_ns_est": 196000000000000,
            "last_sample_pc_monotonic_ns_est": 196020000000000,
        }
        pc = np.array([196000000000000, 196010000000000,
                       196020000000000], dtype=np.int64)
        np.testing.assert_allclose(host_to_scope(pc, mapping, 0, 20),
                                   [0, 10, 20])

    def test_latest_complete_integrated_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name, status in (
                    ("20261120T100000Z_integrated_matrix32_scope", "complete"),
                    ("20261120T110000Z_integrated_matrix32_scope", "failed"),
                    ("20261120T120000Z_integrated_matrix32_scope", "complete")):
                path = root / name
                path.mkdir()
                (path / "metadata.json").write_text(json.dumps({
                    "status": status,
                    "plan": {"integrated_experiment": "MATRIX32_MCU_NANO_SCOPE_V1"},
                }))
            selected, _ = choose_run(root)
            self.assertEqual(selected.name,
                             "20261120T120000Z_integrated_matrix32_scope")

    def test_bad_explicit_run_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)
            (path / "metadata.json").write_text(json.dumps({
                "plan": {"integrated_experiment": "GRAY_TC_V1"}
            }))
            with self.assertRaisesRegex(ValueError, "not an integrated"):
                choose_run(path, path)


if __name__ == "__main__":
    unittest.main()
