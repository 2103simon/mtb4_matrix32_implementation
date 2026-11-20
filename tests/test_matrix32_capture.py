"""Recording mode creates paired CSV / JSONL with common monotonic clock."""
import json
import tempfile
import unittest
from pathlib import Path

from recording.matrix32_trace import OptionalCapture

CONFIG = {
    "tactile_encoding": {"raThreshold": 3, "saMaxFrequencyHz": 150},
    "stimulation_source": {"taxel": 0, "encoding": "sa_ii"},
    "stimulation_control": {"record": True, "armed": True,
                            "zero_request_strategy": "SC1=0_then_SF=2; restore_SF_then_SC1"},
}


class RecordingModeTest(unittest.TestCase):
    def test_no_record_creates_no_files(self):
        with tempfile.TemporaryDirectory() as folder:
            capture = OptionalCapture(False, folder, CONFIG)
            capture.start({"acknowledged": True})
            capture.emit("sf_requested", frequency_hz=50)
            capture.close()
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_capture_uses_one_run_id_and_monotonic_timestamps(self):
        with tempfile.TemporaryDirectory() as folder:
            capture = OptionalCapture(True, folder, CONFIG)
            try:
                capture.start({"acknowledged": True})
                name = capture.recorder.recording_id
                capture.emit("sc_requested", channel=1, current_command=0,
                             source_sample_ns=123456789)
                capture.emit("sc_ack", channel=1, current_command=0, status="OK",
                             evidence={"ack_monotonic_s": 12.5,
                                       "write_begin_monotonic_s": 12.4})
            finally:
                capture.close()
            folder = Path(folder)
            manifest = json.loads((folder / f"{name}_settings.json").read_text())
            self.assertEqual(manifest["files"]["matrix32_commands"],
                             f"{name}_matrix32_commands.jsonl")
            self.assertTrue((folder / f"{name}_tactile_rates.csv").exists())
            rows = [json.loads(line) for line in
                    (folder / f"{name}_matrix32_commands.jsonl").read_text().splitlines()]
            self.assertEqual([r["event"] for r in rows],
                             ["recording_started", "sc_requested",
                              "sc_ack", "recording_closed"])
            self.assertEqual(rows[1]["source_sample_ns"], 123456789)
            self.assertEqual(rows[2]["status"], "OK")
            self.assertIsInstance(rows[2]["pc_monotonic_ns"], int)
            self.assertTrue(all(rows[i]["pc_monotonic_ns"] <= rows[i+1]["pc_monotonic_ns"]
                                for i in range(len(rows)-1)))


if __name__ == "__main__":
    unittest.main()
