"""Operator-cue regressions with fake clocks and bounded hardware-free workers."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from oscilloscope.operator_prompts import (
    OperatorPrompts, load_operator_prompts, normalize_operator_prompts,
)


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


def prompt_schedule():
    return normalize_operator_prompts({
        0: "HOLD",
        3: "RELEASE",
        6: "PRESS GENTLY",
        10: "RELEASE AGAIN",
        13: "PRESS FIRMLY",
    }, 20.0)


def run_written(anchor=100.0):
    return {"run_write_begin_monotonic_s": anchor - 0.001,
            "run_write_end_monotonic_s": anchor}


def observation(*, current=100.0, trigger_state="READY", acquisition_state="1"):
    """Saved scope shape: a successful capture need never report TRIGGER."""
    latest = {
        "command": "TRIGger:STATE?", "value": trigger_state,
        "query_start_monotonic_s": current - 0.005,
        "query_end_monotonic_s": current,
    }
    acq = {"command": "ACQuire:STATE?", "value": acquisition_state,
           "query_start_monotonic_s": current - 0.002,
           "query_end_monotonic_s": current}
    return {
        **run_written(),
        "elapsed_s": current - 100.0,
        "first_trigger_observation": None,
        "last_trigger_observation": None,
        "trigger_state_observations": [latest],
        "acquisition_state_observations": [acq],
        "first_stopped_observation": acq if acquisition_state == "0" else None,
    }


class OperatorPromptConfigurationTests(unittest.TestCase):
    def test_thread_start_failure_can_be_cleaned_up_without_masking_the_error(self):
        prompts = OperatorPrompts(prompt_schedule(), duration_s=20)
        with patch("threading.Thread.start", side_effect=RuntimeError("thread start failed")):
            with self.assertRaisesRegex(RuntimeError, "thread start failed"):
                prompts.start({"run_write_end_monotonic_s": time.monotonic()})
        prompts.finish("acquisition_failed")
        self.assertTrue(prompts.join())
        self.assertFalse(prompts.snapshot()["delivery_complete"])

    def test_python_dictionary_and_json_dictionary_have_same_sorted_schedule(self):
        mapping = {13: "PRESS FIRMLY", 0: "HOLD", 3.5: "RELEASE"}
        expected = [
            {"at_s": 0.0, "text": "HOLD"},
            {"at_s": 3.5, "text": "RELEASE"},
            {"at_s": 13.0, "text": "PRESS FIRMLY"},
        ]
        self.assertEqual(normalize_operator_prompts(mapping, 20.0), expected)
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "schedule.json"
            file.write_text(json.dumps(mapping), encoding="utf-8")
            self.assertEqual(load_operator_prompts(file, 20.0), expected)

    def test_times_must_fit_inside_the_actual_record_window(self):
        for key in ("-1", "20", "21", "nan", "inf", "not-a-time"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                normalize_operator_prompts({key: "RELEASE"}, 20.0)
        self.assertEqual(normalize_operator_prompts({"19.9": "RELEASE"}, 20.0),
                         [{"at_s": 19.9, "text": "RELEASE"}])

    def test_messages_cannot_be_blank_multiline_or_terminal_controls(self):
        for text in ("", "   ", "HOLD\nRELEASE", "HOLD\rRELEASE",
                     "\x1b[2JHOLD", None, 123):
            with self.subTest(text=text), self.assertRaises(ValueError):
                normalize_operator_prompts({0: text}, 20.0)

    def test_numeric_alias_times_cannot_command_two_actions_at_once(self):
        with self.assertRaises(ValueError):
            normalize_operator_prompts({"3": "HOLD", "3.0": "RELEASE"}, 20.0)

    def test_loader_rejects_duplicate_keys_and_invalid_dictionary_files(self):
        payloads = [
            '{"3": "HOLD", "3": "RELEASE"}',
            '{"3": "HOLD", "3.0": "RELEASE"}',
            '{}',
            '[]',
            'null',
            '{not valid JSON}',
        ]
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "schedule.json"
            for payload in payloads:
                with self.subTest(payload=payload):
                    file.write_text(payload, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_operator_prompts(file, 20.0)


class OperatorPromptTimingTests(unittest.TestCase):
    def make_prompts(self, *, record_index=1, record_count=1, display=None):
        clock = FakeClock()
        lines = []
        prompts = OperatorPrompts(
            prompt_schedule(), duration_s=20.0,
            record_index=record_index, record_count=record_count,
            clock=clock, emit=lines.append, display=display,
        )
        return prompts, clock, lines

    def test_no_cues_before_the_run_write_callback(self):
        prompts, clock, lines = self.make_prompts()
        for now, state in ((100.0, "ARMED"), (105.0, "READY")):
            clock.now = now
            prompts.observe(observation(current=now, trigger_state=state))
            prompts.tick()
        self.assertEqual(lines, [])
        evidence = prompts.snapshot()
        self.assertEqual(evidence["status"], "waiting_for_arm")
        self.assertIsNone(evidence["anchor_monotonic_s"])
        self.assertTrue(all(event["status"] == "pending" for event in evidence["events"]))

    def test_run_write_completion_is_anchor_and_actual_clock_records_lateness(self):
        prompts, clock, lines = self.make_prompts(record_index=2, record_count=3)
        clock.now = 101.2
        prompts.start(run_written(101.0), background=False)
        self.assertEqual(lines, ["[PROMPT record 2/3 +0.2s] HOLD"])
        evidence = prompts.snapshot()
        self.assertEqual(evidence["anchor_monotonic_s"], 101.0)
        self.assertEqual(evidence["time_reference"], "host_scope_run_write_end")
        self.assertFalse(evidence["scope_clock_synchronized"])
        first = evidence["events"][0]
        self.assertEqual(first["status"], "printed")
        self.assertEqual(first["emitted_monotonic_s"], 101.2)
        self.assertAlmostEqual(first["elapsed_s"], 0.2)
        self.assertAlmostEqual(first["lateness_s"], 0.2)
        self.assertEqual(first["print_completed_monotonic_s"], 101.2)
        clock.now = 103.99
        prompts.tick()
        self.assertEqual(len(lines), 1)
        clock.now = 104.01
        prompts.tick()
        self.assertEqual(lines[-1], "[PROMPT record 2/3 +3.0s] RELEASE")
        self.assertAlmostEqual(prompts.snapshot()["events"][1]["lateness_s"], 0.01)

    def test_real_baseline_state_pattern_delivers_all_cues_without_trigger(self):
        # Actual query-completion offsets and states selected from the supplied
        # 20261124T080841086567Z baseline, including the first poll after each cue.
        # Its complete trace contains zero TRIGGER observations and 200 Gray frames.
        recorded_polls = [
            (0.0791045980004128, "SAVE", "1"),
            (0.18205958101316355, "SAVE", "1"),
            (0.2909223680035211, "SAVE", "1"),
            (0.4055131410132162, "ARMED", "1"),
            (0.5076719210046576, "ARMED", "1"),
            (0.6100763720023679, "ARMED", "1"),
            (0.712783663009759, "READY", "1"),
            (3.038567139999941, "READY", "1"),
            (6.076273374012089, "READY", "1"),
            (10.00823314700392, "READY", "1"),
            (13.028115862005507, "READY", "1"),
            (20.001537924006698, "READY", "1"),
            (20.207283405005, "READY", "1"),
            (20.310459216008894, "SAVE", "0"),
        ]
        prompts, clock, lines = self.make_prompts()
        prompts.start(run_written(), background=False)
        for elapsed, trigger, acquisition in recorded_polls:
            clock.now = 100.0 + elapsed
            prompts.observe(observation(current=clock.now, trigger_state=trigger,
                                        acquisition_state=acquisition))
            prompts.tick()
        evidence = prompts.snapshot()
        self.assertEqual([line.split("] ", 1)[1] for line in lines],
                         ["HOLD", "RELEASE", "PRESS GENTLY", "RELEASE AGAIN", "PRESS FIRMLY"])
        self.assertEqual(evidence["printed_count"], 5)
        self.assertEqual(evidence["skipped_count"], 0)
        self.assertTrue(evidence["delivery_complete"])
        self.assertTrue(prompts.finished)

    def test_repeated_ticks_do_not_repeat_the_current_instruction(self):
        prompts, clock, lines = self.make_prompts()
        prompts.start(run_written(), background=False)
        for now in (100.0, 100.0, 100.5, 103.0, 103.1, 103.2):
            clock.now = now
            prompts.tick()
        self.assertEqual(lines, ["[PROMPT record 1/1 +0.0s] HOLD",
                                 "[PROMPT record 1/1 +3.0s] RELEASE"])

    def test_late_tick_prints_only_latest_due_action_and_logs_skips(self):
        prompts, clock, lines = self.make_prompts()
        clock.now = 107.4
        prompts.start(run_written(), background=False)
        self.assertEqual(lines, ["[PROMPT record 1/1 +7.4s] PRESS GENTLY"])
        events = prompts.snapshot()["events"]
        for event in events[:2]:
            self.assertEqual(event["status"], "skipped")
            self.assertEqual(event["skip_reason"], "superseded_by_later_due_prompt")
        self.assertEqual(events[2]["status"], "printed")
        self.assertAlmostEqual(events[2]["lateness_s"], 1.4)
        self.assertEqual([event["status"] for event in events[3:]], ["pending", "pending"])

    def test_explicit_scope_stop_cancels_before_a_due_action(self):
        prompts, clock, lines = self.make_prompts()
        prompts.start(run_written(), background=False)
        clock.now = 103.2
        prompts.observe(observation(current=103.2, acquisition_state="0"))
        prompts.tick()
        self.assertEqual(lines, ["[PROMPT record 1/1 +0.0s] HOLD"])
        self.assertTrue(prompts.finished)
        self.assertTrue(all(event["status"] == "skipped"
                            for event in prompts.snapshot()["events"][1:]))
        clock.now = 110.0
        prompts.tick()
        self.assertEqual(len(lines), 1)

    def test_invalid_scope_state_cancels_before_another_instruction(self):
        for invalid_field in ("trigger", "acquisition"):
            with self.subTest(invalid_field=invalid_field):
                prompts, clock, lines = self.make_prompts()
                prompts.start(run_written(), background=False)
                clock.now = 103.2
                failed = observation(current=103.2)
                if invalid_field == "trigger":
                    failed["trigger_state_observations"][-1]["value"] = "AUTO"
                else:
                    failed["acquisition_state_observations"][-1]["value"] = "INVALID"
                prompts.observe(failed)
                prompts.tick()
                self.assertEqual(lines, ["[PROMPT record 1/1 +0.0s] HOLD"])
                self.assertTrue(prompts.finished)
                self.assertEqual(prompts.snapshot()["events"][1]["status"], "skipped")

    def test_failure_finish_skips_future_cues_without_printing_and_is_idempotent(self):
        prompts, clock, lines = self.make_prompts()
        prompts.start(run_written(), background=False)
        clock.now = 101.0
        prompts.finish("acquisition_failed")
        evidence = prompts.snapshot()
        self.assertEqual(evidence["finish_reason"], "acquisition_failed")
        self.assertEqual(evidence["events"][0]["status"], "printed")
        for event in evidence["events"][1:]:
            self.assertEqual(event["status"], "skipped")
            self.assertEqual(event["skip_reason"], "acquisition_failed")
        before = copy.deepcopy(evidence)
        prompts.finish("outer_cleanup")
        clock.now = 110.0
        prompts.tick()
        self.assertEqual(prompts.snapshot(), before)
        self.assertEqual(lines, ["[PROMPT record 1/1 +0.0s] HOLD"])

    def test_no_stale_instruction_is_printed_after_nominal_window(self):
        prompts, clock, lines = self.make_prompts()
        prompts.start(run_written(), background=False)
        clock.now = 120.0
        prompts.tick()
        self.assertEqual(lines, ["[PROMPT record 1/1 +0.0s] HOLD"])
        self.assertEqual(prompts.snapshot()["finish_reason"], "nominal_window_elapsed")
        self.assertTrue(all(event["status"] == "skipped"
                            for event in prompts.snapshot()["events"][1:]))

    def test_zero_and_partial_delivery_are_distinct_from_success(self):
        zero, _, zero_lines = self.make_prompts()
        zero.finish("acquisition_failed")
        self.assertEqual(zero_lines, [])
        evidence = zero.snapshot()
        self.assertFalse(evidence["delivery_complete"])
        self.assertEqual((evidence["printed_count"], evidence["skipped_count"],
                          evidence["cue_count"]), (0, 5, 5))

        partial, _, _ = self.make_prompts()
        partial.start(run_written(), background=False)
        partial.finish("acquisition_failed")
        evidence = partial.snapshot()
        self.assertFalse(evidence["delivery_complete"])
        self.assertEqual((evidence["printed_count"], evidence["skipped_count"]), (1, 4))

    def test_each_record_has_its_own_epoch_without_mutating_shared_schedule(self):
        schedule = prompt_schedule()
        original = copy.deepcopy(schedule)
        clock = FakeClock()
        lines = []
        first = OperatorPrompts(schedule, duration_s=20.0, record_index=1,
                                record_count=2, clock=clock, emit=lines.append)
        first.start(run_written(), background=False)
        first.finish("acquisition_completed")
        clock.now = 250.0
        second = OperatorPrompts(schedule, duration_s=20.0, record_index=2,
                                 record_count=2, clock=clock, emit=lines.append)
        second.start(run_written(250.0), background=False)
        self.assertEqual(lines, ["[PROMPT record 1/2 +0.0s] HOLD",
                                 "[PROMPT record 2/2 +0.0s] HOLD"])
        self.assertEqual(first.snapshot()["anchor_monotonic_s"], 100.0)
        self.assertEqual(second.snapshot()["anchor_monotonic_s"], 250.0)
        self.assertEqual(second.snapshot()["events"][1]["status"], "pending")
        self.assertEqual(schedule, original)

    def test_start_does_not_silently_reset_an_existing_epoch(self):
        prompts, clock, lines = self.make_prompts()
        prompts.start(run_written(), background=False)
        clock.now = 110.0
        with self.assertRaises(RuntimeError):
            prompts.start(run_written(110.0), background=False)
        self.assertEqual(prompts.snapshot()["anchor_monotonic_s"], 100.0)
        self.assertEqual(len(lines), 1)

    def test_display_receives_current_instruction_and_remaining_times(self):
        frames = []
        prompts, clock, lines = self.make_prompts(display=frames.append)
        prompts.start(run_written(), background=False)
        clock.now = 101.25
        prompts.tick()
        frame = frames[-1]
        self.assertEqual(frame["text"], "HOLD")
        self.assertEqual(frame["cue_index"], 0)
        self.assertEqual(frame["next_text"], "RELEASE")
        self.assertAlmostEqual(frame["next_in_s"], 1.75)
        self.assertAlmostEqual(frame["remaining_s"], 18.75)
        self.assertEqual(frame["record_index"], 1)
        self.assertEqual(frame["record_count"], 1)
        self.assertEqual(frame["duration_s"], 20.0)
        self.assertEqual(len(lines), 1)

    def test_snapshot_is_a_copy_not_live_mutable_evidence(self):
        prompts, _, _ = self.make_prompts()
        prompts.start(run_written(), background=False)
        evidence = prompts.snapshot()
        evidence["events"][0]["text"] = "MODIFIED"
        evidence["anchor_observation"]["run_write_end_monotonic_s"] = -1
        self.assertEqual(prompts.snapshot()["events"][0]["text"], "HOLD")
        self.assertEqual(prompts.snapshot()["anchor_monotonic_s"], 100.0)


class OperatorPromptWorkerTests(unittest.TestCase):
    def test_timer_delivers_all_cues_while_main_thread_waits_on_a_query(self):
        lines = []
        last_cue = threading.Event()
        def emit(line):
            lines.append(line)
            if line.endswith("PRESS"):
                last_cue.set()
        prompts = OperatorPrompts(
            normalize_operator_prompts({0: "HOLD", 0.03: "RELEASE", 0.06: "PRESS"}, 0.25),
            duration_s=0.25, emit=emit,
        )
        try:
            prompts.start(run_written(time.monotonic()))
            # No acquisition progress callbacks occur while this simulated
            # scope query waits. A polling-dependent scheduler cannot pass.
            self.assertTrue(last_cue.wait(0.6), "Cue timer stalled with the scope query")
            # Let the completed write and nominal-window exit settle without
            # racing finish() against the last emit callback's return.
            self.assertTrue(prompts.join(timeout=0.5))
        finally:
            prompts.finish("acquisition_completed")
            self.assertTrue(prompts.join(timeout=0.5))
        self.assertEqual([line.split("] ", 1)[1] for line in lines],
                         ["HOLD", "RELEASE", "PRESS"])
        self.assertTrue(prompts.snapshot()["delivery_complete"])

    def test_failure_cancels_worker_before_future_cue(self):
        first_cue = threading.Event()
        lines = []
        def emit(line):
            lines.append(line)
            first_cue.set()
        prompts = OperatorPrompts(
            normalize_operator_prompts({0: "HOLD", 0.3: "RELEASE"}, 0.5),
            duration_s=0.5, emit=emit,
        )
        try:
            prompts.start(run_written(time.monotonic()))
            self.assertTrue(first_cue.wait(0.3))
            prompts.finish("controller_fault")
            self.assertTrue(prompts.join(timeout=0.3))
        finally:
            prompts.finish("test_cleanup")
            prompts.join(timeout=0.5)
        self.assertEqual(len(lines), 1)
        self.assertEqual(prompts.snapshot()["events"][1]["status"], "skipped")
        self.assertFalse(prompts.snapshot()["delivery_complete"])

    def test_finish_does_not_wait_for_a_blocked_terminal_write(self):
        write_entered = threading.Event()
        release_write = threading.Event()
        finish_returned = threading.Event()
        def blocked_emit(line):
            write_entered.set()
            release_write.wait(0.8)
        prompts = OperatorPrompts(
            normalize_operator_prompts({0: "HOLD", 0.4: "RELEASE"}, 0.6),
            duration_s=0.6, emit=blocked_emit,
        )
        finisher = None
        try:
            prompts.start(run_written(time.monotonic()))
            self.assertTrue(write_entered.wait(0.3))
            def finish():
                prompts.finish("scope_stopped")
                finish_returned.set()
            finisher = threading.Thread(target=finish, daemon=True)
            finisher.start()
            self.assertTrue(finish_returned.wait(0.2),
                            "Nano cleanup would wait for terminal output")
            self.assertFalse(release_write.is_set())
            self.assertFalse(prompts.join(timeout=0.01))
        finally:
            release_write.set()
            if finisher is not None:
                finisher.join(timeout=0.5)
            prompts.finish("test_cleanup")
            self.assertTrue(prompts.join(timeout=0.5))
        evidence = prompts.snapshot()
        self.assertTrue(evidence["events"][0]["completed_after_finish"])
        self.assertEqual(evidence["events"][1]["status"], "skipped")
        self.assertFalse(evidence["delivery_complete"])

    def test_background_print_failure_is_recorded_and_propagated_by_check(self):
        attempted = threading.Event()
        def broken_terminal(line):
            attempted.set()
            raise BrokenPipeError("terminal closed")
        prompts = OperatorPrompts(
            normalize_operator_prompts({0: "HOLD", 0.1: "RELEASE"}, 0.2),
            duration_s=0.2, emit=broken_terminal,
        )
        try:
            prompts.start(run_written(time.monotonic()))
            self.assertTrue(attempted.wait(0.3))
            self.assertTrue(prompts.join(timeout=0.5))
            with self.assertRaisesRegex(RuntimeError, "terminal closed"):
                prompts.check()
            evidence = prompts.snapshot()
            self.assertEqual(evidence["events"][0]["status"], "print_failed")
            self.assertEqual(evidence["events"][1]["status"], "skipped")
            self.assertFalse(evidence["delivery_complete"])
        finally:
            prompts.finish("test_cleanup")
            prompts.join(timeout=0.5)


if __name__ == "__main__":
    unittest.main()
