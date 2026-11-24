"""Offline finite-record, transfer provenance and large-file validation tests."""
import argparse
import csv
import struct
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch

from oscilloscope import record_scope as scope
from oscilloscope import tek_mdo_usb as usb


def baseline_args():
    return argparse.Namespace(
        points=10_000_000, time_div_us=2_000_000, channels=[1, 2],
        scales={1: 10.0, 2: 2.0}, trigger_channel=2,
        trigger_edge="rising", trigger_level=1.0, trigger_position_pct=0.0)


def baseline_settings():
    return {
        "identity": "TEKTRONIX,MDO4104B-6,C000000,FW",
        "time_div_s": "2.0000E+0", "record_length": "10000000",
        "sample_rate": "500.0000E+3", "acquisition_mode": "SAMPLE",
        "acquisition_stopafter": "SEQUENCE", "fast_acquisition": "OFF",
        "trigger_mode": "NORMAL", "trigger_source": "CH2",
        "trigger_slope": "RISE", "trigger_level_v": "1.0000",
        "horizontal_delay_mode": "OFF", "trigger_position_pct": "0",
        "ch1_v_div": "10.0000", "ch1_enabled": "ON",
        "ch2_v_div": "2.0000", "ch2_enabled": "ON",
    }


class ScopeReadbackTests(unittest.TestCase):
    def test_10m_20s_readback_and_wrong_rate_or_fractional_count(self):
        scope.verify(baseline_settings(), baseline_args())
        for key, value, message in (
                ("sample_rate", "1.0000E+6", "Sample rate mismatch"),
                ("sample_rate", "nan", "Non-finite"),
                ("record_length", "10000000.25", "record length mismatch")):
            with self.subTest(key=key, value=value):
                settings = {**baseline_settings(), key: value}
                with self.assertRaisesRegex(RuntimeError, message):
                    scope.verify(settings, baseline_args())

    def test_readback_rounding_is_bounded_and_format_independent(self):
        args = baseline_args()
        args.time_div_us = 3_000_000
        settings = {**baseline_settings(), "time_div_s": "3.0000",
                    "sample_rate": "333.3333E+3"}
        # Rounding in a numeric response must not reject the nominal setting.
        scope.verify(settings, args)
        settings["sample_rate"] = "333.3000E+3"
        with self.assertRaisesRegex(RuntimeError, "Sample rate mismatch"):
            scope.verify(settings, args)
        for token in ("500000", "5E5", "500000.000"):
            with self.subTest(token=token):
                settings = {**baseline_settings(), "sample_rate": token}
                scope.verify(settings, baseline_args())
                settings["time_div_s"] = "3.0000"
                with self.assertRaisesRegex(RuntimeError, "Sample rate mismatch"):
                    scope.verify(settings, args)

    def test_probe_configuration_is_read_and_preserved(self):
        observed = []

        def query(_, command):
            observed.append(command)
            values = {"CH1:PROBe:GAIN?": "0.1000E+0", "CH1:COUPling?": "DC",
                      "CH1:TERmination?": "1.0000E+6", "CH1:OFFSet?": "0.0",
                      "CH1:POSition?": "-1.0000", "CH1:INVert?": "OFF"}
            return values.get(command, "1")

        with patch.object(scope, "query", side_effect=query), \
             patch.object(scope, "write") as write:
            settings = scope.scope_settings(None, [1])
        write.assert_not_called()
        self.assertEqual(settings["ch1_probe_gain"], "0.1000E+0")
        self.assertEqual(settings["ch1_probe_attenuation"], 10.0)
        self.assertEqual(settings["ch1_coupling"], "DC")
        self.assertEqual(settings["ch1_termination_ohm"], "1.0000E+6")
        self.assertIn("CH1:INVert?", observed)

    def test_preamble_scaling_matches_probe_adjusted_channel_scale(self):
        settings = {**baseline_settings(), "ch1_probe_gain": "0.1000E+0"}
        for width, multiplier in ((1, "400.0000E-3"), (2, "1.562500E-3")):
            with self.subTest(width=width):
                transfer = {"waveform_preamble": {
                    "source": "CH1", "voltage_units": "V",
                    "bytes_per_sample": width,
                    "ymult_v_per_code": float(multiplier), "ymult_scpi": multiplier}}
                report = scope.verify_waveform_scaling(transfer, settings)
                self.assertEqual(report["preamble_v_div"], 10.0)
                self.assertEqual(report["scope_reported_probe_attenuation"], 10.0)
                self.assertFalse(report["physical_probe_calibration_verified"])
                # A stale/different channel preamble must not silently produce
                # apparently valid voltages for the requested 10 V/div record.
                transfer["waveform_preamble"]["ymult_v_per_code"] *= 2
                with self.assertRaisesRegex(RuntimeError, "waveform scale mismatch"):
                    scope.verify_waveform_scaling(transfer, settings)

    def test_abbreviated_scale_readback_does_not_hide_scale_mismatch(self):
        transfer = {"waveform_preamble": {
            "source": "CH1", "voltage_units": "V", "bytes_per_sample": 2,
            "ymult_v_per_code": 1.7e-3, "ymult_scpi": "1.7E-3"}}
        for token in ("1E1", "10.0000"):
            with self.subTest(token=token):
                settings = {**baseline_settings(), "ch1_v_div": token,
                            "ch1_probe_gain": "0.1"}
                with self.assertRaisesRegex(RuntimeError, "waveform scale mismatch"):
                    scope.verify_waveform_scaling(transfer, settings)


