"""Offline tests for the current approved-negative-onset analysis contract.

Legacy positive-edge detectors and sliding-window compatibility APIs were
removed deliberately. Exercise approved intervals, diagnostic ACK context,
source-preserving waveform loading and the current session entry point.
"""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from oscilloscope.analyze_integrated_matrix32 import (
    acknowledged_frequency_states, choose_run, host_to_scope, load_wave,
    summarize,
)
from oscilloscope.approved_frequency_analysis import (
    comparison_summary, compute_approved_intervals, report_markdown,
)
from plotting.envelopes import minmax_envelope


class IntegratedAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.base = 200_000_000_000
        self.mapping = {
            "first_sample_pc_monotonic_ns_est": self.base,
            "last_sample_pc_monotonic_ns_est": self.base + 2_000_000_000,
            "status": "unqualified",
        }
        self.mcu = {
            "taxel_pc_ns": np.array([self.base], dtype=np.int64),
            "taxel_data": np.ones((1, 12)) * 40,
            "sa_rate_hz": np.ones((1, 12)) * 100,
            "ra_rate_hz": np.ones((1, 12)) * 12,
        }

    def calculate(self, times, states=(), *, mapping=None):
        # Synthetic approved inventories stand in for the separately tested
        # source-lock loader; no automatic pulse detector enters these metrics.
        onsets = [{"id": f"P{i:05d}", "time_s": float(t)}
                  for i, t in enumerate(times, 1)]
        return compute_approved_intervals(
            index=1, approved_onsets=onsets, scope_start=0, scope_end=2,
            mapping=mapping, mcu=self.mcu, taxel=11, encoding="sa_ii",
            ack_states=list(states),
        )

    def test_approved_onset_intervals_are_independent_of_clock_qualification(self):
        onsets = [.01, .03, .05, .07, .09]
        unmapped, without_clock, _ = self.calculate(onsets)
        mapped, unqualified, _ = self.calculate(onsets, mapping=self.mapping)
        self.assertEqual(without_clock["onset_count"], 5)
        self.assertEqual(without_clock["interval_count"], 4)
        self.assertAlmostEqual(without_clock["period_ms"]["median"], 20)
        self.assertAlmostEqual(without_clock["inverse_period_hz"]["median"], 50)
        self.assertEqual(
            [r["physical_inverse_period_hz"] for r in unmapped],
            [r["physical_inverse_period_hz"] for r in mapped],
        )
        self.assertEqual(unqualified["clock_status"], "unqualified")
        self.assertFalse(unqualified["cross_clock_alignment_verified"])
        self.assertFalse(unqualified["physical_application_latency_verified"])

    def test_current_entry_point_delegates_to_approved_session_analysis(self):
        root, metadata = Path("synthetic_run"), {"records": []}
        expected = {"physical_pulse_source": "OPERATOR_APPROVED_S02_NEGATIVE_ONSETS"}
        with patch("oscilloscope.analyze_integrated_session.analyze_session",
                   return_value=expected) as analyze:
            result = summarize(root, metadata, taxel=11, show=False)
        self.assertIs(result, expected)
        analyze.assert_called_once_with(root, metadata, taxel=11, show=False)

    def test_display_envelope_keeps_short_positive_and_negative_extrema(self):
        t = np.arange(10000, dtype=float) * .00002
        v = np.zeros(10000)
        v[1234], v[5432] = 15, -17
        _, low, high = minmax_envelope(t, v, bins=50)
        self.assertEqual(max(high), 15)
        self.assertEqual(min(low), -17)

    def test_saved_waveform_is_validated_without_changing_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ch1.csv"
            original = "time_s,voltage_v,raw\n0,0,0\n0.001,-8,-80\n0.002,8,80\n"
            path.write_text(original)
            times, volts = load_wave(path)
            np.testing.assert_array_equal(times, [0, .001, .002])
            np.testing.assert_array_equal(volts, [0, -8, 8])
            self.assertEqual(path.read_text(), original)
            for invalid in (
                "time_s,voltage_v\n0,0\n0.001,nan\n0.002,8\n",
                "time_s,voltage_v\n0,0\n0.002,-8\n0.001,8\n",
            ):
                path.write_text(invalid)
                with self.assertRaisesRegex(ValueError, "Invalid waveform"):
                    load_wave(path)

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

    def test_initial_start_ack_and_later_sf_ack(self):
        base = self.base
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
        self.assertEqual([x["source"] for x in states],
                         ["start_write_completed", "sf_ack",
                          "sc_ack_muted", "sc_ack_restored"])
        self.assertFalse(states[2]["output_active_assumed"])

    def test_approved_interval_diagnostics_expose_changes_and_discrepancies(self):
        states = [
            {"pc_monotonic_ns": self.base + round(t * 1e9),
             "expected_hz": hz, "output_active_assumed": hz > 0}
            for t, hz in ((0, 100), (.1, 100), (.3, 150), (.6, 0))
        ]
        rows, summary, _ = self.calculate(
            [.12, .13, .295, .305, .4, .42, .65, .66],
            states, mapping=self.mapping,
        )
        self.assertEqual(
            [r["ack_state_assessment"] for r in rows],
            ["COMPATIBLE_DIAGNOSTIC_ONLY",
             "POTENTIALLY_INCONSISTENT_REVIEW",
             "NOT_OBSERVABLE_SPANS_ACK_CHANGE",
             "NOT_OBSERVABLE_PRIOR_CYCLE_GUARD",
             "POTENTIALLY_INCONSISTENT_REVIEW",
             "NOT_OBSERVABLE_SPANS_ACK_CHANGE",
             "NOT_OBSERVABLE_MUTED_OR_UNKNOWN"],
        )
        comparison = comparison_summary(rows)
        self.assertEqual(comparison["approved_intervals"], 7)
        self.assertEqual(comparison["ack_comparable_intervals_provisional"], 3)
        self.assertEqual(comparison["ack_not_comparable_intervals"], 4)
        self.assertEqual(summary["diagnostic_compatible_intervals"], 1)
        self.assertFalse(comparison["cross_clock_error_calibrated"])

    def test_dynamic_approved_intervals_with_many_fast_acks(self):
        states = [
            {"pc_monotonic_ns": self.base+j*40_000_000,
             "expected_hz": 100, "output_active_assumed": True}
            for j in range(50)
        ]
        pulses = np.arange(.015, 2, .01)
        rows, summary, _ = self.calculate(pulses, states, mapping=self.mapping)
        self.assertGreater(summary["diagnostic_comparable_intervals"], 80)
        self.assertAlmostEqual(summary["diagnostic_compatibility_fraction"], 1)
        self.assertGreater(len(rows)-summary["diagnostic_comparable_intervals"], 0)
        self.assertAlmostEqual(summary["inverse_period_hz"]["median"], 100)

    def test_dynamic_discrepancy_and_missing_map_preserve_physical_intervals(self):
        states = [
            {"pc_monotonic_ns": self.base + step,
             "expected_hz": 100, "output_active_assumed": True}
            for step in (0, 50_000_000)
        ]
        pulses = np.arange(.1, 1.9, .02)
        rows, summary, _ = self.calculate(pulses, states, mapping=self.mapping)
        self.assertGreater(
            summary["diagnostic_ack_assessment_counts"].get(
                "POTENTIALLY_INCONSISTENT_REVIEW", 0), 80)
        self.assertEqual(summary["diagnostic_compatible_intervals"], 0)
        unknown, without_map, _ = self.calculate(pulses, states, mapping=None)
        self.assertEqual(without_map["diagnostic_comparable_intervals"], 0)
        self.assertEqual(len(unknown), len(pulses)-1)
        self.assertTrue(all(r["ack_expected_hz_provisional"] is None for r in unknown))
        self.assertEqual(
            [r["physical_inverse_period_hz"] for r in rows],
            [r["physical_inverse_period_hz"] for r in unknown],
        )

    def test_markdown_contains_current_approved_figures_and_limitations(self):
        states = [
            {"pc_monotonic_ns": self.base + step,
             "expected_hz": 100, "output_active_assumed": True}
            for step in (0, 50_000_000)
        ]
        rows, summary, _ = self.calculate(
            [.1, .11, .12], states, mapping=self.mapping)
        summary["command_comparison"] = comparison_summary(rows)
        text = report_markdown(summary)
        self.assertIn("operator-approved S02 negative-onset", text)
        self.assertIn("approved_commanded_vs_measured.png", text)
        self.assertIn("approved_frequency_tracking.png", text)
        self.assertIn("approved_frequency_distribution.png", text)
        self.assertIn("approved_ack_diagnostic.png", text)
        self.assertIn("unqualified", text)
        self.assertIn("NOT calibrated", text)
        self.assertIn("not a calibrated compliance threshold", text)
        self.assertIn("Only intervals within this one scope record", text)

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
