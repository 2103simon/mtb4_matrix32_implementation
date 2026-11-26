"""Hardware-free contract tests for the integrated bench recorder."""
import io
import json
import tempfile
import threading
from unittest.mock import MagicMock
from pathlib import Path
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

sys.path.append(str(Path(__file__).resolve().parents[1] / "oscilloscope"))
from record_integrated_matrix32 import (
    main, parser, plan, run, StimulationChild, acquire_buffered_record,
    verify_nano_info, verify_gray_frame_evidence, NANO_BUFFER_POLL_BUDGET_S,
)
from record_scope import configure
from oscilloscope.operator_prompts import OperatorPrompts, normalize_operator_prompts


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
        self.assertEqual(result["nano_mode_required"], "10HZ_HW_TCA_BUFFERED")
        self.assertTrue(result["nano_buffered_frames_required"])
        self.assertFalse(result["operator_prompts"]["enabled"])
        self.assertEqual(result["operator_prompts"]["schedule"], [])
        self.assertEqual(result["acquisition_order"],
                         "controller_ready_then_pre_sync_then_arm_scope")

    def test_optional_prompt_dictionary_is_planned_without_device_access(self):
        schedule = (Path(__file__).resolve().parents[1]
                    / "config/operator_prompts_resume_20s.json")
        with patch("record_integrated_matrix32.run",
                   side_effect=AssertionError("Devices opened")):
            with redirect_stdout(io.StringIO()):
                result = main(["--points", "10000000",
                               "--operator-prompts", str(schedule)])
        prompts = result["operator_prompts"]
        self.assertTrue(prompts["enabled"])
        self.assertEqual([row["at_s"] for row in prompts["schedule"]],
                         [0, 3, 6, 10, 13])
        self.assertTrue(prompts["repeat_each_record"])
        self.assertFalse(prompts["scope_clock_synchronized"])

    def test_invalid_prompt_configuration_fails_before_devices_open(self):
        with tempfile.TemporaryDirectory() as folder:
            valid = Path(folder) / "valid.json"
            valid.write_text('{"0": "HOLD"}')
            too_late = Path(folder) / "late.json"
            too_late.write_text('{"20": "RELEASE"}')
            for argv in (
                    ["--operator-prompts", str(too_late)],
                    ["--operator-prompts", str(Path(folder) / "missing.json")],
                    ["--operator-prompts", str(valid), "--trigger-position-pct", "10"]):
                with self.subTest(argv=argv), redirect_stdout(io.StringIO()), \
                        redirect_stderr(io.StringIO()), \
                        patch("record_integrated_matrix32.run") as run_mock:
                    with self.assertRaises(SystemExit):
                        main(["--run", *argv])
                    run_mock.assert_not_called()

    def test_prompt_preview_runs_timer_without_devices_or_recording_files(self):
        with tempfile.TemporaryDirectory() as folder:
            schedule = Path(folder) / "prompts.json"
            schedule.write_text('{"0": "HOLD", "0.05": "RELEASE"}')
            output_root = Path(folder) / "recordings"
            terminal = io.StringIO()
            with patch("record_integrated_matrix32.run",
                       side_effect=AssertionError("Real recorder started")), \
                    patch("record_integrated_matrix32.resolve_port",
                          side_effect=AssertionError("Serial resolved")), \
                    patch("record_integrated_matrix32.resolve_scope_device",
                          side_effect=AssertionError("Scope resolved")), \
                    redirect_stdout(terminal):
                result = main(["--preview-prompts", "--operator-prompts", str(schedule),
                               "--time-div-us", "20000", "--output-root", str(output_root)])
            self.assertTrue(result["delivery_complete"])
            self.assertEqual(result["printed_count"], 2)
            self.assertFalse(output_root.exists())
            self.assertIn("REHEARSAL COMPLETE", terminal.getvalue())
            self.assertNotIn('"schema_version"', terminal.getvalue())

    def test_preview_rejects_hardware_flags_and_missing_schedule(self):
        schedule = (Path(__file__).resolve().parents[1]
                    / "config/operator_prompts_resume_20s.json")
        for options in ([], ["--operator-prompts", str(schedule), "--run"],
                        ["--operator-prompts", str(schedule), "--arm"],
                        ["--operator-prompts", str(schedule), "--count", "2"]):
            with self.subTest(options=options), redirect_stderr(io.StringIO()), \
                    redirect_stdout(io.StringIO()), \
                    patch("record_integrated_matrix32.run") as run_mock:
                with self.assertRaises(SystemExit):
                    main(["--preview-prompts", *options])
                run_mock.assert_not_called()

    def test_guided_main_saves_plan_and_routine_output_without_terminal_clutter(self):
        def fake_session(args, requested, root, *, console):
            self.assertIsNotNone(console)
            print("[NANO] routine device detail", flush=True)
            print("[WARNING] visible warning", flush=True)
            console.status("PRESS AND HOLD", "Waiting for the timed instructions.")
            return {"run_dir": str(root)}
        schedule = (Path(__file__).resolve().parents[1]
                    / "config/operator_prompts_resume_20s.json")
        with tempfile.TemporaryDirectory() as folder:
            terminal = io.StringIO()
            with patch("record_integrated_matrix32._run_session", side_effect=fake_session), \
                    redirect_stdout(terminal):
                result = main(["--run", "--operator-prompts", str(schedule),
                               "--output-root", folder])
            log = (Path(result["run_dir"]) / "console.log").read_text()
            self.assertIn('"schema_version"', log)
            self.assertIn("routine device detail", log)
            self.assertNotIn('"schema_version"', terminal.getvalue())
            self.assertNotIn("routine device detail", terminal.getvalue())
            self.assertIn("PRESS AND HOLD", terminal.getvalue())
            self.assertIn("visible warning", terminal.getvalue())

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
                self.buffer_open = False
                self.history = []
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def synchronize(self):
                return "PING_ACK"
            def command(self, command, timeout=None):
                if command == "INFO":
                    return ("ACK INFO 1 FW GRAY_TC_V1 PIN 7 BITS 16 FRAME_US 100000 "
                            "FRAME_MODE 10HZ_HW_TCA_BUFFERED RUN 0")
                return "ACK " + command
            def binary_sync_exchange(self):
                if self.buffer_open:
                    raise AssertionError("SYNC during active frame buffering")
                self.history.append("SYNC")
                self.sequence += 1
                return {"sequence_id": self.sequence, "type": "sync_exchange"}
            def buffered_capture_begin(self):
                if self.buffer_open:
                    raise AssertionError("Already buffering")
                self.buffer_open = True
                self.history.append("BUFFER_BEGIN")
                return {"begin_ack": "ACK BUFFER_BEGIN 1 CAP 256", "capacity": 256}
            def buffered_capture_end_and_dump(self, begun):
                if not self.buffer_open or begun["capacity"] != 256:
                    raise AssertionError("Invalid Nano buffer state")
                self.buffer_open = False
                self.history.append("BUFFER_END_DUMP")
                return {"end_ack": "ACK BUFFER_END 1 COUNT 201 LOST 0 CAP 256",
                        "dump_ack": "ACK BUFFER_DUMP 1 COUNT 201 LOST 0",
                        "frames_received": 201, "frames_lost": 0,
                        "frame_payloads_validated": True,
                        "first_frame_counter": 100, "last_frame_counter": 300}

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
                patch("record_integrated_matrix32.verify_waveform_timing", return_value={"verified": True}),
                patch("record_integrated_matrix32.verify_waveform_scaling", return_value={"verified": True}),
                patch("record_integrated_matrix32.decode_csv",
                      return_value={"decoded_frame_count": 199,
                                    "waveform": {"first_time_s": 0, "last_time_s": 19.999998},
                                    "frames": [{"counter_mod_65536": i,
                                                "gray_hex": f"{i ^ (i >> 1):04X}",
                                                "scope_start_s": (i - 100) * 0.1,
                                                "scope_end_s": (i - 100) * 0.1 + 0.087}
                                               for i in range(101, 300)]}),
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
            self.assertTrue(result["clock_frame_evidence_complete"])
            self.assertEqual(len(result["records"]), 3)
            self.assertEqual([r["index"] for r in result["records"]], [1, 2, 3])
            self.assertEqual([r["status"] for r in result["records"]],
                             ["captured"] * 3)
            self.assertEqual(acq.call_count, 3)
            self.assertEqual(transfer.call_count, 6)
            for rec in result["records"]:
                self.assertEqual(rec["nano_frame_buffer"]["frames_received"], 201)
                self.assertEqual(rec["nano_frame_buffer"]["frames_lost"], 0)
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

    def test_10m_20sec_is_supported_without_arming(self):
        p = parser()
        args = p.parse_args(["--points", "10000000", "--count", "1"])
        result = plan(args, p)
        self.assertEqual(result["points"], 10000000)
        self.assertEqual(result["nominal_window_s"], 20.0)
        self.assertEqual(result["channels"], [1, 2])
        self.assertFalse(result["matrix32_armed"])
        self.assertEqual(result["nano_buffer_capacity_required"], 256)
        self.assertLess(result["effective_acquire_timeout_upper_bound_s"], 25.6)

    def test_capture_before_start_requires_explicit_armed_bench(self):
        p = parser()
        for argv in (["--capture-before-start"],
                     ["--run", "--capture-before-start"],
                     ["--run", "--arm", "--capture-before-start"]):
            with self.subTest(argv=argv):
                with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                    plan(p.parse_args(argv), p)
        data = plan(p.parse_args([
            "--run", "--arm", "--dummy-load-confirmed",
            "--capture-before-start", "--points", "5000000", "--count", "6"
        ]), p)
        self.assertTrue(data["capture_before_matrix32_start"])
        self.assertEqual(data["scope_record_count"], 6)

    def test_capture_before_start_ready_on_mcu_ack_only(self):
        class RunningProcess:
            def poll(self):
                return None

        child = StimulationChild(Path("unused"), arm=True, stale_ms=250,
                                 capture_before_start=True)
        child.process = RunningProcess()
        child.mcu_ready_observed_ns = 123
        child.mcu_ready.set()
        result = child.wait_ready(0.01)
        self.assertTrue(result["scope_capture_precedes_matrix32_start"])
        self.assertIsNone(result["matrix32_start_observed_monotonic_ns"])

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
        self.assertEqual(data["live_transport"], "shared_persistent_sf_sc")
        self.assertNotIn("experimental_shared_sf_sc", data)

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

    def test_supported_memory_depths_and_rejection_of_buffer_overflow_plans(self):
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
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    plan(p.parse_args([
                        "--points", str(points), "--time-div-us", str(us)
                    ]), p)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            plan(parser().parse_args(["--acquire-timeout", "20"]), parser())

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


