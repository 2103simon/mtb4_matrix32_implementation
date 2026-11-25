"""Prevent detector reruns from overwriting operator review evidence."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from oscilloscope.review_pulse_detection import analyze_record
from experiment_analysis.paths import use_results_root


class ReviewEvidencePreservationTests(unittest.TestCase):
    def test_rerun_preserves_all_existing_evidence_and_approved_figure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "raw"
            root.mkdir()
            (root / "metadata.json").write_text('{"status": "complete", "records": []}')
            output = Path(tmp) / "results" / "raw" / "analysis/record_00001/pulse_review"
            (output / "figures").mkdir(parents=True)
            names = (
                "audit.json", "accepted_onsets.csv", "periods_unverified.csv",
                "rejected_onsets.csv", "suppressed_onsets.csv",
                "threshold_sensitive_onsets.csv", "review_manifest.json",
                "review_decisions.csv", "manual_additions.csv",
                "review_approval.json", "approved_onsets.csv",
                "approved_intervals.csv", "report.md", "figures/waveform_full.png",
            )
            expected = {name: f"Existing reviewed evidence: {name}\n".encode()
                        for name in names}
            for name, content in expected.items():
                (output / name).write_bytes(content)
            record = {"index": 1, "status": "captured", "channels": {
                "CH1": {"path": "waveforms/original_ch1.csv"},
            }}
            with patch("oscilloscope.review_pulse_detection.load_wave") as load:
                with use_results_root(Path(tmp) / "results"):
                    with self.assertRaisesRegex(FileExistsError, "existing pulse-review evidence"):
                        analyze_record(root, record)
                load.assert_not_called()
            self.assertEqual(
                {name: (output / name).read_bytes() for name in names}, expected)

    def test_partial_review_evidence_without_manifest_also_survives(self):
        for name in ("review_decisions.csv", "manual_additions.csv",
                     "review_approval.json", "approved_onsets.csv",
                     "approved_intervals.csv"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "raw"
                root.mkdir()
                (root / "metadata.json").write_text('{"status": "complete", "records": []}')
                output = Path(tmp) / "results" / "raw" / "analysis/record_00001/pulse_review"
                output.mkdir(parents=True)
                file = output / name
                file.write_text("Operator evidence must survive\n")
                record = {"index": 1, "status": "captured", "channels": {
                    "CH1": {"path": "waveforms/original_ch1.csv"},
                }}
                with use_results_root(Path(tmp) / "results"):
                    with self.assertRaisesRegex(FileExistsError, "Preserve/archive"):
                        analyze_record(root, record)
                self.assertEqual(file.read_text(), "Operator evidence must survive\n")


if __name__ == "__main__":
    unittest.main()
