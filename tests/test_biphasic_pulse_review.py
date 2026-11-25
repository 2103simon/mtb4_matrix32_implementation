"""Hardware-free biphasic pulse filtering and safe review replacement tests."""
import csv
from pathlib import Path
import tempfile
import unittest

import numpy as np

from oscilloscope.review_pulse_detection import (
    archive_unapproved_report, detect_review,
)


class BiphasicReviewTests(unittest.TestCase):
    def test_only_negative_then_sustained_positive_enters_pulse_inventory(self):
        dt = 20e-6
        t = np.arange(0, 0.075, dt)
        v = np.zeros(len(t))
        for start in (0.010, 0.030, 0.050):
            v[(t >= start) & (t < start + 120e-6)] = -25
        for start in (0.010, 0.030):
            v[(t >= start + 120e-6) & (t < start + 240e-6)] = 24
        # A single positive sample near the third dip is NOT a positive phase.
        spike = int(np.searchsorted(t, 0.050 + 200e-6))
        v[spike] = 24
        report = detect_review(t, v)
        self.assertEqual(report["negative_onset_candidates_before_phase_gate"], 3)
        self.assertEqual(report["negative_only_rejected_count"], 1)
        self.assertEqual(len(report["accepted_pulses"]), 2)
        self.assertEqual(len(report["periods"]), 1)
        self.assertEqual(report["accepted_pulses"][0]["phase_status"],
                         "BIPHASIC_CONFIRMED")
        self.assertLess(report["accepted_pulses"][0]["positive_delay_us"], 550)
        missing = [r for r in report["rejected"]
                   if r["status"] == "REJECTED_MISSING_POSITIVE_PHASE"]
        self.assertEqual(len(missing), 1)
        self.assertAlmostEqual(missing[0]["time_s"], 0.050, delta=dt)
        self.assertIn("NEGATIVE_ONLY_EVENTS_REQUIRE_EXPLICIT_REVIEW",
                      report["flags"])

    def test_late_positive_phase_outside_window_not_accepted(self):
        t = np.arange(0, .05, 10e-6)
        v = np.zeros(len(t))
        v[(t >= .01) & (t < .01015)] = -25
        v[(t >= .0108) & (t < .0110)] = 25
        report = detect_review(t, v)
        self.assertEqual(len(report["accepted_pulses"]), 0)
        self.assertEqual(report["negative_only_rejected_count"], 1)


class ReviewReplacementTests(unittest.TestCase):
    def _review(self, folder, decision="", note=""):
        folder.mkdir(parents=True)
        (folder / "review_manifest.json").write_text('{"schema":"matrix32_pulse_review_v1"}')
        with (folder / "review_decisions.csv").open("w", newline="") as file:
            w = csv.DictWriter(file, fieldnames=(
                "id", "kind", "sample_index", "time_s",
                "detector_status", "decision", "note"))
            w.writeheader()
            w.writerow({"id":"P00001", "kind":"accepted", "sample_index":10,
                        "time_s":.01, "detector_status":"CANDIDATE_REVIEW",
                        "decision":decision, "note":note})
        (folder / "manual_additions.csv").write_text("id,time_s,decision,note\n")
        (folder / "report.md").write_text("Original report")
        return folder

    def test_unsigned_unedited_report_archived_not_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = self._review(Path(temp) / "pulse_review")
            backup = archive_unapproved_report(folder)
            self.assertFalse(folder.exists())
            self.assertTrue(backup.name.startswith("pulse_review_unapproved_backup_"))
            self.assertEqual((backup / "report.md").read_text(), "Original report")

    def test_approved_report_never_replaced(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = self._review(Path(temp) / "pulse_review")
            (folder / "review_approval.json").write_text("{}")
            with self.assertRaisesRegex(FileExistsError, "approved evidence"):
                archive_unapproved_report(folder)
            self.assertTrue((folder / "report.md").exists())

    def test_operator_edits_never_replaced(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = self._review(Path(temp) / "pulse_review", "EXCLUDE")
            with self.assertRaisesRegex(FileExistsError, "Operator edits"):
                archive_unapproved_report(folder)
            self.assertTrue((folder / "report.md").exists())

    def test_manual_additions_never_replaced(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = self._review(Path(temp) / "pulse_review")
            (folder / "manual_additions.csv").write_text(
                "id,time_s,decision,note\nM00001,.04,INCLUDE,manual\n")
            with self.assertRaisesRegex(FileExistsError, "Manual onset additions"):
                archive_unapproved_report(folder)


if __name__ == "__main__":
    unittest.main()
