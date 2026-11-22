"""First integrated, finite Matrix32 + Feather M4 + Nano + Tek scope recording.

Dry run by default. --run opens devices; --arm and --dummy-load-confirmed
are additionally required to send Matrix32 START/SF/SC commands.
Use only with a verified isolated dummy load; NEVER a person.
Scope CH1 = physical Matrix32 dummy-load voltage (operator wiring),
CH2 = Nano Every D7 Gray reference + rising-edge trigger at 1 V.
Scope single-sequence acquisition = 2 s/div, 1M samples, 0% pretrigger.
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
)
from scope_device import resolve_scope_device, verify_scope_identity
from tek_mdo_usb import capture, query
from test_gray_timecode import NanoLog, utc
from decode_gray_timecode import decode_csv

ROOT = Path(__file__).resolve().parents[1]


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
    p.add_argument("--time-div-us", type=float, default=2000000.0)
    p.add_argument("--trigger-position-pct", type=float, default=0.0)
    p.add_argument("--acquire-timeout", type=float, default=90.0)
    p.add_argument("--sync-count", type=int, default=30,
                   help="Number of binary CRC32 sync exchanges before AND after recording")
    p.add_argument("--stale-ms", type=int, default=250)
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
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.label):
        p.error("Invalid --label")
    if not 100 <= args.points <= 20000000:
        p.error("--points must be 100..20000000")
    if not (math.isfinite(args.time_div_us) and 20000 <= args.time_div_us <= 3000000):
        p.error("--time-div-us must be 20000..3000000")
    if not (math.isfinite(args.ch1_v_div) and 0.001 <= args.ch1_v_div <= 100):
        p.error("--ch1-v-div must be 0.001..100")
    if not (math.isfinite(args.trigger_position_pct)
            and 0 <= args.trigger_position_pct <= 100):
        p.error("--trigger-position-pct must be finite and 0..100")
    if not (math.isfinite(args.acquire_timeout)
            and 5 <= args.acquire_timeout <= 120):
        p.error("--acquire-timeout must be 5..120 seconds")
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
        "acquisition_order": "controller_ready_then_pre_sync_then_arm_scope",
        "no_person_connected": True,
        "clock_offset_calibrated": False,
        "sample_gap_free": False,
        "single_scope_record": True,
        "waveform_trigger_source": "CH2_NANO",
        "matrix32_physical_output_timestamp_verified": False,
    }


class StimulationChild:
    """Isolated process preserves existing Matrix32 BLE watchdog and shutdown."""

    def __init__(self, root, *, arm, stale_ms):
        self.root = Path(root)
        self.arm = arm
        self.stale_ms = stale_ms
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
        expected = self.matrix32_started if self.arm else self.mcu_ready
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
                    + ("no ACK-verified Matrix32 START" if self.arm
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
    child = StimulationChild(root, arm=args.arm, stale_ms=args.stale_ms)
    nano_started = False
    try:
        nano_port = resolve_port(args.nano_port, "nano")
        meta["nano_port"] = nano_port
        with NanoLog(nano_port, root / "nano_serial.jsonl") as nano:
            meta["ping"] = nano.synchronize()
            meta["nano_info"] = nano.command("INFO")
            if "FW GRAY_TC_V1" not in meta["nano_info"] or "PIN 7" not in meta["nano_info"]:
                raise RuntimeError("Nano GRAY_TC_V1 firmware on D7 required")
            if "RUN 1" in meta["nano_info"]:
                nano.command("STOP")
            # One START per session. No automatic retry on timeout.
            nano_started = True
            meta["nano_start_ack"] = nano.command("START")
            print(f"[NANO] {nano_port}: {meta['nano_start_ack']}", flush=True)
            try:
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
                    record = {
                        "index": 1, "status": "started", "started_utc": utc(),
                        "clock_epoch": {
                            "epoch_id": 1, "status": "started",
                            "pre_sync_sequence_ids": [],
                            "post_sync_sequence_ids": [],
                        },
                    }
                    meta["records"].append(record)
                    epoch = record["clock_epoch"]
                    # The scope is configured but explicitly STOPPED here.
                    # Startup and BLE configuration can take multiple seconds;
                    # do not let that startup consume the finite waveform.
                    record["controller_started_host_monotonic_ns"] = time.monotonic_ns()
                    record["controller_pid"] = child.start()
                    print(f"[MATRIX32] Controller starting before scope arming; "
                          f"{'ARMED BENCH OUTPUT' if args.arm else 'DRY RUN'}",
                          flush=True)
                    record["controller_ready"] = child.wait_ready(
                        args.controller_ready_timeout
                    )
                    record["controller_ready_observed_monotonic_ns"] = time.monotonic_ns()
                    print("[MATRIX32] MCU configured"
                          + ("; Matrix32 START returned" if args.arm
                             else "; dry-run controller ready")
                          + "; preparing scope acquisition", flush=True)

                    # Bracket capture using fresh, independent Nano SYNCs.
                    # For safety, fail if the controller stops while syncing.
                    for _ in range(args.sync_count):
                        child.check()
                        row = nano.binary_sync_exchange()
                        meta["sync_measurements"].append(row)
                        epoch["pre_sync_sequence_ids"].append(row["sequence_id"])
                    epoch["pre_sync_end_host_monotonic_ns"] = time.monotonic_ns()
                    child.check()
                    print(f"[SYNC] {args.sync_count} CRC32 exchanges before arming",
                          flush=True)
                    atomic_json(root / "metadata.json", meta)

                    record["arm_request_monotonic_ns"] = time.monotonic_ns()
                    record["acquisition"] = acquire(
                        scope, args.acquire_timeout,
                        on_progress=lambda _: child.check(),
                    )
                    record["acquisition_completed_monotonic_ns"] = time.monotonic_ns()
                    record["scope_settings_after_acquisition"] = scope_settings(
                        scope, args.channels, args.trigger_channel,
                        horizontal_position=True
                    )
                    verify(record["scope_settings_after_acquisition"], args)
                    for _ in range(args.sync_count):
                        child.check()
                        row = nano.binary_sync_exchange()
                        meta["sync_measurements"].append(row)
                        epoch["post_sync_sequence_ids"].append(row["sequence_id"])
                    epoch["post_sync_end_host_monotonic_ns"] = time.monotonic_ns()
                    epoch["status"] = "collected"
                    print(f"[SYNC] {args.sync_count} CRC32 exchanges after acquisition",
                          flush=True)
                    # Terminate the Matrix32 process BEFORE potentially slow
                    # 2 x million-point USBTMC waveform transfers.
                    meta["matrix32_shutdown"] = child.stop()
                    meta["matrix32_shutdown"]["physical_output_verified_off"] = False
                    meta["matrix32_shutdown"]["operator_must_confirm_output_off"] = True
                    atomic_json(root / "metadata.json", meta)

                    record["channels"] = {}
                    for channel in args.channels:
                        file = root / "waveforms" / f"record_00001_ch{channel}.csv"
                        record["channels"][f"CH{channel}"] = {
                            "path": str(file.relative_to(root)),
                            "transfer": capture(scope, channel, args.points, file),
                            "waveform": validate_csv(file, args.points),
                        }
                    record["decoded_gray"] = decode_csv(
                        root / record["channels"]["CH2"]["path"]
                    )
                    record["status"] = "captured"
                    print(f"[GRAY] {record['decoded_gray']['decoded_frame_count']} "
                          f"complete frames", flush=True)
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
        atomic_json(root / "metadata.json", meta)
        print(f"[INTEGRATED] Saved {root} ({meta['status']})", flush=True)
        if child.process is not None:
            print("[SAFETY] Verify Matrix32 physically OFF; a software STOP "
                  "write is not an electrical proof.", flush=True)

    # Offline analysis is diagnostic only. It must not erase the valid capture.
    try:
        from analyze_gray_epoch_experiment import analyze
        alignment, clock = analyze(root)
        atomic_json(root / "gray_alignment.json", alignment)
        atomic_json(root / "epoch_clock_analysis.json", clock)
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
        print(f"[ANALYSIS WARN] Clock analysis: {exc}; raw data retained",
              flush=True)
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
