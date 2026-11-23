"""Hardware-free contract tests for the integrated bench recorder."""
import io
import tempfile
from unittest.mock import MagicMock
from pathlib import Path
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "oscilloscope"))
from record_integrated_matrix32 import main, parser, plan, run, StimulationChild
from record_scope import configure


class IntegratedBenchTests(unittest.TestCase):
    def test_default_dry_run_no_device_access(self):
        with patch("record_integrated_matrix32.run",
                   side_effect=AssertionError("Devices opened")):
            with redirect_stdout(io.StringIO()):
                result = main([])
        self.assertEqual(result["channels"], [1, 2])
        self.assertEqual(result["nominal_window_s"], 20)
        self.assertFalse(result["matrix32_armed"])
        self.assertEqual(result["trigger_position_pct"], 0)
        self.assertEqual(result["epoch_sync_count"], 30)
        self.assertEqual(result["acquisition_order"],
                         "controller_ready_then_pre_sync_then_arm_scope")

    def test_multiple_record_plan_and_bounds(self):
        p = parser()
        args = p.parse_args(["--count", "9"])
        requested = plan(args, p)
        self.assertEqual(requested["scope_record_count"], 9)
        self.assertEqual(requested["nominal_observed_scope_duration_s"], 180)
        self.assertFalse(requested["single_scope_record"])
        self.assertTrue(requested["records_are_independent"])
        self.assertTrue(requested["matrix32_session_continuous_across_record_gaps"])
        for count in ("0", "13"):
            with self.subTest(count=count):
                with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                    plan(p.parse_args(["--count", count]), p)

    def test_n_records_keep_one_controller_and_separate_sync_epochs(self):
        class FakeNano:
            def __init__(self, *args):
                self.sequence = 0
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def synchronize(self):
                return "PING_ACK"
            def command(self, command, timeout=None):
                if command == "INFO":
                    return "FW GRAY_TC_V1 PIN 7 RUN 0"
                return "ACK " + command
            def binary_sync_exchange(self):
                self.sequence += 1
                return {"sequence_id": self.sequence, "type": "sync_exchange"}

        class FakeScope:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        class FakeChild:
            instances = []
            def __init__(self, *args, **kwargs):
                self.process = None
                self.start_failure = __import__("threading").Event()
                self.starts = 0
                self.stops = 0
                FakeChild.instances.append(self)
            def start(self):
                self.starts += 1
                self.process = object()
                return 9876
            def wait_ready(self, timeout):
                return {"mcu_configured_observed_monotonic_ns": 1}
            def check(self):
                if self.stops:
                    raise AssertionError("Controller stopped before last capture")
            def stop(self):
                self.stops += 1
                return {"started": True, "stop_write_reported": True}

        def fake_acquire(*args, **kwargs):
            return {"stopped": True}

        with tempfile.TemporaryDirectory() as folder:
            p = parser()
            args = p.parse_args(["--count", "3", "--sync-count", "8",
                                 "--output-root", folder])
            requested = plan(args, p)
            with (
                patch("record_integrated_matrix32.NanoLog", FakeNano),
                patch("record_integrated_matrix32.StimulationChild", FakeChild),
                patch("record_integrated_matrix32.resolve_port", return_value="nano"),
                patch("record_integrated_matrix32.resolve_scope_device", return_value="scope"),
                patch("record_integrated_matrix32.open", return_value=FakeScope(), create=True),
                patch("record_integrated_matrix32.query", return_value="scope"),
                patch("record_integrated_matrix32.verify_scope_identity", return_value="TEK"),
                patch("record_integrated_matrix32.configure"),
                patch("record_integrated_matrix32.scope_settings", return_value={}),
                patch("record_integrated_matrix32.verify"),
                patch("record_integrated_matrix32.acquire", side_effect=fake_acquire) as acq,
                patch("record_integrated_matrix32.capture", return_value={"ok": True}) as transfer,
                patch("record_integrated_matrix32.validate_csv", return_value={"ok": True}),
                patch("record_integrated_matrix32.decode_csv",
                      return_value={"decoded_frame_count": 199}),
                patch("analyze_gray_epoch_experiment.analyze",
                      return_value=(
                          {"decoded_frames_total": 597},
                          {"qualified_provisional_records": 3,
                           "exploratory_local_fit_records": 0,
                           "records": []},
                      )),
            ):
                with redirect_stdout(io.StringIO()):
                    result = run(args, requested)

            self.assertEqual(result["status"], "complete")
            self.assertEqual(len(result["records"]), 3)
            self.assertEqual([r["index"] for r in result["records"]], [1, 2, 3])
            self.assertEqual([r["status"] for r in result["records"]],
                             ["captured"] * 3)
            self.assertEqual(acq.call_count, 3)
            self.assertEqual(transfer.call_count, 6)
            self.assertEqual(FakeChild.instances[-1].starts, 1)
            self.assertEqual(FakeChild.instances[-1].stops, 1)
            self.assertEqual(len(result["sync_measurements"]), 48)
            for i, record in enumerate(result["records"]):
                self.assertEqual(len(record["clock_epoch"]["pre_sync_sequence_ids"]), 8)
                self.assertEqual(len(record["clock_epoch"]["post_sync_sequence_ids"]), 8)
                self.assertEqual(record["clock_epoch"]["epoch_id"], i + 1)
                self.assertEqual(
                    record["channels"]["CH1"]["path"],
                    f"waveforms/record_{i+1:05d}_ch1.csv",
                )
                self.assertEqual(
                    record["channels"]["CH2"]["path"],
                    f"waveforms/record_{i+1:05d}_ch2.csv",
                )

    def test_explicit_two_gate_arming(self):
        for argv in (["--arm"], ["--arm", "--run"],
                     ["--dummy-load-confirmed", "--run"]):
            with self.subTest(argv=argv):
                p = parser()
                with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                    plan(p.parse_args(argv), p)
        p = parser()
        data = plan(p.parse_args(["--arm", "--run",
                                   "--dummy-load-confirmed"]), p)
        self.assertTrue(data["matrix32_armed"])

    def test_scope_ch1_matrix32_ch2_nano(self):
        p = parser()
        args = p.parse_args([])
        plan(args, p)
        with patch("record_scope.write") as write:
            configure(None, args)
        commands = [call.args[1] for call in write.call_args_list]
        self.assertIn("SELect:CH1 ON", commands)
        self.assertIn("SELect:CH2 ON", commands)
        self.assertIn("CH1:SCAle 10", commands)
        self.assertIn("CH2:SCAle 2", commands)
        self.assertIn("TRIGger:A:EDGE:SOUrce CH2", commands)
        self.assertLess(commands.index("HORizontal:SCALe 2"),
                        commands.index("HORizontal:POSition 0"))
        self.assertLess(commands.index("HORizontal:RECOrdlength 1000000"),
                        commands.index("HORizontal:POSition 0"))

    def test_supported_scope_memory_depths_and_longer_capture_plans(self):
        for points in (1000, 10000, 100000, 1000000, 5000000, 10000000, 20000000):
            with self.subTest(points=points):
                p = parser()
                data = plan(p.parse_args(["--points", str(points)]), p)
                self.assertEqual(data["points"], points)
        for points, us, seconds in (
                (5000000, 3000000, 30),
                (20000000, 6000000, 60)):
            with self.subTest(seconds=seconds):
                p = parser()
                data = plan(p.parse_args([
                    "--points", str(points), "--time-div-us", str(us)
                ]), p)
                self.assertEqual(data["nominal_window_s"], seconds)

    def test_invalid_limits(self):
        p = parser()
        for args in (["--points", "1"], ["--points", "123456"],
                     ["--points", "20000001"], ["--time-div-us", "7000000"],
                     ["--sync-count", "2"],
                     ["--trigger-position-pct", "1000"],
                     ["--ch1-v-div", "nan"], ["--stale-ms", "2"],
                     ["--acquire-timeout", "nan"]):
            with self.subTest(args=args):
                with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                    plan(p.parse_args(args), p)

    def test_controller_ready_is_required_before_scope_capture(self):
        class RunningProcess:
            returncode = None
            def poll(self):
                return None
        child = StimulationChild(Path("not_used"), arm=True, stale_ms=250)
        child.process = RunningProcess()
        with self.assertRaisesRegex(TimeoutError, "no ACK-verified Matrix32 START"):
            child.wait_ready(0.01)
        child.mcu_ready_observed_ns = 123
        child.mcu_ready.set()
        # MCU handshake alone cannot authorize an armed scope recording.
        with self.assertRaisesRegex(TimeoutError, "no ACK-verified Matrix32 START"):
            child.wait_ready(0.01)
        child.matrix32_started_observed_ns = 456
        child.matrix32_started.set()
        evidence = child.wait_ready(0.01)
        self.assertEqual(evidence["mcu_configured_observed_monotonic_ns"], 123)
        self.assertEqual(evidence["matrix32_start_observed_monotonic_ns"], 456)
        self.assertFalse(evidence["startup_electrical_output_verified"])

    def test_dry_run_ready_only_needs_mcu_configuration(self):
        class RunningProcess:
            def poll(self):
                return None
        child = StimulationChild(Path("not_used"), arm=False, stale_ms=250)
        child.process = RunningProcess()
        child.mcu_ready_observed_ns = 42
        child.mcu_ready.set()
        self.assertEqual(
            child.wait_ready(0.01)["mcu_configured_observed_monotonic_ns"], 42
        )

    def test_controller_timeout_invalid(self):
        p = parser()
        for value in ("nan", "4", "121"):
            with self.subTest(value=value):
                with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                    plan(p.parse_args(["--controller-ready-timeout", value]), p)

    def test_child_stop_no_start(self):
        child = StimulationChild(Path("not_used"), arm=False, stale_ms=250)
        self.assertEqual(child.stop(), {"started": False})


if __name__ == "__main__":
    unittest.main()