class WaveformValidationTests(unittest.TestCase):
    def test_original_file_and_correct_first_to_last_span(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "waveform.csv"
            original = "time_s,voltage_v,raw\n-0.000002,-1,-1\n0,0,0\n0.000002,1,1\n"
            path.write_text(original)
            result = scope.validate_csv(path, 3)
            self.assertEqual(result["samples"], 3)
            self.assertEqual(result["sample_interval_s"], 2e-6)
            self.assertEqual(result["span_s"], 4e-6)
            self.assertEqual(path.read_text(), original)

    def test_invalid_time_grid_nonfinite_samples_and_count_fail(self):
        cases = [
            (["0,0", "0,1", "2,0"], 3, "Non-increasing"),
            (["0,0", "1,1", "3,0"], 3, "non-uniform"),
            (["0,0", "nan,1", "2,0"], 3, "non-finite"),
            (["0,0", "1,inf", "2,0"], 3, "non-finite"),
            (["0,0", "1,1"], 3, "expected 3 samples"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "waveform.csv"
            for rows, points, message in cases:
                with self.subTest(rows=rows):
                    original = "time_s,voltage_v\n" + "\n".join(rows) + "\n"
                    path.write_text(original)
                    with self.assertRaisesRegex(RuntimeError, message):
                        scope.validate_csv(path, points)
                    self.assertEqual(path.read_text(), original)

    def test_csv_validation_memory_does_not_grow_with_samples(self):
        # A list of 20K DictReader rows needs several MB. Streaming must stay
        # below 1 MB so the 10M/channel recording is not a multi-GB allocation.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "waveform.csv"
            with path.open("w") as file:
                file.write("time_s,voltage_v,raw\n")
                for i in range(20_000):
                    file.write(f"{i * 2e-6},0,0\n")
            tracemalloc.start()
            try:
                result = scope.validate_csv(path, 20_000)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertEqual(result["samples"], 20_000)
            self.assertLess(peak, 1_000_000)

    def test_10m_timing_uses_n_minus_one_span_and_rejects_wrong_duration(self):
        args = baseline_args()
        interval = 2e-6
        span = (args.points - 1) * interval
        timing = {"samples": args.points, "span_s": span,
                  "first_time_s": 0.0, "last_time_s": span}
        transfer = {"waveform_preamble": {
            "xincr_s": interval, "xincr_scpi": "2.0000E-6"}}
        report = scope.verify_waveform_timing(timing, baseline_settings(), args, transfer)
        self.assertEqual(report["nominal_window_s"], 20.0)
        self.assertAlmostEqual(report["expected_first_to_last_span_s"], 19.999998)
        self.assertFalse(report["physical_clock_calibration_verified"])
        timing["span_s"] = timing["last_time_s"] = 20.2
        with self.assertRaisesRegex(RuntimeError, "Waveform duration mismatch"):
            scope.verify_waveform_timing(timing, baseline_settings(), args, transfer)

    def test_short_scpi_tokens_cannot_accept_a_22s_record_as_20s(self):
        args = baseline_args()
        span = (args.points - 1) * 2.2e-6
        timing = {"samples": args.points, "span_s": span,
                  "first_time_s": 0.0, "last_time_s": span}
        transfer = {"waveform_preamble": {
            "xincr_s": 2.2e-6, "xincr_scpi": "2.2E-6"}}
        for token in ("5E5", "500000", "500.0000E+3"):
            with self.subTest(token=token):
                settings = {**baseline_settings(), "sample_rate": token}
                with self.assertRaisesRegex(RuntimeError, "Waveform duration mismatch"):
                    scope.verify_waveform_timing(timing, settings, args, transfer)


class WaveformTransferTests(unittest.TestCase):
    def responses(self, **overrides):
        return {
            "DATA:SOURCE?": "CH1", "WFMPRE:YUNit?": '"V"',
            "WFMPRE:XINcr?": "2.0000E-6", "WFMPRE:XZEro?": "0.0",
            "WFMPRE:PT_Off?": "1", "WFMPRE:YMUlt?": "0.1",
            "WFMPRE:YOFf?": "2", "WFMPRE:YZEro?": "1.0",
            "WFMPRE:BYT_Nr?": "2", "WFMPRE:BYT_Or?": "MSB",
            "WFMPRE:BN_Fmt?": "RI", **overrides,
        }

    def test_signed_samples_endianness_scaling_and_original_codes(self):
        codes = [-32768, -12, 0, 32767]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ch1.csv"
            for endian, name in ((">", "MSB"), ("<", "LSB")):
                with self.subTest(order=name):
                    responses = self.responses(**{"WFMPRE:BYT_Or?": name})
                    with patch.object(usb, "query", side_effect=lambda _, cmd: responses[cmd]), \
                         patch.object(usb, "write"), patch.object(
                             usb, "binary_block", return_value=struct.pack(endian + "4h", *codes)) as block:
                        report = usb.capture(None, 1, len(codes), path)
                    block.assert_called_once_with(None, expected_bytes=8)
                    with path.open() as file:
                        rows = list(csv.DictReader(file))
                    self.assertEqual([int(row["raw"]) for row in rows], codes)
                    self.assertEqual([float(row["time_s"]) for row in rows],
                                     [-2e-6, 0.0, 2e-6, 4e-6])
                    self.assertEqual([float(row["voltage_v"]) for row in rows],
                                     [(code - 2) * 0.1 + 1 for code in codes])
                    self.assertEqual(report["binary_transfer"]["decoded_samples"], 4)
                    self.assertEqual(report["binary_transfer"]["storage_rail_code_count"], 2)
                    self.assertEqual(report["waveform_preamble"]["source"], "CH1")
                    self.assertEqual(report["waveform_preamble"]["xincr_scpi"], "2.0000E-6")

    def test_wrong_source_or_nonvoltage_units_cannot_publish_a_waveform(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ch1.csv"
            for key, value in (("DATA:SOURCE?", "CH2"), ("WFMPRE:YUNit?", '"A"')):
                with self.subTest(key=key):
                    responses = self.responses(**{key: value})
                    with patch.object(usb, "query", side_effect=lambda _, cmd: responses[cmd]), \
                         patch.object(usb, "write"), patch.object(usb, "binary_block") as block:
                        with self.assertRaises(RuntimeError):
                            usb.capture(None, 1, 3, path)
                    block.assert_not_called()
                    self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