INFO = ("ACK INFO 2 FW GRAY_TC_V1 PIN 7 FRAME_US 100000 BITS 16 RUN 0 "
        "FRAME_MODE 10HZ_HW_TCA_BUFFERED NEXT 0 OVERRUNS 0")


class BufferedFailureTests(unittest.TestCase):
    def setUp(self):
        self.args = parser().parse_args([])
        plan(self.args, parser())
        self.nano = MagicMock()
        self.nano.buffered_capture_begin.return_value = {
            "begin_ack": "ACK BUFFER_BEGIN 3 CAP 256", "capacity": 256,
        }
        self.nano.buffered_capture_end_and_dump.return_value = {
            "end_ack": "ACK BUFFER_END 4 COUNT 201 LOST 0 CAP 256",
            "dump_ack": "ACK BUFFER_DUMP 5 COUNT 201 LOST 0",
            "frames_received": 201, "frames_lost": 0,
        }
        self.nano.command.return_value = "ACK BUFFER_END 6 COUNT 12 LOST 0 CAP 256"
        self.child = MagicMock()
        self.child.start_failure.is_set.return_value = False
        self.record = {"index": 1, "status": "started"}

    def test_exact_firmware_fields_reject_prefix_matches_and_duplicates(self):
        self.assertEqual(verify_nano_info(INFO)["FRAME_US"], "100000")
        for invalid in (
            INFO.replace("PIN 7", "PIN 70"),
            INFO.replace("FRAME_US 100000", "FRAME_US 1000000"),
            INFO.replace("10HZ_HW_TCA_BUFFERED", "10HZ_HW_TCA_BUFFERED_OLD"),
            INFO + " RUN 1", INFO + " MALFORMED", INFO.replace("RUN 0", "RUN 10"),
        ):
            with self.subTest(reply=invalid), self.assertRaises(RuntimeError):
                verify_nano_info(invalid)

    def test_gray_identity_qualification_is_independent_of_ch1_and_clock_fit(self):
        buffer = {"frames_received": 6, "frames_lost": 0,
                  "frame_payloads_validated": True, "first_frame_counter": 65534}
        def decoded(counters):
            return {"decoded_frame_count": len(counters),
                    "waveform": {"first_time_s": 0, "last_time_s": 0.5},
                    "frames": [{"counter_mod_65536": counter,
                                "gray_hex": f"{counter ^ (counter >> 1):04X}",
                                "scope_start_s": (i + 1) * 0.1,
                                "scope_end_s": (i + 1) * 0.1 + 0.087}
                               for i, counter in enumerate(counters)]}
        valid = verify_gray_frame_evidence(decoded([65535, 0, 1, 2]), buffer)
        self.assertTrue(valid["complete"])
        self.assertFalse(valid["clock_fit_evaluated"])
        for counters in ([], [65535, 1, 2], [0, 0, 1], [10, 11, 12]):
            with self.subTest(counters=counters):
                result = verify_gray_frame_evidence(decoded(counters), buffer)
                self.assertFalse(result["complete"])
                self.assertTrue(result["issues"])
                self.assertFalse(result["direct_ch1_evidence_invalidated"])
        partial = decoded([65535, 0, 1])
        partial["waveform"]["last_time_s"] = 19.999998
        result = verify_gray_frame_evidence(partial, buffer)
        self.assertFalse(result["complete"])
        self.assertIn("cover the waveform", " ".join(result["issues"]))

    def test_success_has_one_acquisition_and_no_cleanup_command(self):
        with patch("record_integrated_matrix32.acquire", return_value={"seen_running": True}) as acquire_mock, \
                patch("record_integrated_matrix32.OperatorPrompts",
                      side_effect=AssertionError("Optional prompts unexpectedly enabled")):
            acquire_buffered_record(self.nano, None, self.args, self.record, self.child)
        timeout = acquire_mock.call_args.args[1]
        self.assertGreater(timeout, 20)
        self.assertLessEqual(timeout, NANO_BUFFER_POLL_BUDGET_S)
        self.assertEqual(self.record["nano_frame_buffer"]["status"], "verified")
        self.nano.buffered_capture_end_and_dump.assert_called_once()
        self.nano.command.assert_not_called()

    def test_prompt_progress_is_logged_and_finished_before_nano_end_dump(self):
        self.args.operator_prompt_schedule = normalize_operator_prompts(
            {0: "HOLD", 3: "RELEASE", 6: "PRESS"}, 20)
        now = [100.0]
        lines = []
        schedulers = []
        def make_prompts(schedule, **kwargs):
            kwargs.update(clock=lambda: now[0], emit=lines.append)
            prompts = OperatorPrompts(schedule, **kwargs)
            start = prompts.start
            prompts.start = lambda observation: start(observation, background=False)
            schedulers.append(prompts)
            return prompts
        def acquiring(scope, timeout, on_progress, on_run_written):
            on_run_written({"run_write_begin_monotonic_s": 99.99,
                            "run_write_end_monotonic_s": 100.0})
            on_progress({"first_trigger_observation": None})
            self.assertEqual(len(lines), 1)
            now[0] = 103.1
            on_progress({"first_trigger_observation": None,
                         "trigger_state_observations": [{"value": "READY"}]})
            schedulers[0].tick()
            now[0] = 106.1
            stopped = {"first_trigger_observation": None,
                       "first_stopped_observation": {"value": "0"}}
            on_progress(stopped)
            return stopped
        def end_dump(buffer):
            self.assertEqual(schedulers[0].metadata["status"], "finished")
            self.assertEqual(len(lines), 2)
            return {"end_ack": "ACK BUFFER_END", "frames_lost": 0}
        self.nano.buffered_capture_end_and_dump.side_effect = end_dump
        with patch("record_integrated_matrix32.OperatorPrompts", side_effect=make_prompts), \
                patch("record_integrated_matrix32.acquire", side_effect=acquiring):
            acquire_buffered_record(self.nano, None, self.args, self.record, self.child)
        evidence = self.record["operator_prompts"]
        self.assertEqual([e["status"] for e in evidence["events"]],
                         ["printed", "printed", "skipped"])
        self.assertEqual(evidence["finish_reason"], "scope_stopped")
        self.assertEqual(evidence["anchor_monotonic_s"], 100.0)
        self.assertFalse(evidence["delivery_complete"])
        self.assertEqual(evidence["printed_count"], 2)
        self.nano.command.assert_not_called()

    def test_enabled_prompts_cancel_on_fault_timeout_and_interrupt_before_cleanup(self):
        self.args.operator_prompt_schedule = normalize_operator_prompts(
            {0: "HOLD", 3: "RELEASE"}, 20)
        for failure in (RuntimeError("Controller reported fault"),
                        TimeoutError("scope timeout"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                self.nano.reset_mock()
                self.child.start_failure.is_set.return_value = False
                self.record = {"index": 1, "status": "started"}
                lines = []
                schedulers = []
                def make_prompts(schedule, **kwargs):
                    kwargs.update(clock=lambda: 100.0, emit=lines.append)
                    prompts = OperatorPrompts(schedule, **kwargs)
                    start = prompts.start
                    prompts.start = lambda observation: start(observation, background=False)
                    schedulers.append(prompts)
                    return prompts
                def acquiring(scope, timeout, on_progress, on_run_written):
                    if isinstance(failure, RuntimeError):
                        self.child.start_failure.is_set.return_value = True
                    on_run_written({"run_write_begin_monotonic_s": 99.99,
                                    "run_write_end_monotonic_s": 100.0})
                    on_progress({"first_trigger_observation": None})
                    raise failure
                def cleanup(*args, **kwargs):
                    self.assertEqual(schedulers[0].metadata["status"], "finished")
                    return "ACK BUFFER_END"
                self.nano.command.side_effect = cleanup
                with patch("record_integrated_matrix32.OperatorPrompts", side_effect=make_prompts), \
                        patch("record_integrated_matrix32.acquire", side_effect=acquiring):
                    with self.assertRaises(type(failure)):
                        acquire_buffered_record(self.nano, None, self.args, self.record, self.child)
                self.assertEqual(self.record["operator_prompts"]["finish_reason"],
                                 "acquisition_failed")
                self.assertEqual(self.record["operator_prompts"]["events"][-1]["status"],
                                 "skipped")
                if isinstance(failure, RuntimeError):
                    self.assertEqual(lines, [])
                self.nano.command.assert_called_once_with("BUFFER_END", timeout=2.0)
                self.nano.buffered_capture_end_and_dump.assert_not_called()

    def test_nano_end_precedes_join_of_a_blocked_prompt_output(self):
        self.args.operator_prompt_schedule = normalize_operator_prompts({0: "HOLD"}, 20)
        entered = threading.Event()
        release = threading.Event()
        schedulers = []
        def blocked_emit(line):
            entered.set()
            if not release.wait(1):
                raise RuntimeError("test output was not released")
        def make_prompts(schedule, **kwargs):
            kwargs["emit"] = blocked_emit
            prompts = OperatorPrompts(schedule, **kwargs)
            schedulers.append(prompts)
            return prompts
        def acquiring(scope, timeout, on_progress, on_run_written):
            import time
            on_run_written({"run_write_end_monotonic_s": time.monotonic()})
            self.assertTrue(entered.wait(0.5))
            return {"seen_running": True}
        def end_dump(buffer):
            self.assertTrue(schedulers[0].finished)
            self.assertIsNone(schedulers[0].metadata["worker_joined"])
            release.set()
            return {"end_ack": "ACK BUFFER_END", "frames_lost": 0}
        self.nano.buffered_capture_end_and_dump.side_effect = end_dump
        try:
            with patch("record_integrated_matrix32.OperatorPrompts", side_effect=make_prompts), \
                    patch("record_integrated_matrix32.acquire", side_effect=acquiring):
                acquire_buffered_record(self.nano, None, self.args, self.record, self.child)
        finally:
            release.set()
            for prompts in schedulers:
                prompts.finish("test_cleanup")
                prompts.join(1)
        self.assertTrue(self.record["operator_prompts"]["worker_joined"])
        self.assertFalse(self.record["operator_prompts"]["delivery_complete"])

    def test_capture_without_started_prompt_timer_records_incomplete_delivery(self):
        self.args.operator_prompt_schedule = normalize_operator_prompts({0: "HOLD"}, 20)
        with patch("record_integrated_matrix32.acquire", return_value={"seen_running": True}):
            acquire_buffered_record(self.nano, None, self.args, self.record, self.child)
        result = self.record["operator_prompts"]
        self.assertEqual(result["printed_count"], 0)
        self.assertEqual(result["cue_count"], 1)
        self.assertFalse(result["delivery_complete"])
        self.assertEqual(self.record["nano_frame_buffer"]["status"], "verified")

    def test_acquisition_errors_and_keyboard_interrupt_end_buffering(self):
        for error in (TimeoutError("scope timeout"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                self.nano.reset_mock()
                with patch("record_integrated_matrix32.acquire", side_effect=error):
                    with self.assertRaises(type(error)) as raised:
                        acquire_buffered_record(self.nano, None, self.args, self.record, self.child)
                self.assertIs(raised.exception, error)
                self.nano.command.assert_called_once_with("BUFFER_END", timeout=2.0)
                self.nano.buffered_capture_end_and_dump.assert_not_called()
                self.assertEqual(self.record["status"], "failed")
                self.assertIn("cleanup_end_ack", self.record["nano_frame_buffer"])

    def test_ambiguous_begin_timeout_still_attempts_end(self):
        self.nano.buffered_capture_begin.side_effect = TimeoutError("lost BEGIN ACK")
        with patch("record_integrated_matrix32.acquire") as acquire_mock:
            with self.assertRaisesRegex(TimeoutError, "lost BEGIN ACK"):
                acquire_buffered_record(self.nano, None, self.args, self.record, self.child)
        acquire_mock.assert_not_called()
        self.nano.buffered_capture_begin.assert_called_once()
        self.nano.command.assert_called_once_with("BUFFER_END", timeout=2.0)

    def test_cleanup_failure_preserves_primary_exception(self):
        primary = TimeoutError("scope acquisition")
        self.nano.command.side_effect = RuntimeError("serial disconnected")
        with patch("record_integrated_matrix32.acquire", side_effect=primary):
            with self.assertRaises(TimeoutError) as raised:
                acquire_buffered_record(self.nano, None, self.args, self.record, self.child)
        self.assertIs(raised.exception, primary)
        self.assertIn("serial disconnected", self.record["nano_frame_buffer"]["cleanup_end_error"])

    def test_dump_failure_does_not_repeat_acknowledged_buffer_end(self):
        def fail_dump(begun):
            begun["end_ack"] = "ACK BUFFER_END 4 COUNT 201 LOST 0 CAP 256"
            raise RuntimeError("corrupt dump")
        self.nano.buffered_capture_end_and_dump.side_effect = fail_dump
        with patch("record_integrated_matrix32.acquire", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "corrupt dump"):
                acquire_buffered_record(self.nano, None, self.args, self.record, self.child)
        self.nano.command.assert_not_called()

    def test_unexpected_capacity_rejected_before_scope_arm(self):
        self.nano.buffered_capture_begin.return_value["capacity"] = 64
        with patch("record_integrated_matrix32.acquire") as acquire_mock:
            with self.assertRaisesRegex(RuntimeError, "capacity mismatch"):
                acquire_buffered_record(self.nano, None, self.args, self.record, self.child)
        acquire_mock.assert_not_called()
        self.nano.command.assert_called_once_with("BUFFER_END", timeout=2.0)

    def test_delayed_begin_cannot_leave_insufficient_buffer_time(self):
        with (
            patch("record_integrated_matrix32.time.monotonic_ns", side_effect=[0, 5_000_000_000]),
            patch("record_integrated_matrix32.acquire") as acquire_mock,
        ):
            with self.assertRaisesRegex(TimeoutError, "Insufficient Nano buffer"):
                acquire_buffered_record(self.nano, None, self.args, self.record, self.child)
        acquire_mock.assert_not_called()
        self.nano.command.assert_called_once_with("BUFFER_END", timeout=2.0)

    def test_controller_fault_during_acquisition_triggers_buffer_cleanup(self):
        def fault(scope, timeout, on_progress):
            self.child.start_failure.is_set.return_value = True
            on_progress({"elapsed_s": 0.1})
        with patch("record_integrated_matrix32.acquire", side_effect=fault):
            with self.assertRaisesRegex(RuntimeError, "Controller reported fault"):
                acquire_buffered_record(self.nano, None, self.args, self.record, self.child)
        self.nano.command.assert_called_once_with("BUFFER_END", timeout=2.0)
        self.assertEqual(self.record["acquisition_progress"]["elapsed_s"], 0.1)

    def test_ambiguous_start_timeout_attempts_stop_before_serial_close(self):
        history = []
        class Nano:
            def __init__(self, *args):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                history.append("CLOSE")
            def synchronize(self):
                return "ACK PING 1"
            def command(self, op, **kwargs):
                history.append(op)
                if op == "INFO":
                    return INFO
                if op == "START":
                    raise TimeoutError("START ACK lost")
                return "ACK STOP 4"
        with tempfile.TemporaryDirectory() as folder:
            args = parser().parse_args(["--output-root", folder])
            requested = plan(args, parser())
            with (
                patch("record_integrated_matrix32.NanoLog", Nano),
                patch("record_integrated_matrix32.resolve_port", return_value="nano"),
                patch("record_integrated_matrix32.resolve_scope_device") as scope,
                redirect_stdout(io.StringIO()),
            ):
                with self.assertRaisesRegex(TimeoutError, "START ACK lost"):
                    run(args, requested)
            scope.assert_not_called()
            metadata = json.loads(next(Path(folder).glob("*/metadata.json")).read_text())
        self.assertEqual(history, ["INFO", "START", "STOP", "CLOSE"])
        self.assertEqual(metadata["status"], "failed")
        self.assertEqual(metadata["nano_stop_ack"], "ACK STOP 4")


if __name__ == "__main__":
    unittest.main()
