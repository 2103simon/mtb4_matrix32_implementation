"""Hardware-free tests for source-locked S01/S02 pulse review approvals."""
from pathlib import Path
import csv
import json
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oscilloscope.pulse_review_workflow import (
    SOURCE_TABLES, make_scaffold, write_csv, review_candidates
)
from oscilloscope.apply_pulse_review import (
    apply, verified_approval, load_approved_onsets
)


class HumanPulseReviewTests(unittest.TestCase):
    def _fixture(self, root):
        root = Path(root)
        src = root / "record_00001_ch1.csv"
        src.write_text("time_s,voltage_v,raw\n0,0,0\n1,0,0\n")
        output = root / "analysis"
        output.mkdir()
        result = {
            "record_index": 1,
            "source_ch1": "waveforms/record_00001_ch1.csv",
            "detector": "S02-negative-event-v5-noise-tolerant-quiet",
            "accepted_pulses": [
                {"id": "P00001", "sample_index": 10, "time_s": .1,
                 "voltage_v": -12., "status": "CANDIDATE_REVIEW"},
                {"id": "P00002", "sample_index": 30, "time_s": .3,
                 "voltage_v": -12., "status": "RELAXED_QUIET_REVIEW"},
            ],
            "rejected": [
                {"sample_index": 20, "time_s": .2, "voltage_v": -12.,
                 "status": "REJECTED_INSUFFICIENT_BASELINE"}
            ],
            "suppressed": [
                {"sample_index": 21, "time_s": .21, "voltage_v": -3.,
                 "status": "SUPPRESSED_CLOSE_CROSSING"}
            ],
        }
        for filename in SOURCE_TABLES:
            (output / filename).write_text("test\n")
        manifest = make_scaffold(output, src, result)
        (output / "audit.json").write_text(json.dumps({
            "capture_start_s": 0.0, "capture_end_s": 1.0
        }))
        return src, output, manifest

    def test_unresolved_flagged_candidates_block_global_signoff(self):
        with tempfile.TemporaryDirectory() as td:
            src, output, manifest = self._fixture(td)
            with self.assertRaisesRegex(ValueError, "Review incomplete"):
                apply(output, src, reviewer="Operator",
                      confirm_accepted=True, confirm_complete=True)
            self.assertFalse((output / "review_approval.json").exists())
            self.assertEqual(len(manifest["candidates"]), 4)

    def test_source_locked_included_rejected_and_excluded_suppressed(self):
        with tempfile.TemporaryDirectory() as td:
            src, output, _ = self._fixture(td)
            review = output / "review_decisions.csv"
            with review.open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            for row in rows:
                if row["kind"] == "rejected":
                    row["decision"] = "INCLUDE"
                    row["note"] = "Confirmed missing pulse in close-up"
                elif row["kind"] == "suppressed":
                    row["decision"] = "EXCLUDE"
                    row["note"] = "Secondary phase crossing"
            write_csv(review, rows, rows[0].keys())
            approval = apply(output, src, reviewer="Operator",
                             confirm_accepted=True, confirm_complete=True)
            self.assertEqual(approval["included_onsets"], 3)
            self.assertEqual(approval["included_intervals"], 2)
            verified, onsets = load_approved_onsets(output, src)
            self.assertTrue(verified["human_signed_off"])
            self.assertEqual([row["id"] for row in onsets],
                             ["P00001", "R00001", "P00002"])
            self.assertEqual(len(list(csv.DictReader(
                (output / "approved_intervals.csv").open()))), 2)

            # User edits after signoff invalidate it, even if numbers match.
            review.write_text(review.read_text() + "\n")
            self.assertIsNone(verified_approval(output))
            with self.assertRaisesRegex(ValueError, "No valid source"):
                load_approved_onsets(output, src)

    def test_rerun_does_not_overwrite_review_and_detects_source_change(self):
        with tempfile.TemporaryDirectory() as td:
            src, output, _ = self._fixture(td)
            review = output / "review_decisions.csv"
            review.write_text(review.read_text() + "# operator note\n")
            before = review.read_text()
            result = {
                "record_index": 1,
                "source_ch1": "waveforms/record_00001_ch1.csv",
                "detector": "S02-negative-event-v5-noise-tolerant-quiet",
                "accepted_pulses": [
                    {"id": "P00001", "sample_index": 10, "time_s": .1,
                     "voltage_v": -12., "status": "CANDIDATE_REVIEW"},
                    {"id": "P00002", "sample_index": 30, "time_s": .3,
                     "voltage_v": -12., "status": "RELAXED_QUIET_REVIEW"},
                ],
                "rejected": [
                    {"sample_index": 20, "time_s": .2, "voltage_v": -12.,
                     "status": "REJECTED_INSUFFICIENT_BASELINE"}
                ],
                "suppressed": [
                    {"sample_index": 21, "time_s": .21, "voltage_v": -3.,
                     "status": "SUPPRESSED_CLOSE_CROSSING"}
                ],
            }
            make_scaffold(output, src, result)
            self.assertEqual(review.read_text(), before)
            src.write_text("time_s,voltage_v,raw\n0,1,0\n1,0,0\n")
            with self.assertRaisesRegex(RuntimeError, "stale"):
                make_scaffold(output, src, result)


if __name__ == "__main__":
    unittest.main()
