"""Offline MCU scope-window plot regressions (no hardware)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from oscilloscope.plot_mcu_scope_coverage import render


class McuScopePlotTests(unittest.TestCase):
    def test_missing_clock_map_returns_no_plots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "mcu_matrix32").mkdir()
            (root / "mcu_matrix32" / "test_tactile_rates.csv").write_text("")
            with patch("oscilloscope.plot_mcu_scope_coverage.load_recordings",
                       return_value={"taxel_pc_ns": np.array([100, 200], dtype=np.int64)}):
                self.assertEqual(render(root), [])
            self.assertFalse((root / "analysis").exists())

    def test_full_window_and_cropped_plot_use_same_pc_axis(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "mcu_matrix32").mkdir()
            (root / "mcu_matrix32" / "test_tactile_rates.csv").write_text("")
            (root / "epoch_clock_analysis.json").write_text(json.dumps(
                {"records": [{"record_index": 1}]}))
            ns = np.array([1_000_000_000, 2_000_000_000, 3_000_000_000,
                           4_000_000_000, 5_000_000_000], dtype=np.int64)
            mapping = {"first_sample_pc_monotonic_ns_est": 2_000_000_000,
                       "last_sample_pc_monotonic_ns_est": 4_000_000_000}
            with (patch("oscilloscope.plot_mcu_scope_coverage.load_recordings",
                        return_value={"taxel_pc_ns": ns}),
                  patch("oscilloscope.plot_mcu_scope_coverage.tentative_host_map",
                        return_value=mapping),
                  patch("oscilloscope.plot_mcu_scope_coverage.plot_traces") as plot):
                paths = render(root, taxel=11)
            self.assertEqual(len(paths), 2)
            self.assertEqual(plot.call_count, 2)
            full, crop = plot.call_args_list
            self.assertEqual(full.kwargs["x_axis"], "pc")
            self.assertEqual(full.kwargs["scope_windows"], [(1000.0, 3000.0)])
            self.assertEqual(crop.kwargs["interval"], (1000.0, 3000.0))
            self.assertEqual(crop.kwargs["scope_windows"], [(1000.0, 3000.0)])
            self.assertEqual(full.kwargs["selected_taxel"], 11)


if __name__ == "__main__":
    unittest.main()
