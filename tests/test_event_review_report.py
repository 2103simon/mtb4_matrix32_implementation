"""Offline tests for S01/S02-style per-record event tables."""
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from oscilloscope.event_review_report import associate, generate


class EventContextTests(unittest.TestCase):
    def test_no_clock_mapping_keeps_context_unknown(self):
        self.assertEqual(associate({
            "mapping": None, "times": [], "rows": [],
            "events": [], "event_times": [],
        }, 0.25, 0, 1)[-1], "NO_CLOCK_MAP")

    def test_provisional_last_ack_and_fresh_mcu_sample(self):
        info = {
            "mapping": {
                "first_sample_pc_monotonic_ns_est": 1000000000,
                "last_sample_pc_monotonic_ns_est": 2000000000,
                "status": "unqualified",
            },
            "times": [1400000000], "rows": [(1400000000, 35.0, 72.0)],
            "events": [
                {"pc_monotonic_ns": 1200000000, "event": "sf_ack",
                 "status": "OK", "frequency_hz": 72},
                {"pc_monotonic_ns": 1300000000, "event": "sc_ack",
                 "status": "OK", "current_command": 0},
            ],
            "event_times": [1200000000, 1300000000],
        }
        self.assertEqual(associate(info, .5, 0, 1),
                         (35, 72, 72, 0, "PROVISIONAL_unqualified"))

    def test_complete_table_flags_and_operator_decision_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "analysis" / "record_00001" / "pulse_review"
            output.mkdir(parents=True)
            with (output / "review_decisions.csv").open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=[
                    "id", "kind", "sample_index", "time_s",
                    "detector_status", "decision", "note"])
                writer.writeheader()
                writer.writerows([
                    {"id": "P00001", "kind": "accepted", "sample_index": 1,
                     "time_s": .1, "detector_status": "CANDIDATE_REVIEW",
                     "decision": "", "note": ""},
                    {"id": "P00002", "kind": "accepted", "sample_index": 3,
                     "time_s": .2, "detector_status": "RELAXED_QUIET_REVIEW",
                     "decision": "INCLUDE", "note": "reviewed"},
                    {"id": "R00001", "kind": "rejected", "sample_index": 2,
                     "time_s": .15, "detector_status": "REJECTED_INSUFFICIENT_BASELINE",
                     "decision": "EXCLUDE", "note": "secondary waveform"},
                ])
            result = {
                "capture_start_s": 0., "capture_end_s": 1.,
                "accepted_pulses": [
                    {"id": "P00001", "sample_index": 1,
                     "time_s": .1, "voltage_v": -10.,
                     "status": "CANDIDATE_REVIEW"},
                    {"id": "P00002", "sample_index": 3,
                     "time_s": .2, "voltage_v": -11.,
                     "status": "RELAXED_QUIET_REVIEW"},
                ],
                "rejected": [{"sample_index": 2, "time_s": .15,
                              "voltage_v": -6.,
                              "status": "REJECTED_INSUFFICIENT_BASELINE"}],
                "suppressed": [],
            }
            with patch("oscilloscope.event_review_report.context", return_value={
                "mapping": None, "taxel": 11, "encoding": "sa_ii",
                "times": [], "rows": [], "events": [], "event_times": [],
            }):
                markdown = generate(root, {"index": 1}, result, output,
                                    {"source_ch1_sha256": "abc"})
            rows = list(csv.DictReader((output / "pulse_inventory_context.csv").open()))
            self.assertEqual(len(rows), 3)
            self.assertEqual([r["id"] for r in rows], ["P00001", "R00001", "P00002"])
            self.assertEqual(rows[0]["flag"], "None")
            self.assertEqual(rows[0]["decision"], "")
            self.assertEqual(rows[1]["decision"], "EXCLUDE")
            self.assertEqual(rows[2]["flag"], "RELAXED_QUIET_REVIEW")
            self.assertAlmostEqual(float(rows[2]["preceding_period_ms"]), 100)
            self.assertEqual(rows[0]["clock_context_status"], "NO_CLOCK_MAP")
            self.assertTrue(any("R00001" in line for line in markdown))


if __name__ == "__main__":
    unittest.main()
