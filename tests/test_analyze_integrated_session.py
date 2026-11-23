"""Regression tests for independent scope records and gap-aware summaries."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oscilloscope.analyze_integrated_session import (
    analyze_one, analyze_session, coverage_overlaps, describe_gaps,
    record_boundaries, select_overlap_mcu, summarize_full_stream,
)


def synthetic_mcu():
    ns = np.array([100_000_000_000, 101_000_000_000, 102_000_000_000,
                   111_000_000_000, 112_000_000_000], dtype=np.int64)
    return {
        "recording_id": "synthetic", "mode": "rates",
        "taxel_pc_ns": ns,
        "taxel_data": np.ones((5, 12)) * 100,
        "sa_rate_hz": np.ones((5, 12)) * 100,
        "ra_rate_hz": np.ones((5, 12)) * 30,
        "taxel_corrected_timestamps": np.arange(5.),
        "taxel_raw_timestamps": np.arange(5, dtype=np.uint64),
        "experiment_settings": {"configuration": {
            "stimulation_source": {"taxel": 11, "encoding": "sa_ii"}
        }},
    }


def make_scope_record(idx):
    return {"index": idx, "status": "captured",
            "channels": {
                "CH1": {"path": f"waveforms/record_{idx:05d}_ch1.csv"},
                "CH2": {"path": f"waveforms/record_{idx:05d}_ch2.csv"},
            }, "decoded_gray": {"decoded_frame_count": 2}}


def mapping(h0, h1):
    return {"first_sample_pc_monotonic_ns_est": h0,
            "last_sample_pc_monotonic_ns_est": h1,
            "status": "provisional_local_fit",
            "scope_rms_us": 50., "nano_pc_rms_us": 100.}


class PerRecordScopeTests(unittest.TestCase):
    def test_records_require_unique_indices_and_complete_channels(self):
        records = [make_scope_record(2), make_scope_record(1)]
        self.assertEqual([r["index"] for r in record_boundaries(records)], [1, 2])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            record_boundaries([records[0], records[0]])
        bad = make_scope_record(3)
        del bad["channels"]["CH1"]
        with self.assertRaisesRegex(ValueError, "missing CH1"):
            record_boundaries([bad])

    def test_local_clock_maps_match_exact_record_index(self):
        from oscilloscope.analyze_integrated_matrix32 import tentative_host_map
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "epoch_clock_analysis.json").write_text(json.dumps({
                "records": [
                    {"record_index": 1, "status": "provisional_local_fit",
                     "fit": {"rms_residual_us": 200.},
                     "first_sample_host_monotonic_ns_est": 100,
                     "last_sample_host_monotonic_ns_est": 200,
                     "scope_edge_fit_rms_us": 75.},
                    {"record_index": 2, "status": "unqualified",
                     "fit": {"rms_residual_us": 220.},
                     "first_sample_host_monotonic_ns_est": 350,
                     "last_sample_host_monotonic_ns_est": 450,
                     "scope_edge_fit_rms_us": 150.},
                ]
            }))
            first = tentative_host_map(root, record_index=1)
            second = tentative_host_map(root, record_index=2)
            self.assertEqual(first["first_sample_pc_monotonic_ns_est"], 100)
            self.assertEqual(second["first_sample_pc_monotonic_ns_est"], 350)
            self.assertEqual(second["status"], "unqualified")
            self.assertIsNone(tentative_host_map(root, record_index=3))

    def test_mcu_and_sf_ack_throughput_are_pc_native(self):
        mcu = synthetic_mcu()
        events = [
            {"event": "sf_requested", "pc_monotonic_ns": 101_000_000_000},
            {"event": "sf_ack", "status": "OK",
             "frequency_hz": 100, "pc_monotonic_ns": 101_010_000_000},
            {"event": "sf_requested", "pc_monotonic_ns": 101_020_000_000},
            {"event": "sf_ack", "status": "OK",
             "frequency_hz": 50, "pc_monotonic_ns": 101_030_000_000},
            {"event": "sf_failed", "pc_monotonic_ns": 101_050_000_000},
        ]
        row = summarize_full_stream(mcu, events)
        self.assertEqual(row["sf_ack_ok_count"], 2)
        self.assertEqual(row["sf_requested_event_count"], 2)
        self.assertEqual(row["sf_failed_event_count"], 1)
        self.assertEqual(row["sf_ack_ok_distinct_frequencies_hz"], [50, 100])
        self.assertEqual(row["sf_ack_ok_active_frequencies_hz"], [50, 100])
        self.assertEqual(row["sf_ack_ok_muted_fast_scheduler_count"], 0)
        self.assertAlmostEqual(row["sf_ack_ok_median_interack_ms"], 20)
        self.assertEqual(row["ble_events"], len(events))
        self.assertEqual(row["rate_snapshots"], 5)
        self.assertIn("arrival_interval_p90_ms", row)
        self.assertIn("sf_ack_matching_warning", row)

    def test_muted_scheduler_sf400_ack_is_not_active_output_evidence(self):
        mcu = synthetic_mcu()
        events = [
            {"event": "sc_ack", "current_command": 0, "status": "OK",
             "pc_monotonic_ns": 101_000_000_000},
            {"event": "sf_ack", "status": "OK", "frequency_hz": 400,
             "zero_current_workaround": True,
             "pc_monotonic_ns": 101_010_000_000},
            {"event": "sf_ack", "status": "OK", "frequency_hz": 50,
             "restoring_current": True,
             "pc_monotonic_ns": 101_020_000_000},
        ]
        stat = summarize_full_stream(mcu, events)
        self.assertEqual(stat["sf_ack_ok_count"], 2)
        self.assertEqual(stat["sf_ack_ok_muted_fast_scheduler_count"], 1)
        self.assertEqual(stat["sf_ack_ok_restore_while_zero_current_count"], 1)
        self.assertEqual(stat["sf_ack_ok_active_frequency_count"], 0)
        self.assertEqual(stat["sf_ack_ok_active_frequencies_hz"], [])
        self.assertEqual(stat["sf_ack_ok_distinct_frequencies_hz"], [50, 400])

    def test_unmapped_overlaps_are_unknown_not_zero(self):
        mcu = synthetic_mcu()
        overlap = coverage_overlaps(None, mcu, [])
        self.assertIsNone(overlap["mcu_sample_count"])
        self.assertIsNone(overlap["ble_event_count"])
        self.assertEqual(len(select_overlap_mcu(mcu, None)["taxel_pc_ns"]), 0)

    def test_full_mcu_stream_counted_once_and_gap_not_filled(self):
        mcu = synthetic_mcu()
        events = [{"event": "sf_ack", "pc_monotonic_ns": 101_000_000_000},
                  {"event": "sf_ack", "pc_monotonic_ns": 111_000_000_000}]
        full = summarize_full_stream(mcu, events)
        self.assertEqual(full["rate_snapshots"], 5)
        self.assertEqual(full["ble_events"], 2)
        first = mapping(100_000_000_000, 102_000_000_000)
        second = mapping(110_000_000_000, 112_000_000_000)
        self.assertEqual(coverage_overlaps(first, mcu, events)["mcu_sample_count"], 3)
        self.assertEqual(coverage_overlaps(second, mcu, events)["mcu_sample_count"], 2)
        gaps = describe_gaps([
            {"record_index": 1, "clock_mapping": first},
            {"record_index": 2, "clock_mapping": second},
        ])
        self.assertEqual(gaps[0]["estimated_gap_s"], 8)
        self.assertFalse(gaps[0]["scope_samples_present"])

    def test_reverse_mapped_records_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "overlap/reverse"):
            describe_gaps([
                {"record_index": 1, "clock_mapping": mapping(100, 200)},
                {"record_index": 2, "clock_mapping": mapping(190, 300)},
            ])

    def test_independent_waveform_and_host_overlap_per_record(self):
        mcu = synthetic_mcu()
        events = [{"event": "sf_ack", "pc_monotonic_ns": 101_000_000_000,
                   "frequency_hz": 100, "status": "OK"},
                  {"event": "sf_ack", "pc_monotonic_ns": 111_000_000_000,
                   "frequency_hz": 100, "status": "OK"}]
        ack = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "analysis").mkdir()
            first_t = np.arange(0, 2, .001)
            first_v = np.zeros_like(first_t)
            first_v[100::100] = 8
            second_t = np.arange(0, 2, .001)
            second_v = np.zeros_like(second_t)
            second_v[200::200] = 8

            def fake_wave(path):
                return ((first_t, first_v) if "00001" in str(path)
                        else (second_t, second_v))

            def fake_clock(_root, record_index=1):
                return (mapping(100_000_000_000, 102_000_000_000)
                        if record_index == 1
                        else mapping(110_000_000_000, 112_000_000_000))

            with patch("oscilloscope.analyze_integrated_session.load_wave",
                       side_effect=fake_wave), patch(
                           "oscilloscope.analyze_integrated_session.tentative_host_map",
                           side_effect=fake_clock), patch(
                           "oscilloscope.analyze_integrated_session.make_plots",
                           return_value=[]), patch(
                           "oscilloscope.analyze_integrated_session.plot_frequency_comparison",
                           return_value="frequency.png"), patch(
                           "oscilloscope.analyze_integrated_session.plot_dynamic_tracking",
                           return_value="tracking.png"):
                rows = [
                    analyze_one(root, make_scope_record(idx), mcu, events, ack,
                                threshold_v=2, deadtime_ms=1,
                                taxel=11, zoom_start=None, zoom_sec=.5,
                                window_s=1, step_s=.25,
                                tolerance_hz=3, tolerance_fraction=.05)
                    for idx in (1, 2)
                ]
            self.assertEqual([x["scope"]["pulse_detection"]["positive_rising_events"]
                              for x in rows], [19, 9])
            self.assertEqual([x["mcu"]["samples"] for x in rows], [3, 2])
            self.assertEqual([x["matrix32_commands"]["events"] for x in rows],
                             [1, 1])
            self.assertTrue((root / "analysis/record_00001/report.md").exists())
            self.assertTrue((root / "analysis/record_00002/report.md").exists())

    def test_session_only_aggregates_distinct_scope_records(self):
        mcu = synthetic_mcu()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "mcu_matrix32").mkdir()
            (root / "mcu_matrix32/synthetic_tactile_rates.csv").write_text(
                "synthetic fixture: loader is mocked"
            )
            meta = {"run_id": "test_two", "status": "complete",
                    "records": [make_scope_record(1), make_scope_record(2)]}
            def record_result(root, record, *args, **kwargs):
                idx = record["index"]
                return {
                    "record_index": idx,
                    "scope": {"duration_s": 2., "pulse_detection": {
                        "positive_rising_events": 20}},
                    "mcu": {"samples": 3 if idx == 1 else 2},
                    "matrix32_commands": {"events": 1},
                    "scope_to_host_overlap": {
                        "mcu_sample_count": 3 if idx == 1 else 2,
                        "ble_event_count": 1},
                    "clock_mapping": mapping(
                        (100 if idx == 1 else 110)*1_000_000_000,
                        (102 if idx == 1 else 112)*1_000_000_000),
                    "record_report": str(root / "analysis" / f"record_{idx:05d}" / "report.md"),
                    "record_summary": str(root / "analysis" / f"record_{idx:05d}" / "summary.json"),
                    "dynamic_tracking": {
                        "classified": 10, "compatible": 8,
                        "discrepant": 2, "not_comparable": 0},
                }
            with patch("oscilloscope.analyze_integrated_session.choose_recording",
                       return_value="synthetic"), patch(
                           "oscilloscope.analyze_integrated_session.load_recordings",
                           return_value=mcu), patch(
                           "oscilloscope.analyze_integrated_session.plot_traces"), patch(
                           "oscilloscope.analyze_integrated_session.analyze_one",
                           side_effect=record_result):
                result = analyze_session(root, meta)
            self.assertEqual(result["total_observed_scope_s"], 4.)
            self.assertEqual(result["total_scope_positive_events"], 40)
            self.assertEqual(result["mcu_full_stream"]["rate_snapshots"], 5)
            self.assertEqual(len(result["inter_record_gaps"]), 1)
            self.assertEqual(result["inter_record_gaps"][0]["estimated_gap_s"], 8.)
            self.assertIn("Record 2", (root / "analysis/integrated_report.md").read_text())
            self.assertFalse(result["continuous_time_series_were_concatenated"])


if __name__ == "__main__":
    unittest.main()
