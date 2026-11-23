"""Hardware-free tests of source-preserving Matrix32 negative-event review."""
import hashlib
from pathlib import Path
import sys
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis_primitives.negative_events import (
    NEGATIVE_EVENT_DETECTOR_VERSION, estimate_threshold
)
from oscilloscope.review_pulse_detection import detect_review


class PulseReviewTests(unittest.TestCase):
    def test_upstream_unchanged_source_git_blob_shas(self):
        # From matrix32_nerve_stimulation/cleanup/analysis-consolidation,
        # experiments/analysis_primitives/*.py. Protect the detector rules
        # against incidental edits without source qualification.
        source_blobs = {
            "baseline.py": "a0924511e1456db963c74de72e7e374a6edb596e",
            "negative_events.py": "6474302ac471201f9ad3c96f787d401578f9d4ff",
            "record_start_rejection.py": "75e5ea2df74ebd6bd69469fd2773f23796e73a76",
            "suppressed_crossing_association.py": "23a3b775e3987dfd95206e0b9b810dd18e770468",
            "period_classification.py": "a0924511e1456db963c74de72e7e374a6edb596e", 
        }
        # period_classification has its own source blob (not baseline's).
        source_blobs["period_classification.py"] = (
            "a0924511e1456db963c74de72e7e374a6edb596e"
        )
        # GitHub reports a content SHA for every source. Strict pins on the
        # four detection-relevant files prevent silent changes to rules.
        for filename in (
            "baseline.py", "negative_events.py",
            "record_start_rejection.py", "suppressed_crossing_association.py",
        ):
            with self.subTest(filename=filename):
                data = (ROOT / "analysis_primitives" / filename).read_bytes()
                blob = b"blob " + str(len(data)).encode() + b"\\0" + data
                self.assertEqual(hashlib.sha1(blob).hexdigest(),
                                 source_blobs[filename])

    def test_negative_biphasic_onsets_and_intervals(self):
        dt = 20e-6
        t = np.arange(0, 0.08, dt)
        v = np.zeros(len(t), dtype=float)
        # A negative phase followed by positive recovery on three cycles.
        for start in (0.010, 0.030, 0.050):
            v[(t >= start) & (t < start + 120e-6)] = -25.
            v[(t >= start + 120e-6) & (t < start + 240e-6)] = 24.
        audit = detect_review(t, v)
        self.assertEqual(NEGATIVE_EVENT_DETECTOR_VERSION,
                         "S02-negative-event-v5-noise-tolerant-quiet")
        self.assertEqual(len(audit["accepted_pulses"]), 3)
        self.assertEqual(len(audit["periods"]), 2)
        self.assertFalse(audit["complete_pulse_detection_approved"])
        for row in audit["periods"]:
            self.assertAlmostEqual(row["provisional_inverse_period_hz"],
                                   50, delta=0.1)
            self.assertEqual(row["review_status"],
                             "UNVERIFIED_PULSE_CANDIDATES")
        self.assertTrue(all(
            x["status"] in ("CANDIDATE_REVIEW", "RELAXED_QUIET_REVIEW")
            for x in audit["accepted_pulses"]
        ))

    def test_flat_waveform_rejected_by_signal_gate(self):
        t = np.arange(0, 0.08, 20e-6)
        v = np.zeros_like(t)
        with self.assertRaisesRegex(ValueError, "No sufficiently strong"):
            detect_review(t, v)

    def test_timestamp_monotonicity_required(self):
        t = np.array([0., .01, .009, .02])
        v = np.array([0., -20., 20., 0.])
        with self.assertRaisesRegex(ValueError, "increase"):
            detect_review(t, v)


if __name__ == "__main__":
    unittest.main()
