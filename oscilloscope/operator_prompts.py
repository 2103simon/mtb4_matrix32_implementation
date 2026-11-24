"""Optional human cues on a host timer independent of scope USB queries.

The epoch is completion of the host's scope RUN write, not an observed or
calibrated hardware trigger. This module never sends device commands.
"""
from __future__ import annotations

from collections.abc import Mapping
import copy
import json
import math
from pathlib import Path
import threading
import time


TIME_REFERENCE = "host_scope_run_write_end"


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
    """Independent cue timer for one scope record.

    Signal finish before Nano cleanup; join only afterward. State locks never
    cover terminal I/O, so a slow terminal cannot hold up BUFFER_END.
    """

    def __init__(self, schedule, *, duration_s, record_index=1, record_count=1,
                 clock=None, emit=None, display=None):
        self.clock = clock if clock is not None else time.monotonic
        self.emit = emit if emit is not None else lambda line: print(line, flush=True)
        self.display = display
        self.duration_s = duration_s
        self.record_index = record_index
        self.record_count = record_count
        self.next_index = 0
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread = None
        self._failure = None
        self.metadata = {
            "enabled": True,
            "status": "waiting_for_arm",
            "time_reference": TIME_REFERENCE,
            "scope_clock_synchronized": False,
            "operator_actions_verified": False,
            "nominal_window_s": duration_s,
            "anchor_monotonic_s": None,
            "anchor_observation": None,
            "worker_joined": None,
            "events": [{**item, "status": "pending"} for item in schedule],
        }

    @property
    def finished(self):
        return self._stop.is_set()

    def start(self, observation, *, background=True):
        """Called immediately after RUN is written, before any scope query."""
        anchor = float(observation["run_write_end_monotonic_s"])
        if not math.isfinite(anchor):
            raise ValueError("Prompt RUN-write timestamp must be finite")
        with self._lock:
            if self.metadata["status"] != "waiting_for_arm":
                raise RuntimeError("Operator prompt timeline already started or finished")
            self.metadata.update(
                status="running", anchor_monotonic_s=anchor,
                anchor_observation=dict(observation),
                background_timer=bool(background),
            )
        if background:
            self._thread = threading.Thread(
                target=self._worker, name=f"operator-prompts-{self.record_index}",
                daemon=True,
            )
            self._thread.start()
        else:
            self.tick()

    def finish(self, reason):
        """Cancel pending cues without printing or waiting for a worker."""
        self._stop.set()
        with self._lock:
            if self.metadata["status"] == "finished":
                return
            self.metadata.update(status="finished", finish_reason=reason,
                                 finished_monotonic_s=self.clock())
            for event in self.metadata["events"]:
                if event["status"] == "pending":
                    event.update(status="skipped", skip_reason=reason)

    def join(self, timeout=0.5):
        """Finalize the timer after Nano cleanup, with a bounded wait."""
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join(timeout=timeout)
        joined = self._thread is None or not self._thread.is_alive()
        with self._lock:
            self.metadata["worker_joined"] = joined
        return joined

    def check(self):
        """Surface a timer/output error through the recorder's cleanup path."""
        if self._failure is not None:
            raise RuntimeError(f"Operator prompt output failed: {self._failure}") from self._failure

    def snapshot(self):
        with self._lock:
            result = copy.deepcopy(self.metadata)
        events = result["events"]
        result["cue_count"] = len(events)
        result["printed_count"] = sum(e["status"] == "printed" for e in events)
        result["skipped_count"] = sum(e["status"] == "skipped" for e in events)
        result["delivery_complete"] = bool(events) and all(
            e["status"] == "printed" and not e.get("completed_after_finish")
            for e in events
        ) and not result.get("error") and result["worker_joined"] is not False
        return result

    def observe(self, observation):
        """Use explicit acquisition stop/fault evidence only, never a TRIGGER gate."""
        if self.finished:
            return
        if observation.get("first_stopped_observation") is not None:
            self.finish("scope_stopped")
            return
        states = observation.get("acquisition_state_observations", [])
        if states and states[-1]["value"] not in ("1", "RUN", "ON"):
            self.finish("scope_not_acquiring")
            return
        triggers = observation.get("trigger_state_observations", [])
        if triggers and triggers[-1]["value"] not in ("TRIGGER", "SAVE", "READY", "ARMED"):
            self.finish("invalid_trigger_state")
        # Real completed captures can report SAVE/ARMED/READY throughout.
        # Their presence or absence does not start, pause or end human cues.

    def _worker(self):
        try:
            while not self.finished:
                self.tick()
                if self.finished:
                    break
                with self._lock:
                    events = self.metadata["events"]
                    next_s = (events[self.next_index]["at_s"]
                              if self.next_index < len(events) else self.duration_s)
                    target = self.metadata["anchor_monotonic_s"] + next_s
                self._stop.wait(max(0.001, min(0.1, target - self.clock())))
        except BaseException as exc:
            with self._lock:
                self._failure = exc
                self.metadata["error"] = f"{type(exc).__name__}: {exc}"
            self.finish("prompt_output_failed")

    def tick(self):
        """Advance from the host clock; public for deterministic offline replay."""
        if self.finished:
            return
        with self._lock:
            anchor = self.metadata["anchor_monotonic_s"]
            if anchor is None:
                return
            now = self.clock()
            elapsed_s = now - anchor
            if elapsed_s >= self.duration_s:
                self.finish("nominal_window_elapsed")
                return
            events = self.metadata["events"]
            due_end = self.next_index
            while due_end < len(events) and events[due_end]["at_s"] <= elapsed_s:
                due_end += 1
            event = None
            if due_end > self.next_index:
                for old in events[self.next_index:due_end - 1]:
                    old.update(status="skipped",
                               skip_reason="superseded_by_later_due_prompt",
                               observed_monotonic_s=now)
                event = events[due_end - 1]
                event.update(status="printing", emitted_monotonic_s=now,
                             elapsed_s=elapsed_s, lateness_s=elapsed_s - event["at_s"])
                self.next_index = due_end
        if event is not None:
            if self.finished:
                with self._lock:
                    event.update(status="skipped",
                                 skip_reason=self.metadata.get("finish_reason", "cancelled"))
                return
            try:
                self.emit(
                    f"[PROMPT record {self.record_index}/{self.record_count} "
                    f"+{elapsed_s:.1f}s] {event['text']}"
                )
                if self.display is not None and not self.finished:
                    self.display(self._frame())
            except BaseException:
                with self._lock:
                    event["status"] = "print_failed"
                raise
            with self._lock:
                event.update(status="printed", print_completed_monotonic_s=self.clock(),
                             completed_after_finish=self.finished)
        elif self.display is not None and not self.finished:
            self.display(self._frame())

    def _frame(self):
        with self._lock:
            elapsed = max(0.0, self.clock() - self.metadata["anchor_monotonic_s"])
            events = self.metadata["events"]
            current = self.next_index - 1 if self.next_index else None
            upcoming = events[self.next_index] if self.next_index < len(events) else None
            return {
                "record_index": self.record_index, "record_count": self.record_count,
                "elapsed_s": elapsed, "duration_s": self.duration_s,
                "text": (events[current]["text"] if current is not None
                         else "WAIT for the first instruction."),
                "cue_index": current,
                "next_in_s": (max(0.0, upcoming["at_s"] - elapsed)
                              if upcoming is not None else None),
                "next_text": upcoming["text"] if upcoming is not None else None,
                "remaining_s": max(0.0, self.duration_s - elapsed),
            }
