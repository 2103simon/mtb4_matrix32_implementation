"""Offline tests for integrated Matrix32 analysis. No hardware calls."""
from pathlib import Path
import json
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "oscilloscope"))
from analyze_integrated_matrix32 import (
    acknowledged_frequency_states, choose_run, compare_physical_with_acks,
    envelope, host_to_scope, pulse_statistics, rising_pulses,
    sliding_physical_rates, summarize_frequency_by_ack, write_markdown_report,
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

    def test_sliding_rates_count_exact_100hz(self):
        starts = np.arange(0.005, 10, 0.01)
        windows = sliding_physical_rates(starts, 0, 10, window_s=1, step_s=.5)
        self.assertEqual(len(windows), 19)
        self.assertTrue(all(w["physical_rate_hz"] == 100 for w in windows))

    def test_initial_start_ack_and_later_sf_ack(self):
        base = 200_000_000_000
        events = [
            {"event": "configuration_ack", "pc_monotonic_ns": base-2_000_000_000,
             "frequency_hz": 78, "verified": True},
            {"event": "start_write_completed", "pc_monotonic_ns": base,
             "frequency_hz": 78},
            {"event": "sf_requested", "pc_monotonic_ns": base+2_000_000_000,
             "frequency_hz": 150},
            {"event": "sf_ack", "pc_monotonic_ns": base+3_000_000_000,
             "frequency_hz": 150, "status": "OK"},
            {"event": "sf_ack", "pc_monotonic_ns": base+5_000_000_000,
             "frequency_hz": 90, "status": "FAILED"},
            {"event": "sc_ack", "pc_monotonic_ns": base+6_000_000_000,
             "current_command": 0, "status": "OK"},
            {"event": "sf_ack", "pc_monotonic_ns": base+7_000_000_000,
             "frequency_hz": 2, "status": "OK"},
            {"event": "sc_ack", "pc_monotonic_ns": base+8_000_000_000,
             "current_command": 50, "status": "OK"},
        ]
        states = acknowledged_frequency_states(events)
        self.assertEqual([x["expected_hz"] for x in states], [78, 150, 0, 2])
        mapping = {
            "first_sample_pc_monotonic_ns_est": base,
            "last_sample_pc_monotonic_ns_est": base+10_000_000_000,
            "status": "unqualified",
        }
        windows = [
            {"center_scope_s": 1.5, "window_s": 1, "physical_rate_hz": 78},
            {"center_scope_s": 3.0, "window_s": 1, "physical_rate_hz": 150},
            {"center_scope_s": 4.5, "window_s": 1, "physical_rate_hz": 148},
            {"center_scope_s": 5.0, "window_s": 1, "physical_rate_hz": 100},
            {"center_scope_s": 7.5, "window_s": 1, "physical_rate_hz": 40},
        ]
        compared = compare_physical_with_acks(
            windows, states, mapping, 0, 10
        )
        self.assertEqual(compared[0]["assessment"], "compatible_provisional")
        self.assertEqual(compared[1]["assessment"], "not_comparable")
        self.assertEqual(compared[2]["assessment"], "compatible_provisional")
        self.assertEqual(compared[3]["assessment"], "discrepant_provisional")
        self.assertEqual(compared[4]["assessment"], "not_comparable")

    def test_group_by_ack_frequency_exposes_discrepancy_and_untested(self):
        windows = [
            {"ack_expected_hz": 78, "physical_rate_hz": 79,
             "assessment": "compatible_provisional"},
            {"ack_expected_hz": 78, "physical_rate_hz": 100,
             "assessment": "discrepant_provisional"},
            {"ack_expected_hz": 150, "physical_rate_hz": 149,
             "assessment": "compatible_provisional"},
        ]
        states = [{"expected_hz": 78}, {"expected_hz": 150},
                  {"expected_hz": 400}]
        rows = summarize_frequency_by_ack(windows, states)
        self.assertEqual([x["acknowledged_frequency_hz"] for x in rows],
                         [78, 150, 400])
        self.assertEqual(rows[0]["discrepant_windows"], 1)
        self.assertEqual(rows[1]["compatible_windows"], 1)
        self.assertEqual(rows[2]["assessment"], "not_testable")

    def test_no_clock_map_refuses_ack_comparison(self):
        rows = [{"center_scope_s": 2.0, "window_s": 1,
                 "physical_rate_hz": 78.0}]
        states = [{"pc_monotonic_ns": 100000000, "expected_hz": 78}]
        result = compare_physical_with_acks(rows, states, None, 0, 10)
        self.assertEqual(result[0]["assessment"], "not_comparable")
        self.assertIsNone(result[0]["ack_expected_hz"])

    def test_markdown_contains_figures_and_status(self):
        report = {
            "run_id": "test_run", "recording_status": "complete",
            "scope": {
                "duration_s": 20., "samples": 1000000,
                "ch1_min_v": -10., "ch1_max_v": 12.,
                "gray_frames": 199, "ch1_path": "waveforms/a.csv",
                "ch2_path": "waveforms/b.csv",
                "pulse_detection": {
                    "positive_rising_events": 1900,
                    "median_instantaneous_rate_hz": 98.,
                    "median_interevent_ms": 10.2,
                    "positive_threshold_v": 2.,
                },
            },
            "mcu": {"samples": 900, "taxel": 11, "encoding": "sa_ii",
                    "observed_update_rate_hz": 44.5,
                    "requested_rate_min_hz": 0., "requested_rate_max_hz": 150.},
            "matrix32_commands": {"events": 4,
                                  "counts_by_event": {"sf_ack": 3}},
            "frequency_comparison": {
                "window_s": 1., "step_s": .25, "tolerance_hz": 3.,
                "tolerance_fraction": .05,
                "by_ack_frequency": [{
                    "acknowledged_frequency_hz": 78,
                    "settled_windows": 1, "compatible_windows": 1,
                    "discrepant_windows": 0,
                    "median_measured_rate_hz": 78.0,
                    "assessment": "compatible_in_observed_windows_provisional",
                }],
                "comparable_windows": 1, "compatible_windows": 1,
                "discrepant_windows": 0, "not_comparable_windows": 0,
                "windows": [{
                    "center_scope_s": 1., "physical_rate_hz": 78.,
                    "ack_expected_hz": 78, "difference_hz": 0,
                    "assessment": "compatible_provisional",
                }],
            },
            "clock_mapping": {"status": "unqualified", "scope_rms_us": 111.6,
                              "nano_pc_rms_us": 200.},
            "warnings": ["Clock is provisional"],
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "analysis").mkdir()
            path = Path(write_markdown_report(root, report))
            text = path.read_text()
            self.assertIn("compatible_provisional", text)
            self.assertIn("matrix32_frequency_comparison.png", text)
            self.assertIn("integrated_overview.png", text)
            self.assertIn("matrix32_pulse_zoom.png", text)
            self.assertIn("unqualified", text)

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
