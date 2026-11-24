"""Deterministic operator-cue tests: no devices, threads or real-time waits."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

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


def observation(*, anchor=None, current=100.0, trigger_state="TRIGGER",
                stopped=False):
    """Shape emitted by record_scope.acquire(), including its earlier RUN."""
    first_trigger = None if anchor is None else {
        "command": "TRIGger:STATE?", "value": "TRIGGER",
        "query_start_monotonic_s": anchor - 0.005,
        "query_end_monotonic_s": anchor,
    }
    latest = {
        "command": "TRIGger:STATE?", "value": trigger_state,
        "query_start_monotonic_s": current - 0.005,
        "query_end_monotonic_s": current,
    }
    return {
        "elapsed_s": current - 90.0,
        "run_write_begin_monotonic_s": 90.0,
        "run_write_end_monotonic_s": 90.001,
        "first_trigger_observation": first_trigger,
        "last_trigger_observation": first_trigger,
        "trigger_state_observations": [latest],
        "first_stopped_observation": {
            "command": "ACQuire:STATE?", "value": "0",
            "query_start_monotonic_s": current - 0.002,
            "query_end_monotonic_s": current,
        } if stopped else None,
    }


class OperatorPromptConfigurationTests(unittest.TestCase):
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
    def make_prompts(self, *, record_index=1, record_count=1):
        clock = FakeClock()
        lines = []
        prompts = OperatorPrompts(
            prompt_schedule(), duration_s=20.0,
            record_index=record_index, record_count=record_count,
            clock=clock, emit=lines.append,
        )
        return prompts, clock, lines

    def test_no_cues_while_armed_and_waiting_for_the_trigger(self):
        prompts, clock, lines = self.make_prompts()
        for now, state in ((100.0, "ARMED"), (105.0, "READY")):
            clock.now = now
            prompts.observe(observation(current=now, trigger_state=state))
        self.assertEqual(lines, [])
        self.assertEqual(prompts.metadata["status"], "waiting_for_trigger")
        self.assertIsNone(prompts.metadata["anchor_monotonic_s"])
        self.assertTrue(all(event["status"] == "pending"
                            for event in prompts.metadata["events"]))

    def test_trigger_query_end_is_anchor_and_actual_clock_records_lateness(self):
        prompts, clock, lines = self.make_prompts(record_index=2, record_count=3)
        clock.now = 101.2
        prompts.observe(observation(anchor=101.0, current=101.1))
        self.assertEqual(lines, ["[PROMPT record 2/3 +0.2s] HOLD"])
        self.assertEqual(prompts.metadata["anchor_monotonic_s"], 101.0)
        self.assertEqual(prompts.metadata["time_reference"],
                         "host_observed_trigger_query_end")
        self.assertFalse(prompts.metadata["scope_clock_synchronized"])
        first = prompts.metadata["events"][0]
        self.assertEqual(first["status"], "printed")
        self.assertEqual(first["emitted_monotonic_s"], 101.2)
        self.assertAlmostEqual(first["elapsed_s"], 0.2)
        self.assertAlmostEqual(first["lateness_s"], 0.2)
        self.assertEqual(first["print_completed_monotonic_s"], 101.2)

        clock.now = 103.99
        prompts.observe(observation(anchor=101.0, current=103.99))
        self.assertEqual(len(lines), 1)
        clock.now = 104.01
        prompts.observe(observation(anchor=101.0, current=104.01))
        self.assertEqual(lines[-1], "[PROMPT record 2/3 +3.0s] RELEASE")
        second = prompts.metadata["events"][1]
        self.assertAlmostEqual(second["lateness_s"], 0.01)

    def test_repeated_progress_does_not_repeat_the_current_instruction(self):
        prompts, clock, lines = self.make_prompts()
        for now in (100.0, 100.0, 100.5, 103.0, 103.1, 103.2):
            clock.now = now
            prompts.observe(observation(anchor=100.0, current=now))
        self.assertEqual(lines, ["[PROMPT record 1/1 +0.0s] HOLD",
                                 "[PROMPT record 1/1 +3.0s] RELEASE"])

    def test_late_callback_prints_only_latest_due_action_and_logs_skipped_cues(self):
        prompts, clock, lines = self.make_prompts()
        clock.now = 107.4
        prompts.observe(observation(anchor=100.0, current=100.0))
        self.assertEqual(lines, ["[PROMPT record 1/1 +7.4s] PRESS GENTLY"])
        events = prompts.metadata["events"]
        for event in events[:2]:
            self.assertEqual(event["status"], "skipped")
            self.assertEqual(event["skip_reason"], "superseded_by_later_due_prompt")
        self.assertEqual(events[2]["status"], "printed")
        self.assertAlmostEqual(events[2]["lateness_s"], 1.4)
        self.assertEqual([event["status"] for event in events[3:]],
                         ["pending", "pending"])

    def test_observed_completion_cancels_before_dispatching_a_due_action(self):
        for stopped, state in ((True, "TRIGGER"), (False, "SAVE"),
                               (False, "READY"), (False, "ARMED")):
            with self.subTest(stopped=stopped, state=state):
                prompts, clock, lines = self.make_prompts()
                prompts.observe(observation(anchor=100.0, current=100.0))
                clock.now = 103.2
                prompts.observe(observation(anchor=100.0, current=103.2,
                                            trigger_state=state, stopped=stopped))
                self.assertEqual(lines, ["[PROMPT record 1/1 +0.0s] HOLD"])
                self.assertEqual(prompts.metadata["status"], "finished")
                self.assertTrue(all(event["status"] == "skipped"
                                    for event in prompts.metadata["events"][1:]))
                clock.now = 110.0
                prompts.observe(observation(anchor=100.0, current=110.0))
                self.assertEqual(len(lines), 1)

    def test_first_callback_already_finished_does_not_start_the_schedule(self):
        for stopped, state in ((True, "TRIGGER"), (False, "SAVE")):
            with self.subTest(stopped=stopped, state=state):
                prompts, clock, lines = self.make_prompts()
                clock.now = 105.0
                prompts.observe(observation(anchor=100.0, current=105.0,
                                            trigger_state=state, stopped=stopped))
                self.assertEqual(lines, [])
                self.assertEqual(prompts.metadata["status"], "finished")
                self.assertIsNone(prompts.metadata["anchor_monotonic_s"])

    def test_invalid_scope_state_does_not_emit_a_cue_before_acquire_raises(self):
        for invalid_field in ("trigger", "acquisition"):
            with self.subTest(invalid_field=invalid_field):
                prompts, clock, lines = self.make_prompts()
                prompts.observe(observation(anchor=100.0, current=100.0))
                clock.now = 103.2
                failed = observation(anchor=100.0, current=103.2)
                if invalid_field == "trigger":
                    failed["trigger_state_observations"][-1]["value"] = "AUTO"
                else:
                    failed["acquisition_state_observations"] = [{"value": "INVALID"}]
                prompts.observe(failed)
                self.assertEqual(lines, ["[PROMPT record 1/1 +0.0s] HOLD"])
                self.assertEqual(prompts.metadata["status"], "finished")
                self.assertEqual(prompts.metadata["events"][1]["status"], "skipped")

    def test_failure_finish_skips_future_cues_without_printing_and_is_idempotent(self):
        prompts, clock, lines = self.make_prompts()
        prompts.observe(observation(anchor=100.0, current=100.0))
        clock.now = 101.0
        prompts.finish("acquisition_failed")
        self.assertEqual(prompts.metadata["finish_reason"], "acquisition_failed")
        self.assertEqual(prompts.metadata["events"][0]["status"], "printed")
        for event in prompts.metadata["events"][1:]:
            self.assertEqual(event["status"], "skipped")
            self.assertEqual(event["skip_reason"], "acquisition_failed")
        before = copy.deepcopy(prompts.metadata)
        prompts.finish("outer_cleanup")
        clock.now = 110.0
        prompts.observe(observation(anchor=100.0, current=110.0))
        self.assertEqual(prompts.metadata, before)
        self.assertEqual(lines, ["[PROMPT record 1/1 +0.0s] HOLD"])

    def test_no_stale_instruction_is_printed_after_nominal_window(self):
        prompts, clock, lines = self.make_prompts()
        prompts.observe(observation(anchor=100.0, current=100.0))
        clock.now = 120.0
        prompts.observe(observation(anchor=100.0, current=120.0))
        self.assertEqual(lines, ["[PROMPT record 1/1 +0.0s] HOLD"])
        self.assertEqual(prompts.metadata["finish_reason"], "nominal_window_elapsed")
        self.assertTrue(all(event["status"] == "skipped"
                            for event in prompts.metadata["events"][1:]))

    def test_each_record_has_its_own_epoch_without_mutating_shared_schedule(self):
        schedule = prompt_schedule()
        original = copy.deepcopy(schedule)
        clock = FakeClock()
        lines = []
        first = OperatorPrompts(schedule, duration_s=20.0, record_index=1,
                                record_count=2, clock=clock, emit=lines.append)
        first.observe(observation(anchor=100.0, current=100.0))
        first.finish("acquisition_completed")
        clock.now = 250.0
        second = OperatorPrompts(schedule, duration_s=20.0, record_index=2,
                                 record_count=2, clock=clock, emit=lines.append)
        second.observe(observation(anchor=250.0, current=250.0))
        self.assertEqual(lines, ["[PROMPT record 1/2 +0.0s] HOLD",
                                 "[PROMPT record 2/2 +0.0s] HOLD"])
        self.assertEqual(first.metadata["anchor_monotonic_s"], 100.0)
        self.assertEqual(second.metadata["anchor_monotonic_s"], 250.0)
        self.assertEqual(second.metadata["events"][1]["status"], "pending")
        self.assertEqual(schedule, original)

    def test_print_failure_propagates_and_is_not_recorded_as_success(self):
        def broken_terminal(line):
            raise BrokenPipeError("terminal closed")

        clock = FakeClock()
        prompts = OperatorPrompts(prompt_schedule(), duration_s=20.0,
                                  clock=clock, emit=broken_terminal)
        with self.assertRaisesRegex(BrokenPipeError, "terminal closed"):
            prompts.observe(observation(anchor=100.0, current=100.0))
        self.assertEqual(prompts.metadata["events"][0]["status"], "print_failed")
        prompts.finish("acquisition_failed")
        self.assertEqual(prompts.metadata["events"][0]["status"], "print_failed")
        self.assertTrue(all(event["status"] == "skipped"
                            for event in prompts.metadata["events"][1:]))


if __name__ == "__main__":
    unittest.main()
