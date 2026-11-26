"""Repeated finite Matrix32 + Feather M4 + Nano + Tek scope recording.

Dry run by default. --run opens devices; --arm and --dummy-load-confirmed
are additionally required to send Matrix32 START/SF/SC commands.
Use only with the operator-verified CH1 resistive dummy-load bench, never a person.
Scope CH1 = physical Matrix32 dummy-load voltage (operator wiring),
CH2 = Nano Every D7 Gray reference + rising-edge trigger at 1 V.
Default scope record = 2 s/div, 1M samples (override --points 10000000), 0% pretrigger; distinct acquisitions with gaps.
No stimulation is initiated without an active, fresh MCU sample.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from communication.serial_ports import resolve_port
from communication.rate_replay import load_replay
from record_scope import (
    acquire, atomic_json, configure, scope_settings, validate_csv, verify,
    verify_waveform_timing, verify_waveform_scaling,
)
from scope_device import resolve_scope_device, verify_scope_identity
from tek_mdo_usb import capture, query
from oscilloscope.test_gray_timecode import NanoLog, utc
from oscilloscope.operator_prompts import (
    OperatorPrompts, TIME_REFERENCE as PROMPT_TIME_REFERENCE, load_operator_prompts,
)
from oscilloscope.guided_console import GuidedConsole
from decode_gray_timecode import decode_csv

ROOT = Path(__file__).resolve().parents[1]
NANO_FRAME_INTERVAL_S = 0.1
NANO_BUFFER_CAPACITY = 256
# A frame can arrive immediately after BUFFER_BEGIN. Reserve one frame plus
# one second for trigger/USB scheduling and BUFFER_END; LOST is still checked.
# This polling budget cannot interrupt a blocked USBTMC system call.
NANO_BUFFER_CLEANUP_RESERVE_S = 1.0
NANO_BUFFER_POLL_BUDGET_S = (
    (NANO_BUFFER_CAPACITY - 1) * NANO_FRAME_INTERVAL_S
    - NANO_BUFFER_CLEANUP_RESERVE_S
)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", action="store_true",
                   help="Opt in to real scope/Nano/MCU/BLE connection (Matrix32 still dry-run)")
    p.add_argument("--arm", action="store_true",
                   help="Explicitly permit Matrix32 stimulation on the isolated dummy load")
    p.add_argument("--dummy-load-confirmed", action="store_true",
                   help="Confirm no person/electrodes attached, suitable isolated dummy load and probe")
    p.add_argument("--nano-port", default=None)
    p.add_argument("--scope-device", default="auto")
    p.add_argument("--ch1-v-div", type=float, default=10.0)
    p.add_argument("--points", type=int, default=1000000)
    p.add_argument("--count", type=int, default=1,
                   help="Number of distinct finite CH1+CH2 scope records (1..12); "
                        "one continuous MCU/Matrix32 session, gaps during transfer")
    p.add_argument("--time-div-us", type=float, default=2000000.0)
    p.add_argument("--trigger-position-pct", type=float, default=0.0)
    p.add_argument("--acquire-timeout", type=float, default=90.0,
                   help="Requested acquisition wait; additionally bounded by the "
                        "24.5 s Nano buffer polling budget including BEGIN overhead")
    p.add_argument("--sync-count", type=int, default=30,
                   help="Number of binary CRC32 sync exchanges before AND after recording")
    p.add_argument("--experimental-shared-sf-sc", action="store_true",
                   help="Opt in to unqualified mixed persistent SF/SC ACK transport")
    p.add_argument("--replay-source", type=Path, default=None,
                   help="Replay recorded MCU rate snapshots from an existing integrated run")
    p.add_argument("--replay-start-s", type=float, default=None,
                   help="Override source offset from first MCU arrival; default prior scope start")
    p.add_argument("--stale-ms", type=int, default=250)
    p.add_argument("--capture-before-start", action="store_true",
                   help="Bench-only: arm scope after MCU ACKs even if positive sensor input "
                        "has not yet triggered Matrix32 START; records may contain no output")
    p.add_argument("--controller-ready-timeout", type=float, default=45.0,
                   help="Seconds to wait for Feather config and, with --arm, "
                        "ACK-verified Matrix32 START before scope is armed")
    p.add_argument("--output-root", type=Path, default=Path("data/recordings"))
    p.add_argument("--label", default="integrated_matrix32_scope")
    p.add_argument("--operator-prompts", type=Path, default=None, metavar="JSON",
                   help="Optional JSON seconds-to-text dictionary of terminal cues; "
                        "starts a host timer after each scope RUN write "
                        "and requires 0%% trigger position")
    p.add_argument("--preview-prompts", action="store_true",
                   help="Rehearse one operator-prompt schedule in real time without devices")
    p.add_argument("--verbose", action="store_true",
                   help="With operator prompts, also show routine diagnostics in the terminal")
    return p


def plan(args, p):
    import re
    if args.preview_prompts and (
            args.operator_prompts is None or args.run or args.arm
            or args.dummy_load_confirmed or args.capture_before_start or args.count != 1):
        p.error("--preview-prompts requires --operator-prompts, --count 1, and no --run/arming flags")
    if args.arm and (not args.run or not args.dummy_load_confirmed):
        p.error("--arm requires BOTH --run and --dummy-load-confirmed")
    if args.replay_start_s is not None and args.replay_source is None:
        p.error("--replay-start-s requires --replay-source")
    if args.replay_source is not None and (
            args.count != 1 or args.operator_prompts is not None
            or args.trigger_position_pct != 0
            or (args.run and args.arm and not args.capture_before_start)):
        p.error("Replay requires one record, 0% trigger, no prompts, and --capture-before-start when armed")
    if args.dummy_load_confirmed and not args.arm:
        p.error("--dummy-load-confirmed is only used together with --arm")
    if args.capture_before_start and (not args.run or not args.arm or not args.dummy_load_confirmed):
        p.error("--capture-before-start requires --run --arm --dummy-load-confirmed")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.label):
        p.error("Invalid --label")
    if not 1 <= args.count <= 12:
        p.error("--count must be 1..12")
    if args.points not in (1000, 10000, 100000, 1000000, 5000000, 10000000, 20000000):
        p.error("--points must be a supported scope selection: 1000, 10K, 100K, 1M, 5M, 10M, 20M")
    if not (math.isfinite(args.time_div_us) and 20000 <= args.time_div_us <= 6000000):
        p.error("--time-div-us must be 20000..6000000")
    if not (math.isfinite(args.ch1_v_div) and 0.001 <= args.ch1_v_div <= 100):
        p.error("--ch1-v-div must be 0.001..100")
    if not (math.isfinite(args.trigger_position_pct)
            and 0 <= args.trigger_position_pct <= 100):
        p.error("--trigger-position-pct must be finite and 0..100")
    if not (math.isfinite(args.acquire_timeout)
            and 5 <= args.acquire_timeout <= 120):
        p.error("--acquire-timeout must be 5..120 seconds")
    nominal_window_s = 10 * args.time_div_us * 1e-6
    if nominal_window_s >= NANO_BUFFER_POLL_BUDGET_S:
        p.error(
            f"Scope window must be shorter than {NANO_BUFFER_POLL_BUDGET_S:g}s "
            f"with the fixed {NANO_BUFFER_CAPACITY}-frame Nano buffer; "
            "use separate finite records for longer sessions"
        )
    if args.acquire_timeout <= nominal_window_s:
        p.error("--acquire-timeout must exceed the nominal scope window")
    if not 8 <= args.sync_count <= 200:
        p.error("--sync-count must be 8..200")
    if not 50 <= args.stale_ms <= 2000:
        p.error("--stale-ms must be 50..2000")
    if (not math.isfinite(args.controller_ready_timeout)
            or not 5 <= args.controller_ready_timeout <= 120):
        p.error("--controller-ready-timeout must be 5..120 seconds")
    try:
        args.operator_prompt_schedule = (
            load_operator_prompts(args.operator_prompts, nominal_window_s)
            if args.operator_prompts is not None else []
        )
    except (OSError, ValueError, UnicodeError) as exc:
        p.error(f"Invalid --operator-prompts: {exc}")
    if args.operator_prompt_schedule and args.trigger_position_pct != 0:
        p.error("--operator-prompts requires --trigger-position-pct 0")
    args.replay_provenance = None
    if args.replay_source is not None:
        try:
            replay = load_replay(args.replay_source,
                                 duration_s=nominal_window_s+.5,
                                 start_s=args.replay_start_s, stale_s=args.stale_ms/1000)
            args.replay_provenance = replay.provenance
        except (OSError, ValueError, KeyError, TypeError) as exc:
            p.error(f"Invalid replay source: {exc}")
    # Explicitly reuse the shape that record_scope.configure/verify expects.
    args.channels = [1, 2]
    args.scales = {1: args.ch1_v_div, 2: 2.0}
    args.trigger_channel = 2
    args.trigger_edge = "rising"
    args.trigger_level = 1.0
    return {
        "schema_version": 1, "experiment": "GRAY_TC_V1",
        "integrated_experiment": "MATRIX32_MCU_NANO_SCOPE_V1",
        "channels": [1, 2],
        "ch1": "Matrix32 physical output measured ONLY across verified dummy load",
        "ch2": "Nano Every D7 Gray timecode, rising +1V trigger",
        "ch1_v_div": args.ch1_v_div,
        "points": args.points, "time_div_us": args.time_div_us,
        "scope_record_count": args.count,
        "nominal_observed_scope_duration_s": args.count * 10 * args.time_div_us * 1e-6,
        "nominal_window_s": 10 * args.time_div_us * 1e-6,
        "trigger_position_pct": args.trigger_position_pct,
        "horizontal_delay_mode_requested": "OFF",
        "per_record_epochs": True, "epoch_sync_count": args.sync_count,
        "sync_transport": "binary_crc32_v1",
        "acquire_timeout_s": args.acquire_timeout,
        "matrix32_armed": args.arm,
        "verified_dummy_load_operator_confirmation": bool(args.dummy_load_confirmed),
        "mcu_stale_timeout_ms": args.stale_ms,
        "experimental_shared_sf_sc": args.experimental_shared_sf_sc,
        "replay_source": args.replay_provenance,
        "controller_ready_timeout_s": args.controller_ready_timeout,
        "capture_before_matrix32_start": args.capture_before_start,
        "acquisition_order": ("replay_ready_then_pre_sync_then_scope_RUN_triggers_replay"
                              if args.replay_provenance else
                              "controller_ready_then_pre_sync_then_arm_scope"),
        "no_person_connected": True,
        "clock_offset_calibrated": False,
        "sample_gap_free": False,
        "single_scope_record": args.count == 1,
        "records_are_independent": True,
        "matrix32_session_continuous_across_record_gaps": True,
        "waveform_trigger_source": "CH2_NANO",
        "matrix32_physical_output_timestamp_verified": False,
        "nano_mode_required": "10HZ_HW_TCA_BUFFERED",
        "nano_buffered_frames_required": True,
        "nano_buffer_capacity_required": NANO_BUFFER_CAPACITY,
        "nano_buffer_poll_budget_s": NANO_BUFFER_POLL_BUDGET_S,
        "effective_acquire_timeout_upper_bound_s": min(
            args.acquire_timeout, NANO_BUFFER_POLL_BUDGET_S),
        "operator_prompts": {
            "enabled": bool(args.operator_prompt_schedule),
            "source": (str(args.operator_prompts.resolve())
                       if args.operator_prompts is not None else None),
            "time_reference": PROMPT_TIME_REFERENCE,
            "scope_clock_synchronized": False,
            "timer_independent_of_scope_queries": True,
            "console_mode": "verbose" if args.verbose else "guided",
            "repeat_each_record": True,
            "schedule": args.operator_prompt_schedule,
        },
    }


def verify_nano_info(reply):
    """Match complete INFO fields, never firmware-name or numeric prefixes."""
    tokens = reply.split()
    if (len(tokens) < 3 or tokens[:2] != ["ACK", "INFO"]
            or not tokens[2].isdecimal() or (len(tokens) - 3) % 2):
        raise RuntimeError("Malformed Nano INFO reply: " + reply)
    pairs = list(zip(tokens[3::2], tokens[4::2]))
    fields = dict(pairs)
    required = {
        "FW": "GRAY_TC_V1", "PIN": "7", "BITS": "16",
        "FRAME_US": "100000", "FRAME_MODE": "10HZ_HW_TCA_BUFFERED",
    }
    if (len(fields) != len(pairs)
            or any(fields.get(key) != value for key, value in required.items())
            or fields.get("RUN") not in ("0", "1")):
        raise RuntimeError(
            "Integrated recording requires the final 10Hz hardware-timed "
            "buffered Nano firmware; received: " + reply
        )
    return fields


def acquire_buffered_record(nano, scope, args, record, child, *, console=None):
    """Bound buffering around one acquisition, including ambiguous ACK failures.

    Preserve the primary exception and cleanup outcome separately. START and
    BUFFER_BEGIN are not retried; an ACK timeout may follow a successful write.
    No normal FRAME telemetry or clock SYNC is requested during acquisition.
    """
    prompts = None
    if getattr(args, "operator_prompt_schedule", None):
        prompts = OperatorPrompts(
            args.operator_prompt_schedule, duration_s=10 * args.time_div_us * 1e-6,
            record_index=record["index"], record_count=args.count,
            emit=console.log_prompt if console is not None else None,
            display=console.show_prompt if console is not None else None,
        )
        record["operator_prompts"] = prompts.snapshot()
    begun_ns = time.monotonic_ns()
    buffer = {"status": "begin_attempted",
              "begin_attempted_monotonic_ns": begun_ns}
    record["nano_frame_buffer"] = buffer
    deadline_ns = begun_ns + round(NANO_BUFFER_POLL_BUDGET_S * 1e9)

    def check_capture():
        child.check()
        if child.start_failure.is_set():
            raise RuntimeError("Controller reported fault during scope acquisition")
        if time.monotonic_ns() >= deadline_ns:
            raise TimeoutError("Nano frame-buffer polling budget exhausted")

    def start_prompts(observation):
        check_capture()
        if prompts is not None:
            prompts.start(observation)
        if getattr(args, "replay_provenance", None) is not None:
            record["replay_trigger"] = child.trigger_replay(observation)

    def progress(observation):
        record["acquisition_progress"] = observation
        check_capture()
        if prompts is not None:
            prompts.check()
            prompts.observe(observation)

    try:
        buffer.update(nano.buffered_capture_begin())
        if buffer["capacity"] != NANO_BUFFER_CAPACITY:
            raise RuntimeError(
                f"Nano buffer capacity mismatch: expected {NANO_BUFFER_CAPACITY}, "
                f"received {buffer['capacity']}"
            )
        buffer["status"] = "buffering"
        remaining_s = (deadline_ns - time.monotonic_ns()) / 1e9
        if remaining_s <= 10 * args.time_div_us * 1e-6:
            raise TimeoutError("Insufficient Nano buffer time remains to arm scope")
        record["effective_acquire_timeout_s"] = min(args.acquire_timeout, remaining_s)
        record["arm_request_monotonic_ns"] = time.monotonic_ns()
        callbacks = {"on_progress": progress}
        if prompts is not None or getattr(args, "replay_provenance", None) is not None:
            callbacks["on_run_written"] = start_prompts
        record["acquisition"] = acquire(
            scope, record["effective_acquire_timeout_s"], **callbacks,
        )
        record["acquisition_completed_monotonic_ns"] = time.monotonic_ns()
        if prompts is not None:
            prompts.finish("acquisition_completed")
        # End immediately, before metadata writes, settings queries or transfers.
        buffer.update(nano.buffered_capture_end_and_dump(buffer))
        buffer["status"] = "verified"
    except BaseException as exc:
        if prompts is not None:
            prompts.finish("acquisition_failed")
        buffer["status"] = "failed"
        buffer["error"] = f"{type(exc).__name__}: {exc}"
        record["status"] = "failed"
        # BUFFER_END itself is not idempotent. If its ACK was already received
        # (even if later dump validation failed), the firmware has stopped
        # buffering. Otherwise explicitly attempt cleanup before outer STOP.
        if "end_ack" not in buffer:
            try:
                buffer["cleanup_end_ack"] = nano.command("BUFFER_END", timeout=2.0)
            except BaseException as cleanup_exc:
                buffer["cleanup_end_error"] = repr(cleanup_exc)
        raise
    finally:
        if prompts is not None:
            # Cancel before Nano END (above); join only after END/DUMP or the
            # cleanup attempt, never while the fixed frame buffer is active.
            prompts.finish("acquisition_exited")
            prompts.join(timeout=0.5)
            record["operator_prompts"] = prompts.snapshot()


def verify_gray_frame_evidence(decoded, buffer):
    """Qualify frame identities independently of physical CH1 pulse timing.

    NanoLog already validates every stored counter/timestamp. A complete scope
    frame must identify one of those rows, with no missing interior frames.
    This is not a clock fit, and imposes no residual or absolute-latency limit.
    """
    frames = decoded.get("frames", [])
    issues = []
    if decoded.get("decoded_frame_count") != len(frames):
        issues.append("Decoded frame count disagrees with the frame inventory")
    if len(frames) < 3:
        issues.append("Fewer than three complete Gray frames for clock alignment")
    count = buffer.get("frames_received", 0)
    first = buffer.get("first_frame_counter")
    start_times = []
    end_times = []
    if (not buffer.get("frame_payloads_validated") or buffer.get("frames_lost") != 0
            or not isinstance(first, int) or not 0 < count <= NANO_BUFFER_CAPACITY):
        issues.append("No complete validated Nano frame buffer for this record")
    else:
        previous_offset = None
        for frame in frames:
            try:
                short = int(frame["counter_mod_65536"])
                gray = int(frame["gray_hex"], 16)
                start = float(frame["scope_start_s"])
                end = float(frame["scope_end_s"])
            except (KeyError, TypeError, ValueError):
                issues.append("Malformed decoded Gray frame")
                continue
            offset = (short - first) % 65536
            if not 0 <= short < 65536 or offset >= count:
                issues.append(f"Scope Gray counter {short} is absent from this record's Nano buffer")
            if gray != short ^ (short >> 1):
                issues.append("Decoded Gray word disagrees with its matched Nano counter")
            if previous_offset is not None and offset != previous_offset + 1:
                issues.append("Decoded scope Gray counters are not consecutive")
            if (not math.isfinite(start) or not math.isfinite(end) or end <= start
                    or (start_times and start <= start_times[-1])):
                issues.append("Decoded scope frame timestamps are invalid or unordered")
            start_times.append(start)
            end_times.append(end)
            previous_offset = offset
    coverage = {}
    if start_times:
        try:
            first_sample = float(decoded["waveform"]["first_time_s"])
            last_sample = float(decoded["waveform"]["last_time_s"])
            if not (math.isfinite(first_sample) and math.isfinite(last_sample)
                    and first_sample < last_sample):
                raise ValueError("invalid waveform boundaries")
            # Partial frames at either boundary are expected. A few matched
            # frames at one end do not establish reference coverage for 20 s.
            allowance = 2 * NANO_FRAME_INTERVAL_S
            leading = start_times[0] - first_sample
            trailing = last_sample - end_times[-1]
            coverage = {"leading_uncovered_s": leading,
                        "trailing_uncovered_s": trailing,
                        "boundary_allowance_s": allowance}
            if not (0 <= leading <= allowance and 0 <= trailing <= allowance):
                issues.append("Complete Gray frames do not cover the waveform within boundary allowance")
        except (KeyError, TypeError, ValueError):
            issues.append("Waveform boundaries unavailable for Gray coverage verification")
    return {"complete": not issues, "issues": list(dict.fromkeys(issues)),
            "decoded_frames": len(frames), "buffered_frames": count,
            "coverage": coverage,
            "clock_fit_evaluated": False,
            "direct_ch1_evidence_invalidated": False}


class StimulationChild:
    """Isolated process preserves existing Matrix32 BLE watchdog and shutdown."""

    def __init__(self, root, *, arm, stale_ms, capture_before_start=False,
                 experimental_shared_sf_sc=False, replay_provenance=None,
                 replay_start_s=None):
        self.root = Path(root)
        self.arm = arm
        self.stale_ms = stale_ms
        self.experimental_shared_sf_sc = experimental_shared_sf_sc
        self.replay_provenance = replay_provenance
        self.replay_start_s = replay_start_s
        self.replay_ready = threading.Event()
        self.replay_completed = threading.Event()
        self.replay_ready_observed_ns = None
        self.capture_before_start = capture_before_start
        self.process = None
        self.log = None
        self.log_thread = None
        self.mcu_ready = threading.Event()
        self.matrix32_started = threading.Event()
        self.mcu_ready_observed_ns = None
        self.matrix32_started_observed_ns = None
        self.start_failure = threading.Event()

    def start(self):
        if self.process is not None:
            raise RuntimeError("Stimulation process already started")
        command = [sys.executable, "-u", str(ROOT / "run_tactile_stimulation.py"),
                   "--record", "--recordings-dir", str(self.root / "mcu_matrix32"),
                   "--stale-ms", str(self.stale_ms)]
        if self.arm:
            command.append("--arm")
        if self.experimental_shared_sf_sc:
            command.append("--experimental-shared-sf-sc")
            if self.arm:
                # The parent plan already requires explicit dummy-load confirmation.
                command.append("--dummy-load-confirmed")
        if self.replay_provenance is not None:
            command.extend(["--replay-source", self.replay_provenance["source_run"],
                            "--replay-duration-s", str(self.replay_provenance["duration_s"]),
                            "--replay-trigger", str(self.root / "replay_start.json")])
            if self.replay_start_s is not None:
                command.extend(["--replay-start-s", str(self.replay_start_s)])
            if self.arm and "--dummy-load-confirmed" not in command:
                command.append("--dummy-load-confirmed")
        # Separate process group: cancelling this scope session must cancel
        # the entire BLE/MCU task group, not just this parent process.
        self.process = subprocess.Popen(
            command, cwd=ROOT, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            start_new_session=True,
        )
        self.log = (self.root / "matrix32_runner.log").open(
            "w", encoding="utf-8", buffering=1
        )
        def relay():
            try:
                for line in self.process.stdout:
                    observed_ns = time.monotonic_ns()
                    self.log.write(line)
                    if "[REPLAY] Ready;" in line:
                        self.replay_ready_observed_ns = observed_ns
                        self.replay_ready.set()
                    if "[REPLAY] Completed;" in line:
                        self.replay_completed.set()
                    if "[MCU] Configured; ready for live data." in line:
                        self.mcu_ready_observed_ns = observed_ns
                        self.mcu_ready.set()
                    if line.startswith("[START] Matrix32 CH1:"):
                        self.matrix32_started_observed_ns = observed_ns
                        self.matrix32_started.set()
                    if ("[ERROR]" in line or "[WARNING] Matrix32" in line
                            or "[SHUTDOWN]" in line):
                        self.start_failure.set()
                    if any(tag in line for tag in
                           ("[ERROR]", "[STOP]", "[START]", "[MCU]", "[BLE]", "[REPLAY]",
                            "[SHUTDOWN]", "[WARNING]", "[RECORD]")):
                        print(f"[MATRIX32] {line.rstrip()}", flush=True)
            finally:
                self.log.flush()
        self.log_thread = threading.Thread(
            target=relay, name="integrated-matrix32-log", daemon=True
        )
        self.log_thread.start()
        return self.process.pid

    def check(self):
        if self.process is not None and self.process.poll() is not None:
            raise RuntimeError(
                f"MCU/Matrix32 controller exited early with code "
                f"{self.process.returncode}; see matrix32_runner.log"
            )

    def wait_ready(self, timeout):
        """Require MCU config ACKs, and actual BLE START return when armed.

        stdout is observable host-side evidence of protocol completion, not
        proof of electrical pulses. No scope capture on startup timeout.
        """
        if self.process is None:
            raise RuntimeError("Controller has not been started")
        deadline = time.monotonic() + timeout
        expected = (self.replay_ready if self.replay_provenance is not None else
                    self.matrix32_started if self.arm and not self.capture_before_start
                    else self.mcu_ready)
        while not expected.is_set():
            self.check()
            if self.start_failure.is_set():
                raise RuntimeError(
                    "Matrix32 controller reported failure while starting; "
                    "see matrix32_runner.log"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Controller not ready after {timeout:g}s: "
                    + ("no ACK-verified Matrix32 START" if self.arm and not self.capture_before_start
                       else "MCU configuration handshake incomplete")
                    + "; see matrix32_runner.log"
                )
            expected.wait(timeout=min(0.05, max(0.001, deadline-time.monotonic())))
        if self.replay_provenance is not None:
            self.check()
            return {"replay_ready_observed_monotonic_ns": self.replay_ready_observed_ns,
                    "mcu_configuration_acknowledged": False,
                    "startup_electrical_output_verified": False,
                    "scope_capture_precedes_matrix32_start": True}
        # Require the MCU configuration ACK indication in both live modes.
        if not self.mcu_ready.is_set():
            raise RuntimeError("Matrix32 START reported without MCU handshake")
        self.check()
        return {
            "mcu_configured_observed_monotonic_ns": self.mcu_ready_observed_ns,
            "matrix32_start_observed_monotonic_ns": self.matrix32_started_observed_ns,
            "startup_electrical_output_verified": False,
            "scope_capture_precedes_matrix32_start": bool(
                self.arm and self.capture_before_start and not self.matrix32_started.is_set()),
        }

    def trigger_replay(self, observation):
        if self.replay_provenance is None or not self.replay_ready.is_set():
            raise RuntimeError("Replay is not ready")
        self.check()
        path = self.root/"replay_start.json"
        if path.exists():
            raise FileExistsError("Replay trigger already exists")
        # Host RUN write is the replay start reference, not physical trigger time.
        marker = {"start_ns": round(observation["run_write_end_monotonic_s"]*1e9),
                  "source": self.replay_provenance}
        atomic_json(path, marker)
        return marker

    def stop(self):
        if self.process is None:
            return {"started": False}
        result = {"started": True, "pid": self.process.pid,
                  "graceful_shutdown_confirmed": False,
                  "output_state": "UNKNOWN"}
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                result["forced_termination"] = True
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=3)
        result["exit_code"] = self.process.returncode
        if self.log_thread is not None:
            self.log_thread.join(timeout=3)
        if self.log is not None:
            self.log.close()
        # A successful process exit is not proof physical current is OFF.
        if (self.root / "matrix32_runner.log").exists():
            output = (self.root / "matrix32_runner.log").read_text(
                encoding="utf-8", errors="replace"
            )
            result["stop_write_reported"] = (
                "[SHUTDOWN] Matrix32 STOP sent." in output
            )
            result["graceful_shutdown_confirmed"] = (
                result["stop_write_reported"] and not result.get("forced_termination")
            )
        return result


def run(args, requested):
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_" + args.label
    # The controller subprocess runs from ROOT; use an absolute path so
    # its traces land in the same session even when invoked from elsewhere.
    root = (args.output_root / run_id).resolve()
    (root / "waveforms").mkdir(parents=True, exist_ok=False)
    (root / "mcu_matrix32").mkdir()
    if not args.operator_prompt_schedule:
        return _run_session(args, requested, root)
    console = GuidedConsole(verbose=args.verbose)
    with console.capture(root / "console.log"):
        print(json.dumps(requested, indent=2), flush=True)
        console.status("SETTING UP GUIDED RECORDING",
                       f"{args.count} record(s), {requested['nominal_window_s']:g}s each. "
                       f"Full diagnostics: {root / 'console.log'}")
        try:
            return _run_session(args, requested, root, console=console)
        except BaseException as exc:
            console.log_prompt(traceback.format_exc())
            console.status("RECORDING INTERRUPTED" if isinstance(exc, KeyboardInterrupt)
                           else "RECORDING FAILED",
                           f"{type(exc).__name__}: {exc}. Details: {root / 'console.log'}")
            raise


def _run_session(args, requested, root, *, console=None):
    meta = {
        "run_id": root.name, "run_dir": str(root), "plan": requested,
        "status": "started", "started_utc": utc(),
        "records": [], "sync_measurements": [],
        "host_clock_synchronization_verified": False,
    }
    if console is not None:
        meta["console_log"] = "console.log"
    child = StimulationChild(root, arm=args.arm, stale_ms=args.stale_ms,
                             capture_before_start=args.capture_before_start,
                             experimental_shared_sf_sc=args.experimental_shared_sf_sc,
                             replay_provenance=args.replay_provenance,
                             replay_start_s=args.replay_start_s)
    nano_started = False
    try:
        nano_port = resolve_port(args.nano_port, "nano")
        meta["nano_port"] = nano_port
        with NanoLog(nano_port, root / "nano_serial.jsonl") as nano:
            meta["ping"] = nano.synchronize()
            meta["nano_info"] = nano.command("INFO")
            meta["nano_info_fields"] = verify_nano_info(meta["nano_info"])
            if meta["nano_info_fields"]["RUN"] == "1":
                nano.command("STOP")
            # One START per session. No automatic retry on timeout.
            nano_started = True
            try:
                meta["nano_start_ack"] = nano.command("START")
                print(f"[NANO] {nano_port}: {meta['nano_start_ack']}", flush=True)
                node = resolve_scope_device(args.scope_device)
                meta["scope_node"] = str(node)
                with open(node, "r+b", buffering=0) as scope:
                    meta["scope_identity"] = verify_scope_identity(query(scope, "*IDN?"))
                    configure(scope, args)
                    meta["scope_settings_before"] = scope_settings(
                        scope, args.channels, args.trigger_channel,
                        horizontal_position=True
                    )
                    verify(meta["scope_settings_before"], args)
                    print("[SCOPE] Configuration verified, CH2 Nano trigger at 0% "
                          "(or requested position)", flush=True)
                    # Keep one Feather/Matrix32 controller alive for ALL records.
                    # Scope transfers occur between records; physical samples
                    # during those gaps do not exist and must never be stitched.
                    if args.operator_prompt_schedule:
                        if args.arm and not args.capture_before_start:
                            console.status("PRESS AND HOLD THE SELECTED TAXEL",
                                           "Keep holding while the controller starts. "
                                           "Timed instructions will appear automatically.")
                        else:
                            console.status("GET READY FOR TIMED INSTRUCTIONS",
                                           "The cue timer starts when the scope is armed.")
                    meta["controller_started_host_monotonic_ns"] = time.monotonic_ns()
                    meta["controller_pid"] = child.start()
                    print(f"[MATRIX32] Controller starting before scope arming; "
                          f"{'ARMED BENCH OUTPUT' if args.arm else 'DRY RUN'}",
                          flush=True)
                    meta["controller_ready"] = child.wait_ready(
                        args.controller_ready_timeout
                    )
                    meta["controller_ready_observed_monotonic_ns"] = time.monotonic_ns()
                    print(("[MATRIX32] Replay ready" if args.replay_provenance else "[MATRIX32] MCU configured")
                          + ("; Matrix32 START returned" if args.arm and not args.capture_before_start
                             else "; armed scope may precede Matrix32 START" if args.arm
                             else "; dry-run controller ready")
                          + f"; preparing {args.count} separate scope records",
                          flush=True)

                    for index in range(1, args.count + 1):
                        child.check()
                        if child.start_failure.is_set():
                            raise RuntimeError(
                                "Controller reported STOP, warning, or failure; "
                                "refusing to start another scope record"
                            )
                        record = {
                            "index": index, "status": "started", "started_utc": utc(),
                            "clock_epoch": {
                                "epoch_id": index, "status": "started",
                                "pre_sync_sequence_ids": [],
                                "post_sync_sequence_ids": [],
                            },
                        }
                        meta["records"].append(record)
                        epoch = record["clock_epoch"]
                        print(f"[RECORD {index}/{args.count}] Preparing independent "
                              "scope acquisition", flush=True)
                        if console is not None:
                            console.status(f"PREPARING RECORD {index}/{args.count}",
                                           "Keep holding. Follow DO NOW when the timed display starts.")

                        # Separate Nano->host clock epochs around every capture.
                        # Nano full frame counter stays running across gaps.
                        for _ in range(args.sync_count):
                            child.check()
                            if child.start_failure.is_set():
                                raise RuntimeError(
                                    "Controller reported fault during pre-SYNC"
                                )
                            row = nano.binary_sync_exchange()
                            meta["sync_measurements"].append(row)
                            epoch["pre_sync_sequence_ids"].append(row["sequence_id"])
                        epoch["pre_sync_end_host_monotonic_ns"] = time.monotonic_ns()
                        child.check()
                        print(f"[SYNC {index}] {args.sync_count} CRC32 exchanges "
                              "before scope arming", flush=True)
                        atomic_json(root / "metadata.json", meta)

                        # Record actual Nano START-edge timestamps ONLY inside
                        # this finite scope acquisition. Do not print FRAME
                        # telemetry while acquiring; dump after scope completes.
                        acquire_buffered_record(nano, scope, args, record, child,
                                                console=console)
                        if args.operator_prompt_schedule:
                            delivery = record["operator_prompts"]
                            complete = delivery["delivery_complete"]
                            console.status(
                                "DONE WITH THIS RECORD" if complete else "PROMPTS INCOMPLETE",
                                f"{delivery['printed_count']}/{delivery['cue_count']} cues delivered. "
                                "Scope acquisition ended; you may release the taxel. "
                                "Saving waveforms now.",
                            )
                        if args.replay_provenance is not None:
                            # Finite replay can end shortly after the 20 s capture.
                            # Require its explicit completion before transferring waveforms.
                            replay_deadline = time.monotonic()+6
                            while not child.replay_completed.wait(timeout=.05):
                                child.check()
                                if child.start_failure.is_set() or time.monotonic() >= replay_deadline:
                                    raise RuntimeError("Replay did not complete with confirmed STOP write")
                            record["replay_completion_reported"] = True
                        atomic_json(root / "metadata.json", meta)
                        record["scope_settings_after_acquisition"] = scope_settings(
                            scope, args.channels, args.trigger_channel,
                            horizontal_position=True
                        )
                        verify(record["scope_settings_after_acquisition"], args)
                        for _ in range(args.sync_count):
                            child.check()
                            if child.start_failure.is_set():
                                raise RuntimeError(
                                    "Controller reported fault during post-SYNC"
                                )
                            row = nano.binary_sync_exchange()
                            meta["sync_measurements"].append(row)
                            epoch["post_sync_sequence_ids"].append(row["sequence_id"])
                        epoch["post_sync_end_host_monotonic_ns"] = time.monotonic_ns()
                        epoch["status"] = "collected"
                        print(f"[SYNC {index}] {args.sync_count} CRC32 exchanges "
                              "after scope acquisition", flush=True)

                        # Download the frozen scope SEQUENCE before rearming.
                        # The controller remains active on the MCU/BLE side
                        # during the unobserved USBTMC transfer interval.
                        record["channels"] = {}
                        for channel in args.channels:
                            file = (root / "waveforms"
                                    / f"record_{index:05d}_ch{channel}.csv")
                            channel_record = {"path": str(file.relative_to(root))}
                            record["channels"][f"CH{channel}"] = channel_record
                            channel_record["transfer"] = capture(
                                scope, channel, args.points, file)
                            channel_record["waveform"] = validate_csv(file, args.points)
                            channel_record["timing_verification"] = verify_waveform_timing(
                                channel_record["waveform"],
                                record["scope_settings_after_acquisition"], args,
                                transfer=channel_record["transfer"],
                            )
                            channel_record["scaling_verification"] = verify_waveform_scaling(
                                channel_record["transfer"],
                                record["scope_settings_after_acquisition"],
                            )
                        record["decoded_gray"] = decode_csv(
                            root / record["channels"]["CH2"]["path"]
                        )
                        record["clock_frame_evidence"] = verify_gray_frame_evidence(
                            record["decoded_gray"], record["nano_frame_buffer"])
                        if not record["clock_frame_evidence"]["complete"]:
                            print(f"[FRAME EVIDENCE WARN] Record {index}: "
                                  + "; ".join(record["clock_frame_evidence"]["issues"])
                                  + "; raw CH1+CH2 retained", flush=True)
                        record["status"] = "captured"
                        record["finished_utc"] = utc()
                        atomic_json(root / "metadata.json", meta)
                        print(f"[RECORD {index}/{args.count}] Captured "
                              f"{record['decoded_gray']['decoded_frame_count']} "
                              "complete Gray frames; raw CH1+CH2 saved", flush=True)

                    # Shutdown follows the LAST frozen/downloaded record.
                    # A STOP/ACK is not a physical proof that the load is off.
                    meta["matrix32_shutdown"] = child.stop()
                    meta["matrix32_shutdown"]["physical_output_verified_off"] = False
                    meta["matrix32_shutdown"]["operator_must_confirm_output_off"] = True
                    meta["clock_frame_evidence_complete"] = all(
                        item["clock_frame_evidence"]["complete"]
                        for item in meta["records"])
                    atomic_json(root / "metadata.json", meta)
                    meta["status"] = "complete"
            finally:
                if nano_started:
                    try:
                        meta["nano_stop_ack"] = nano.command("STOP", timeout=2.0)
                    except BaseException as exc:
                        meta["nano_stop_error"] = repr(exc)
    except BaseException as exc:
        meta["status"] = "failed"
        meta["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        # Fail closed if any operation fails: explicitly stop child and Nano.
        if child.process is not None and "matrix32_shutdown" not in meta:
            try:
                meta["matrix32_shutdown"] = child.stop()
            except BaseException as exc:
                meta["matrix32_shutdown"] = {
                    "output_state": "UNKNOWN", "stop_error": repr(exc)}
        if child.process is not None:
            meta["matrix32_shutdown"]["physical_output_verified_off"] = False
            meta["matrix32_shutdown"]["operator_must_confirm_output_off"] = True
        meta["finished_utc"] = utc()
        if args.operator_prompt_schedule:
            meta["operator_prompt_delivery_complete"] = bool(meta["records"]) and all(
                row.get("operator_prompts", {}).get("delivery_complete", False)
                for row in meta["records"]
            )
        atomic_json(root / "metadata.json", meta)
        print(f"[INTEGRATED] Saved {root} ({meta['status']})", flush=True)
        if child.process is not None:
            print("[SAFETY] Verify Matrix32 physically OFF; a software STOP "
                  "write is not an electrical proof.", flush=True)

    # Offline analysis is diagnostic only. It must not erase the valid capture.
    try:
        if not meta["clock_frame_evidence_complete"]:
            raise ValueError("Incomplete frame evidence; clock alignment is not qualified")
        from analyze_gray_epoch_experiment import analyze
        alignment, clock = analyze(root)
        atomic_json(root / "gray_alignment.json", alignment)
        atomic_json(root / "epoch_clock_analysis.json", clock)
        meta["clock_analysis"] = {"status": "diagnostic_complete",
                                  "absolute_latency_calibrated": False}
        print(f"[ANALYSIS] Clock epochs: "
              f"{clock['qualified_provisional_records']} provisional, "
              f"{clock['exploratory_local_fit_records']} exploratory",
              flush=True)
        for item in clock["records"]:
            print(f"[ANALYSIS] Record {item['record_index']}: "
                  f"scope RMS={item['scope_edge_fit_rms_us']:.1f} us, "
                  f"Nano->PC drift={item.get('fit', {}).get('drift_ppm_nano_to_host', float('nan')):.1f} ppm, "
                  f"status={item['status']}", flush=True)
    except Exception as exc:
        meta["clock_analysis"] = {"status": "unavailable",
                                  "error": f"{type(exc).__name__}: {exc}",
                                  "direct_ch1_evidence_invalidated": False}
        print(f"[ANALYSIS WARN] Clock analysis: {exc}; raw data retained",
              flush=True)
    finally:
        atomic_json(root / "metadata.json", meta)
    # Optional offline visual aid: never make plot failure invalidate a capture.
    try:
        from oscilloscope.plot_mcu_scope_coverage import render as plot_mcu_coverage
        plot_mcu_coverage(root)
    except Exception as exc:
        print(f"[MCU PLOT WARN] {type(exc).__name__}: {exc}; raw capture retained",
              flush=True)
    if console is not None:
        console.status(
            "RECORDING SAVED — ALL CUES DELIVERED"
            if meta["operator_prompt_delivery_complete"] else "RECORDING SAVED — PROMPTS INCOMPLETE",
            str(root),
        )
    return meta


def preview_operator_prompts(args):
    """Rehearse the real timer/display without opening a device or recording."""
    console = GuidedConsole(verbose=args.verbose)
    console.status("PROMPT REHEARSAL — NO DEVICES OPENED",
                   "Follow the display to check that the instructions are easy to see.")
    prompts = OperatorPrompts(
        args.operator_prompt_schedule, duration_s=10 * args.time_div_us * 1e-6,
        emit=console.log_prompt, display=console.show_prompt,
    )
    anchor = time.monotonic()
    try:
        prompts.start({"run_write_begin_monotonic_s": anchor,
                       "run_write_end_monotonic_s": anchor, "simulated": True})
        while not prompts.join(timeout=0.2):
            prompts.check()
        prompts.check()
    finally:
        prompts.finish("preview_exited")
        prompts.join(timeout=0.5)
    result = prompts.snapshot()
    console.status("REHEARSAL COMPLETE" if result["delivery_complete"]
                   else "REHEARSAL INCOMPLETE",
                   f"{result['printed_count']}/{result['cue_count']} cues delivered. No devices opened.")
    return result


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    requested = plan(args, p)
    if args.preview_prompts:
        return preview_operator_prompts(args)
    if not (args.run and args.operator_prompt_schedule):
        print(json.dumps(requested, indent=2), flush=True)
    if not args.run:
        print("[DRY RUN] No devices opened, no stimulation commanded.", flush=True)
        return requested
    return run(args, requested)


if __name__ == "__main__":
    main()
