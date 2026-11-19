"""Verify self-describing run identifiers and reproducible experiment manifests."""
import csv
import json
import tempfile
import unittest
from pathlib import Path

import matplotlib
matplotlib.use("Agg")

from recording.data_recorder import DataRecorder
from analyze_mcu_recording import (
    _experiment_label, choose_recording, load_recordings, plot_traces,
)


class RecordingManifestTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "tactile_encoding": {"raThreshold": 5, "saMaxFrequencyHz": 200},
            "stimulation_source": {"taxel": 7, "encoding": "ra_i"},
            "serial": {"port": "/dev/ttyACM0", "input_baud_rate": 115200},
            "clock_compensation": {"enabled": True},
        }
        self.acks = {
            "raThreshold": 5,
            "saMaxFrequencyHz": 200,
            "acknowledged": True,
        }

    def test_id_and_metadata_match_rate_and_figure_names(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = DataRecorder(directory)
            run_id = recorder.make_recording_id(self.config, "20261119_101112")
            self.assertEqual(run_id, "20261119_101112_ra5_sa200Hz_t7_rai")
            recorder.start(timestamp=run_id, config=self.config, applied_settings=self.acks)
            for i in range(5):
                recorder.record_tactile_rates({
                    "corrected_timestamp_ms": i * 10,
                    "raw_timestamp_ms": i * 10,
                    "pc_arrival_monotonic_ns": i * 10_000_000,
                    "taxels": [i] * 12,
                    "ra_rates_hz": [i * 100.0] * 12,
                    "sa_rates_hz": [i * 10.0] * 12,
                })
            recorder.stop()

            root = Path(directory)
            settings_path = root / f"{run_id}_settings.json"
            settings = json.loads(settings_path.read_text())
            self.assertEqual(settings["recording_id"], run_id)
            self.assertEqual(settings["mcu_configuration_confirmed"], self.acks)
            self.assertEqual(settings["configuration"], self.config)
            self.assertEqual(choose_recording(root), run_id)
            data = load_recordings(root, run_id)
            self.assertEqual(data["mode"], "rates")
            self.assertIn("RA threshold=5", _experiment_label(data))
            self.assertIn("selected taxel=7", _experiment_label(data))
            figure = root / f"{run_id}_traces.png"
            plot_traces(data, figure, selected_taxel=7)
            self.assertGreater(figure.stat().st_size, 1000)

            # Run IDs never silently overwrite previous experiments.
            with self.assertRaises(FileExistsError):
                DataRecorder(directory).start(timestamp=run_id, config=self.config)

    def test_legacy_recorder_has_no_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = DataRecorder(directory)
            recorder.start(timestamp="legacy")
            recorder.stop()
            self.assertTrue((Path(directory) / "legacy_tactile_rates.csv").exists())
            self.assertFalse((Path(directory) / "legacy_settings.json").exists())


if __name__ == "__main__":
    unittest.main()
