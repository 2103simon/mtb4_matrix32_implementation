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
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from communication.serial_ports import resolve_port
from record_scope import (
    acquire, atomic_json, configure, scope_settings, validate_csv, verify,
    verify_waveform_timing, verify_waveform_scaling,
)
from scope_device import resolve_scope_device, verify_scope_identity
from tek_mdo_usb import capture, query
from oscilloscope.test_gray_timecode import NanoLog, utc
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
    p.add_argument("--stale-ms", type=int, default=250)
    p.add_argument("--capture-before-start", action="store_true",
                   help="Bench-only: arm scope after MCU ACKs even if positive sensor input "
                        "has not yet triggered Matrix32 START; records may contain no output")
    p.add_argument("--controller-ready-timeout", type=float, default=45.0,
                   help="Seconds to wait for Feather config and, with --arm, "
                        "ACK-verified Matrix32 START before scope is armed")
    p.add_argument("--output-root", type=Path, default=Path("data/recordings"))
    p.add_argument("--label", default="integrated_matrix32_scope")
    return p


def plan(args, p):
    import re
    if args.arm and (not args.run or not args.dummy_load_confirmed):
        p.error("--arm requires BOTH --run and --dummy-load-confirmed")
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
        "controller_ready_timeout_s": args.controller_ready_timeout,
        "capture_before_matrix32_start": args.capture_before_start,
        "acquisition_order": "controller_ready_then_pre_sync_then_arm_scope",
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


def acquire_buffered_record(nano, scope, args, record, child):
    """Bound buffering around one acquisition, including ambiguous ACK failures.

    Preserve the primary exception and cleanup outcome separately. START and
    BUFFER_BEGIN are not retried; an ACK timeout may follow a successful write.
    No normal FRAME telemetry or clock SYNC is requested during acquisition.
    """
    begun_ns = time.monotonic_ns()
    buffer = {"status": "begin_attempted",
              "begin_attempted_monotonic_ns": begun_ns}
    record["nano_frame_buffer"] = buffer
    deadline_ns = begun_ns + round(NANO_BUFFER_POLL_BUDGET_S * 1e9)

    def progress(observation):
        record["acquisition_progress"] = observation
        child.check()
        if child.start_failure.is_set():
            raise RuntimeError("Controller reported fault during scope acquisition")
        if time.monotonic_ns() >= deadline_ns:
            raise TimeoutError("Nano frame-buffer polling budget exhausted")

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
        record["acquisition"] = acquire(
            scope, record["effective_acquire_timeout_s"], on_progress=progress,
        )
        record["acquisition_completed_monotonic_ns"] = time.monotonic_ns()
        # End immediately, before metadata writes, settings queries or transfers.
        buffer.update(nano.buffered_capture_end_and_dump(buffer))
        buffer["status"] = "verified"
    except BaseException as exc:
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

    def __init__(self, root, *, arm, stale_ms, capture_before_start=False):
        self.root = Path(root)
        self.arm = arm
        self.stale_ms = stale_ms
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
                           ("[ERROR]", "[STOP]", "[START]", "[MCU]", "[BLE]",
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
        expected = (self.matrix32_started if self.arm and not self.capture_before_start
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
        # Require the MCU configuration ACK indication in both modes.
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
    meta = {
        "run_id": run_id, "run_dir": str(root), "plan": requested,
        "status": "started", "started_utc": utc(),
        "records": [], "sync_measurements": [],
        "host_clock_synchronization_verified": False,
    }
    child = StimulationChild(root, arm=args.arm, stale_ms=args.stale_ms,
                             capture_before_start=args.capture_before_start)
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
                    meta["controller_started_host_monotonic_ns"] = time.monotonic_ns()
                    meta["controller_pid"] = child.start()
                    print(f"[MATRIX32] Controller starting before scope arming; "
                          f"{'ARMED BENCH OUTPUT' if args.arm else 'DRY RUN'}",
                          flush=True)
                    meta["controller_ready"] = child.wait_ready(
                        args.controller_ready_timeout
                    )
                    meta["controller_ready_observed_monotonic_ns"] = time.monotonic_ns()
                    print("[MATRIX32] MCU configured"
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
                        acquire_buffered_record(nano, scope, args, record, child)
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
    return meta


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    requested = plan(args, p)
    print(json.dumps(requested, indent=2), flush=True)
    if not args.run:
        print("[DRY RUN] No devices opened, no stimulation commanded.", flush=True)
        return requested
    return run(args, requested)


if __name__ == "__main__":
    main()
