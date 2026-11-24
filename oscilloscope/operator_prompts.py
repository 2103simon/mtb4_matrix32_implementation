"""Optional human instructions on the existing scope acquisition polling loop.

No device commands, sleeps or background threads. Times are relative to the
host's first observed TRIGGER query completion, not a calibrated scope edge.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
import math
from pathlib import Path
import time


TIME_REFERENCE = "host_observed_trigger_query_end"


def normalize_operator_prompts(schedule, duration_s):
    """Validate a Python/JSON seconds-to-message dictionary before device access."""
    if not math.isfinite(duration_s) or duration_s <= 0:
        raise ValueError("Prompt duration must be finite and positive")
    if not isinstance(schedule, Mapping) or not schedule:
        raise ValueError("Operator prompts must be a nonempty seconds-to-text dictionary")
    normalized = []
    seen = set()
    for seconds, text in schedule.items():
        try:
            at_s = float(seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Invalid prompt time: {seconds!r}") from exc
        if isinstance(seconds, bool) or not math.isfinite(at_s) or not 0 <= at_s < duration_s:
            raise ValueError(
                f"Prompt time {seconds!r} must be finite and 0 <= time < {duration_s:g}s"
            )
        if at_s in seen:
            raise ValueError(f"Duplicate numeric prompt time: {seconds!r}")
        if not isinstance(text, str) or not text.strip() or not text.isprintable():
            raise ValueError(f"Prompt at {seconds!r} must contain nonempty single-line text")
        seen.add(at_s)
        normalized.append({"at_s": at_s, "text": text.strip()})
    return sorted(normalized, key=lambda item: item["at_s"])


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON prompt time: {key!r}")
        result[key] = value
    return result


def load_operator_prompts(path, duration_s):
    """Load the optional editable JSON dictionary; preserve duplicate-key errors."""
    schedule = json.loads(
        Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object,
    )
    return normalize_operator_prompts(schedule, duration_s)


class OperatorPrompts:
    """One independent cue timeline for one finite scope record.

    observe() is called only after the recorder's controller/deadline checks.
    The existing USB polling can delay a cue; record actual host print times
    and skip obsolete instructions instead of printing contradictory catch-up
    cues. finish() only updates memory, so Nano BUFFER_END need not wait for
    terminal output or a worker thread to join.
    """

    def __init__(self, schedule, *, duration_s, record_index=1, record_count=1,
                 clock=None, emit=None):
        self.clock = clock if clock is not None else time.monotonic
        self.emit = emit if emit is not None else lambda line: print(line, flush=True)
        self.duration_s = duration_s
        self.record_index = record_index
        self.record_count = record_count
        self.next_index = 0
        self.metadata = {
            "enabled": True,
            "status": "waiting_for_trigger",
            "time_reference": TIME_REFERENCE,
            "scope_clock_synchronized": False,
            "operator_actions_verified": False,
            "nominal_window_s": duration_s,
            "anchor_monotonic_s": None,
            "anchor_observation": None,
            "events": [{**item, "status": "pending"} for item in schedule],
        }

    def finish(self, reason):
        """Cancel all pending cues without terminal I/O; safe on every exit path."""
        if self.metadata["status"] == "finished":
            return
        self.metadata.update(status="finished", finish_reason=reason,
                             finished_monotonic_s=self.clock())
        for event in self.metadata["events"][self.next_index:]:
            if event["status"] == "pending":
                event.update(status="skipped", skip_reason=reason)
        self.next_index = len(self.metadata["events"])

    def observe(self, observation):
        if self.metadata["status"] == "finished":
            return
        # Check completion before starting or dispatching: the first snapshot
        # containing TRIGGER may already contain STOP after a slow query.
        if observation.get("first_stopped_observation") is not None:
            self.finish("scope_stopped")
            return
        acquisition_states = observation.get("acquisition_state_observations", [])
        if acquisition_states and acquisition_states[-1]["value"] not in ("1", "RUN", "ON"):
            # acquire() publishes one final snapshot before raising for an
            # invalid state. Never deliver an action from that failed poll.
            self.finish("scope_not_acquiring")
            return
        first_trigger = observation.get("first_trigger_observation")
        trigger_states = observation.get("trigger_state_observations", [])
        if first_trigger is not None and trigger_states:
            if trigger_states[-1]["value"] != "TRIGGER":
                self.finish("scope_left_posttrigger")
                return
        if first_trigger is None:
            return
        if self.metadata["anchor_monotonic_s"] is None:
            self.metadata.update(
                status="running",
                anchor_monotonic_s=first_trigger["query_end_monotonic_s"],
                anchor_observation=dict(first_trigger),
            )
        now = self.clock()
        elapsed_s = now - self.metadata["anchor_monotonic_s"]
        if elapsed_s >= self.duration_s:
            self.finish("nominal_window_elapsed")
            return
        events = self.metadata["events"]
        due_end = self.next_index
        while due_end < len(events) and events[due_end]["at_s"] <= elapsed_s:
            due_end += 1
        if due_end == self.next_index:
            return
        for event in events[self.next_index:due_end - 1]:
            event.update(status="skipped",
                         skip_reason="superseded_by_later_due_prompt",
                         observed_monotonic_s=now)
        event = events[due_end - 1]
        event.update(emitted_monotonic_s=now, elapsed_s=elapsed_s,
                     lateness_s=elapsed_s - event["at_s"])
        try:
            self.emit(
                f"[PROMPT record {self.record_index}/{self.record_count} "
                f"+{elapsed_s:.1f}s] {event['text']}"
            )
        except BaseException:
            event["status"] = "print_failed"
            raise
        event.update(status="printed", print_completed_monotonic_s=self.clock())
        self.next_index = due_end
