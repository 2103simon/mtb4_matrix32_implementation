"""Hardware-free synthetic regression for offline Matrix32 SF ACK observability."""
import os
from pathlib import Path
import subprocess
import sys
import unittest

import numpy as np

from oscilloscope.audit_ack_physical_observability import (
    classify_ack_rows, cycle_context, run,
)


def ev(t, name, **k):
    return {'pc_monotonic_ns': int(t*1e9), 'event': name, **k}


class AckObservabilityTests(unittest.TestCase):
    axis = (0.0, 20.0, 0, 20_000_000_000)

    def test_matching_cycle_requires_prior_guard_and_next_ack_bound(self):
        events = [ev(0, 'start_write_completed', frequency_hz=10),
                  ev(1, 'sf_requested', frequency_hz=10, source_sample_ns=int(.9e9)),
                  ev(1.01, 'sf_ack', frequency_hz=10, status='OK'),
                  ev(2.5, 'sf_ack', frequency_hz=20, status='OK')]
        times = np.array([1.15, 1.25, 1.35, 3.0])
        rows = classify_ack_rows(events, times, self.axis, 1.8)
        self.assertEqual(rows[0]['classification'], 'STABLE_MATCHING_PERIOD_OBSERVED')
        self.assertEqual(rows[0]['matching_stable_intervals'], 2)
        self.assertFalse(rows[0]['physical_execution_proven'])

    def test_superseded_before_next_pulse_not_failure(self):
        events = [ev(0, 'start_write_completed', frequency_hz=10),
                  ev(1.0, 'sf_ack', frequency_hz=25, status='OK'),
                  ev(1.04, 'sf_ack', frequency_hz=30, status='OK')]
        rows = classify_ack_rows(events, np.array([1.2, 1.24, 1.28]), self.axis, 1.8)
        self.assertEqual(rows[0]['classification'], 'NEXT_SF_ACK_BEFORE_NEXT_PULSE')

    def test_muted_ack_never_counted_physical_400hz(self):
        events = [ev(0, 'start_write_completed', frequency_hz=15),
                  ev(1.0, 'sc_ack', current_command=0, status='OK'),
                  ev(1.1, 'sf_ack', frequency_hz=400, status='OK')]
        rows = classify_ack_rows(events, np.array([.1, .2, 2.]), self.axis, 1.8)
        self.assertEqual(rows[0]['classification'], 'ACK_WHILE_CURRENT_MUTED')

    def test_outside_scope_is_not_physical_observation(self):
        events = [ev(0, 'start_write_completed', frequency_hz=10),
                  ev(25, 'sf_ack', frequency_hz=80, status='OK')]
        rows = classify_ack_rows(events, np.array([.1, .2]), self.axis, 1.8)
        self.assertEqual(rows[0]['classification'], 'OUTSIDE_SCOPE_WINDOW')

    def test_short_intervals_crossing_ack_boundary_not_matching(self):
        events = [ev(0, 'start_write_completed', frequency_hz=10),
                  ev(1.0, 'sf_ack', frequency_hz=10, status='OK'),
                  ev(1.155, 'sf_ack', frequency_hz=20, status='OK')]
        rows = classify_ack_rows(events, np.array([1.1, 1.2, 1.25]), self.axis, 1.8)
        self.assertEqual(rows[0]['matching_stable_intervals'], 0)

    def test_cycle_context_labels_long_gap_not_a_low_rate(self):
        events = [ev(0, 'start_write_completed', frequency_hz=30)]
        rows = cycle_context(events, np.array([1.0, 4.3, 4.34]), self.axis,
                             onset_ids=['A', 'B', 'C'])
        self.assertEqual(rows[0]['assessment'], 'LONG_PAUSE_NOT_CYCLE')
        self.assertEqual(rows[0]['start_id'], 'A')
        self.assertEqual(rows[1]['end_id'], 'C')

    def test_module_help_runs_without_acquisition_hardware(self):
        result = subprocess.run(
            [sys.executable, '-m', 'oscilloscope.audit_ack_physical_observability',
             '--help'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--run-dir', result.stdout)

    @unittest.skipUnless(os.getenv('MTB4_ACK_AUDIT_SAMPLE_RUN'),
                         'Set MTB4_ACK_AUDIT_SAMPLE_RUN for real signed-data regression')
    def test_real_source_locked_run_preserves_review_and_counts(self):
        root = Path(os.environ['MTB4_ACK_AUDIT_SAMPLE_RUN'])
        review = root/'analysis/record_00001/pulse_review/review_approval.json'
        before = review.read_bytes()
        result = run(root)
        self.assertEqual(before, review.read_bytes())
        self.assertEqual(result['approved_biphasic_negative_onsets'], 418)
        self.assertEqual(result['sf_ack_success_all_session'], 266)
        self.assertEqual(result['sf_ack_success_in_provisional_scope_window'], 246)
        self.assertEqual(result['ack_classifications_in_scope'][
            'STABLE_MATCHING_PERIOD_OBSERVED'], 59)
        self.assertEqual(result['sf400_muted_scheduler_policy_ack_count'], 22)
        self.assertTrue((root/'analysis/ack_physical_observability/final_report.md').is_file())
        self.assertTrue((root/'analysis/ack_physical_observability/restart_02_detail.png').is_file())


if __name__ == '__main__':
    unittest.main()
