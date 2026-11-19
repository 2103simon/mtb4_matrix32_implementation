"""Smoke tests for the updated MCU rate-recording plots."""
import csv
import tempfile
import unittest
from pathlib import Path

import matplotlib
matplotlib.use("Agg")

from analyze_mcu_recording import choose_recording, load_recordings, plot_traces


class AnalyzeRateRecordingTest(unittest.TestCase):
    def test_rate_csv_preferred_over_header_only_legacy_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stem = "20261119_082300"
            with (root / f"{stem}_tactile_rates.csv").open("w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(
                    ["corrected_timestamp_ms", "raw_timestamp_ms", "pc_arrival_monotonic_ns"]
                    + [f"taxel_{i}" for i in range(12)]
                    + [f"ra_rate_hz_{i}" for i in range(12)]
                    + [f"sa_rate_hz_{i}" for i in range(12)]
                )
                for n in range(10):
                    writer.writerow(
                        [n * 10, n * 10, n * 10_000_000]
                        + [n] * 12
                        + [float(n) * 100] * 12
                        + [float(n) * 10] * 12
                    )
            (root / f"{stem}_taxel.csv").write_text(
                "corrected_timestamp_ms,raw_timestamp_ms\n"
            )
            (root / f"{stem}_events.csv").write_text(
                "raw_timestamp_ms,corrected_timestamp_ms,id0,id1\n"
            )
            self.assertEqual(choose_recording(root), stem)
            data = load_recordings(root, stem)
            self.assertEqual(data["mode"], "rates")
            self.assertEqual(data["ra_rate_hz"].shape, (10, 12))
            self.assertEqual(data["sa_rate_hz"].shape, (10, 12))
            self.assertEqual(data["ra_rate_hz"][9, 0], 900)
            self.assertEqual(data["sa_rate_hz"][9, 0], 90)
            output = root / "all.png"
            plot_traces(data, output)
            self.assertGreater(output.stat().st_size, 1000)
            selected = root / "taxel3.png"
            plot_traces(data, selected, selected_taxel=3, interval=(10, 60))
            self.assertGreater(selected.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
