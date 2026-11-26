"""Synthetic tests for command-chain evidence, without hardware."""
import unittest

from experiment_analysis.analyze_command_chain import (
    transaction_rows, sample_coverage, pair_transactions, physical_disagreements,
)


def event(name, t, **details):
    return {"event": name, "pc_monotonic_ns": t, **details}


class CommandChainTests(unittest.TestCase):
    def test_integer_target_and_host_latency(self):
        events = [
            event("sf_requested", 1002000000, source_sample_ns=1000000000, frequency_hz=81),
            event("sf_ack", 1015000000, frequency_hz=81, status="OK",
                  evidence={"write_begin_monotonic_s": 1.005, "ack_monotonic_s": 1.014}),
        ]
        rows, issues = transaction_rows(events, [1000000000], [81])
        self.assertFalse(issues)
        r = rows[0]
        self.assertEqual(r["source_match"], "MATCH")
        self.assertEqual(r["source_to_request_ms"], 2)
        self.assertEqual(r["request_to_write_ms"], 3)
        self.assertEqual(r["write_to_ack_ms"], 9)

    def test_ambiguous_arrival_batch_and_mute_scheduler(self):
        events = [
            event("sf_requested", 10, source_sample_ns=1, frequency_hz=80),
            event("sf_ack", 20, frequency_hz=80, status="OK"),
            event("sf_requested", 30, source_sample_ns=25, frequency_hz=400,
                  zero_current_workaround=True),
            event("sf_ack", 40, frequency_hz=400, status="OK"),
        ]
        rows, _ = transaction_rows(events, [1, 1, 25], [80, 81, 0])
        self.assertEqual(rows[0]["source_match"], "SOURCE_MISSING_OR_AMBIGUOUS")
        self.assertEqual(rows[1]["source_match"], "MUTE_SCHEDULER")
        self.assertIsNone(rows[0]["write_to_ack_ms"])

    def test_unreferenced_change_during_busy_window(self):
        events = [event("sf_requested", 10, source_sample_ns=5, frequency_hz=20),
                  event("sf_ack", 30, frequency_hz=20, status="OK")]
        tx, _ = transaction_rows(events, [1, 5, 20, 40], [10, 20, 30, 30])
        rows = sample_coverage([1, 5, 20, 40], [10, 20, 30, 30], tx, events)
        self.assertEqual(rows[1]["status"], "CHANGE_REFERENCED_BY_REQUEST")
        self.assertEqual(rows[2]["status"], "CHANGE_UNREFERENCED_DURING_TRANSACTION")
        self.assertEqual(rows[3]["status"], "UNCHANGED_TARGET")

    def test_overlapping_requests_not_paired(self):
        events = [event("sf_requested", 10, frequency_hz=20),
                  event("sf_requested", 11, frequency_hz=30),
                  event("sf_ack", 20, frequency_hz=30, status="OK")]
        pairs, issues = pair_transactions(events)
        self.assertFalse(pairs)
        self.assertEqual(len(issues), 2)

    def test_physical_link_does_not_use_future_ack(self):
        p = {"controller_target_assessment": "STABLE_ACROSS_TESTED_OFFSETS",
             "ack_assessment": "STABLE_ACROSS_TESTED_OFFSETS",
             "controller_target_hz_if_stable": "81", "ack_expected_hz_if_stable": "98",
             "start_host_lower_ns": "100", "start_host_upper_ns": "110",
             "record": "1", "start_onset_id": "1", "start_scope_s": ".1",
             "measured_inverse_period_hz": "98.039"}
        tx = [{"family": "sf", "success": True, "terminal_ns": 120,
               "requested_sf_hz": 98}]
        rows = physical_disagreements([p], tx)
        self.assertFalse(rows[0]["target_equals_ack"])
        self.assertIsNone(rows[0]["preceding_matching_sf_terminal_ns"])


if __name__ == "__main__":
    unittest.main()
