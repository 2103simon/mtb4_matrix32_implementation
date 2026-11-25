"""Session reports must describe the current approved record inventory."""
import unittest

from oscilloscope.approved_frequency_analysis import session_report


def summary(index, onsets):
    intervals = max(0, onsets - 1)
    return {
        "record_index": index, "onset_count": onsets,
        "interval_count": intervals,
        "period_ms": {"median": 10 if intervals else None},
        "inverse_period_hz": {"median": 100 if intervals else None},
        "clock_status": "unqualified",
    }


class ApprovedSessionReportingTests(unittest.TestCase):
    def test_new_single_record_report_has_no_historical_empty_record_claim(self):
        text = session_report([summary(1, 4)], 1)
        self.assertNotIn("Record 5", text)
        self.assertNotIn("empty onset inventories", text)
        self.assertIn("Approved records:** 1 / 1", text)

    def test_empty_inventory_note_names_only_actual_empty_records(self):
        text = session_report([summary(2, 0), summary(7, 1), summary(9, 0)], 3)
        self.assertIn("empty onset inventories (records 2, 9)", text)
        self.assertIn("do not independently certify zero current", text)
        self.assertNotIn("records 2, 7", text)
        self.assertNotIn("Record 5", text)


if __name__ == "__main__":
    unittest.main()
