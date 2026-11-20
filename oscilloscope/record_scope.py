"""Configurable Tektronix finite-record acquisition (scope only; no stimulation).

Uses the existing validated USBTMC transfer implementation. Dry-run by default.
Run under an external watchdog because USBTMC I/O can block.
"""
import argparse
import csv
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time

from tek_mdo_usb import capture, query, write
from scope_device import resolve_scope_device, verify_scope_identity


def scope_settings(f, channels, trigger_channel=None, *, horizontal_position=False):
    commands = {
        "identity": "*IDN?",
        "time_div_s": "HORizontal:SCALe?",
        "record_length": "HORizontal:RECOrdlength?",
        "sample_rate": "HORizontal:SAMPLERate?",
        "acquisition_mode": "ACQuire:MODe?",
        "acquisition_stopafter": "ACQuire:STOPAfter?",
        "fast_acquisition": "ACQuire:FASTAcq:STATE?",
        "trigger_mode": "TRIGger:A:MODe?",
        "trigger_source": "TRIGger:A:EDGE:SOUrce?",
        "trigger_slope": "TRIGger:A:EDGE:SLOpe?",
        "trigger_level_v": f"TRIGger:A:LEVel:CH{trigger_channel}?" if trigger_channel is not None else None,
    }
    if horizontal_position:
        commands["horizontal_delay_mode"] = "HORizontal:DELay:MODe?"
        commands["trigger_position_pct"] = "HORizontal:POSition?"
    result = {key: query(f, cmd).strip() for key, cmd in commands.items() if cmd}
    for ch in channels:
        result[f"ch{ch}_v_div"] = query(f, f"CH{ch}:SCAle?").strip()
        result[f"ch{ch}_enabled"] = query(f, f"SELect:CH{ch}?").strip()
    return result


def verify(settings, args):
    def close(key, target):
        actual = float(settings[key])
        if not math.isfinite(actual) or not math.isclose(actual, target, rel_tol=0.025, abs_tol=1e-12):
            raise RuntimeError(f"{key}: expected {target}, scope reports {actual}")
    if "TEKTRONIX" not in settings["identity"].upper():
        raise RuntimeError("Unexpected instrument identity")
    close("time_div_s", args.time_div_us * 1e-6)
    if args.trigger_position_pct is not None:
        mode = settings["horizontal_delay_mode"].strip().upper()
        if mode not in ("OFF", "0", "0.0"):
            raise RuntimeError(f"Horizontal delay must be OFF, scope reports {mode!r}")
        actual_position = float(settings["trigger_position_pct"])
        if (not math.isfinite(actual_position) or
                not math.isclose(actual_position, args.trigger_position_pct,
                                 abs_tol=0.5, rel_tol=0)):
            raise RuntimeError(
                f"Horizontal trigger position mismatch: "
                f"expected {args.trigger_position_pct}%, scope reports "
                f"{actual_position}%"
            )
    if int(float(settings["record_length"])) != args.points:
        raise RuntimeError("Scope record length does not match requested points")
    if settings["acquisition_mode"].upper() not in ("SAM", "SAMP", "SAMPLE"):
        raise RuntimeError("Acquisition mode must be SAMPLE for a finite waveform")
    if settings["acquisition_stopafter"].upper() not in ("SEQ", "SEQU", "SEQUENCE"):
        raise RuntimeError("Acquisition must stop after one SEQUENCE")
    if settings["fast_acquisition"].upper() not in ("OFF", "0", "0.0"):
        raise RuntimeError("FastAcq must be OFF for ordinary finite acquisition")
    if settings["trigger_mode"].upper() not in ("NORM", "NORMAL"):
        raise RuntimeError("Trigger must be NORMAL")
    if "trigger_level_v" in settings:
        close("trigger_level_v", args.trigger_level)
    if settings["trigger_source"].upper() != f"CH{args.trigger_channel}":
        raise RuntimeError("Trigger source mismatch")
    actual_slope = settings["trigger_slope"].strip().upper()
    accepted_slopes = (
        ("FALL",) if args.trigger_edge == "falling"
        else ("RIS", "RISE")
    )
    if actual_slope not in accepted_slopes:
        raise RuntimeError(
            f"Trigger slope mismatch: expected {args.trigger_edge}, "
            f"scope reports {settings['trigger_slope']!r}"
        )
    for ch in args.channels:
        close(f"ch{ch}_v_div", args.scales[ch])
        if settings[f"ch{ch}_enabled"].upper() not in ("ON", "1"):
            raise RuntimeError(f"CH{ch} is disabled")


def configure(f, args):
    write(f, "HEADER OFF")
    write(f, "ACQuire:STATE STOP")
    # Inherited Average/Envelope settings can combine different live states.
    write(f, "ACQuire:FASTAcq:STATE OFF")
    write(f, "ACQuire:MODe SAMple")
    write(f, "ACQuire:STOPAfter SEQuence")
    write(f, f"HORizontal:RECOrdlength {args.points}")
    write(f, f"HORizontal:SCALe {args.time_div_us * 1e-6:.9g}")
    # This must follow timebase and record-length writes: the scope may
    # recenter the trigger when either of those settings changes.
    if args.trigger_position_pct is not None:
        write(f, "HORizontal:DELay:MODe OFF")
        write(f, f"HORizontal:POSition {args.trigger_position_pct:.9g}")
    for ch in args.channels:
        write(f, f"SELect:CH{ch} ON")
        write(f, f"CH{ch}:SCAle {args.scales[ch]:.9g}")
    write(f, "TRIGger:A:TYPe EDGE")
    write(f, f"TRIGger:A:EDGE:SOUrce CH{args.trigger_channel}")
    write(f, f"TRIGger:A:EDGE:SLOpe {'FALL' if args.trigger_edge == 'falling' else 'RISE'}")
    write(f, "TRIGger:A:MODe NORM")
    write(f, f"TRIGger:A:LEVel:CH{args.trigger_channel} {args.trigger_level:.9g}")


def atomic_json(path, payload):
    """Publish one complete marker; readers never see a partially written JSON."""
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(temporary, path)


def observed_query(f, command):
    """Bracket a SCPI query on this host's monotonic clock, not the scope clock."""
    before = time.monotonic()
    value = query(f, command).strip().upper()
    after = time.monotonic()
    return {"command": command, "value": value,
            "query_start_monotonic_s": before,
            "query_end_monotonic_s": after}


def acquire(f, timeout, on_armed=None, *, on_progress=None):
    """Acquire one sequence, optionally reporting host-observed trigger states.

    The Tektronix programmer manual defines TRIGGER as acquiring post-trigger
    data (077-0510-05, p. 2-827). In one SAMPLE/SEQUENCE acquisition, a TRIGGER
    query completed before a host command and another TRIGGER query begun after
    it establish that the command interval was inside that same acquisition.
    This is a containment observation, not a synchronized waveform timestamp.
    ACQ:STATE RUN/STOP brackets are diagnostic only: RUN can include waiting
    and completion processing, so they do not bound the final physical sample.

    Without on_progress, preserve the existing acquisition-only polling path.
    All USB queries can block; the caller must retain the child-process watchdog.
    """
    write(f, "ACQuire:STATE STOP")
    write(f, "ACQuire:STOPAfter SEQuence")
    run_begin = time.monotonic()
    write(f, "ACQuire:STATE RUN")
    run_end = time.monotonic()
    # RUN establishes an enabled fresh sequence, not its hardware trigger time.
    first = observed_query(f, "ACQuire:STATE?")
    if first["value"] not in ("1", "RUN", "ON"):
        raise RuntimeError("Scope stopped before first trigger; cannot certify fresh arming")
    observations = [first]
    trigger_observations = []
    last_running = first
    first_stopped = None
    first_trigger = None
    last_trigger = None
    posttrigger_finished = False
    posttrigger_left_trigger = False

    def snapshot():
        return {
            "elapsed_s": time.monotonic() - run_begin,
            "seen_running": True,
            "run_write_begin_monotonic_s": run_begin,
            "run_write_end_monotonic_s": run_end,
            "first_running_observation": first,
            "last_running_observation": last_running,
            "first_stopped_observation": first_stopped,
            "first_trigger_observation": first_trigger,
            "last_trigger_observation": last_trigger,
            "acquisition_state_observations": list(observations),
            "trigger_state_observations": list(trigger_observations),
            "scope_clock_synchronized": False,
            "timing_interpretation": (
                "Host monotonic SCPI query brackets; TRIGGER denotes observed post-trigger "
                "acquisition. READY/ARMED after TRIGGER is retained rather than treated "
                "as automatic rearming; a later second TRIGGER is rejected. ACQ RUN/STOP "
                "does not locate the final physical sample. No microsecond "
                "command-to-waveform synchronization is established."
            ),
        }

    if on_progress is not None:
        on_progress(snapshot())
    if on_armed is not None:
        on_armed()
    while time.monotonic() - run_begin < timeout:
        if on_progress is not None:
            trigger = observed_query(f, "TRIGger:STATE?")
            aliases = {"ARM": "ARMED", "ARMED": "ARMED",
                       "REA": "READY", "READY": "READY",
                       "TRIG": "TRIGGER", "TRIGGER": "TRIGGER",
                       "SAV": "SAVE", "SAVE": "SAVE"}
            original = trigger["value"]
            trigger["raw_value"] = original
            trigger_observations.append(trigger)
            if original not in aliases:
                on_progress(snapshot())
                raise RuntimeError(f"Unexpected NORMAL trigger state: {original!r}")
            trigger["value"] = aliases[original]
            if (first_trigger is not None and
                    trigger["value"] in ("ARMED", "READY")):
                # The reviewed MDO4104B-6 can report READY after TRIGGER while
                # ACQuire:STATE is still completing the finite SEQUENCE.
                # Preserve this observation; only a later return to TRIGGER
                # would indicate an actual resumed/retriggered acquisition.
                posttrigger_left_trigger = True
            if trigger["value"] == "TRIGGER":
                if posttrigger_finished or posttrigger_left_trigger:
                    on_progress(snapshot())
                    raise RuntimeError(
                        "Post-trigger acquisition resumed during one finite sequence")
                if first_trigger is None:
                    first_trigger = trigger
                last_trigger = trigger
            elif trigger["value"] == "SAVE" and first_trigger is not None:
                posttrigger_finished = True
                posttrigger_left_trigger = True
        observation = observed_query(f, "ACQuire:STATE?")
        observations.append(observation)
        state = observation["value"]
        if state in ("1", "RUN", "ON"):
            last_running = observation
        elif state in ("0", "STOP", "OFF"):
            first_stopped = observation
            result = snapshot()
            if on_progress is not None:
                on_progress(result)
            return result
        else:
            if on_progress is not None:
                on_progress(snapshot())
            raise RuntimeError(f"Unexpected acquisition state: {state}")
        if on_progress is not None:
            on_progress(snapshot())
        time.sleep(0.1)
    raise TimeoutError("No completed single-sequence acquisition")


def validate_csv(path, points):
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    if len(rows) != points:
        raise RuntimeError(f"{path}: expected {points} samples, received {len(rows)}")
    times = [float(row["time_s"]) for row in rows]
    if any(b <= a for a, b in zip(times, times[1:])):
        raise RuntimeError("Non-increasing waveform times")
    return {"samples": len(rows), "sample_interval_s": times[1] - times[0],
            "span_s": times[-1] - times[0]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--device", default="auto",
                   help="auto-discover Tektronix USBTMC node; explicit /dev/usbtmcN override")
    p.add_argument("--channels", nargs="+", type=int, default=[1, 2, 3, 4])
    p.add_argument("--scale", action="append", default=[], help="Repeat CH:V_DIV")
    p.add_argument("--time-div-us", type=float, default=200)
    p.add_argument("--points", type=int, default=10000)
    p.add_argument("--trigger-channel", type=int, default=1)
    p.add_argument("--trigger-edge", choices=("rising", "falling"), default="falling")
    p.add_argument("--trigger-level", type=float, default=-10)
    p.add_argument("--trigger-position-pct", type=float, default=None,
                   help="Percent of record before trigger: 0=left, 50=center; "
                        "also disables horizontal delay; omitted=leave position unchanged")
    p.add_argument("--timeout", type=float, default=15)
    p.add_argument("--expected-frequency-hz", type=float, default=None,
                   help="Optional observation-window sanity check; not used to control stimulation")
    p.add_argument("--expected-width-us", type=float, default=None,
                   help="Optional nominal pulse-resolution check")
    p.add_argument("--label", default="scope_capture")
    p.add_argument("--output-root", type=Path, default=Path("data/recordings"))
    p.add_argument("--run", action="store_true", help="Configure scope and acquire; default is dry-run")
    p.add_argument("--run-dir", type=Path, help="Use this exact per-experiment directory")
    p.add_argument("--signal-armed", action="store_true",
                   help="Write an atomic armed.json marker in the run directory after actual arming")
    p.add_argument("--signal-progress", action="store_true",
                   help="Poll TRIGger:STATE? and publish host query brackets in atomic "
                        "acquisition_progress.json and acquisition_complete.json markers")
    p.add_argument("--await-stop-marker", action="store_true",
                   help="Keep completed scope record frozen until session signals Matrix32 STOP")
    a = p.parse_args()
    if not a.channels or len(set(a.channels)) != len(a.channels) or any(ch not in range(1, 5) for ch in a.channels):
        p.error("Select unique channels 1..4")
    if a.trigger_channel not in a.channels:
        p.error("Trigger channel must be selected")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", a.label):
        p.error("Invalid label")
    if not (a.points >= 2 and math.isfinite(a.time_div_us) and a.time_div_us > 0 and a.timeout > 0):
        p.error("Invalid record length/timebase/timeout")
    if a.trigger_position_pct is not None and not (
            math.isfinite(a.trigger_position_pct) and
            0 <= a.trigger_position_pct <= 100):
        p.error("--trigger-position-pct must be between 0 and 100")
    a.scales = {}
    for spec in a.scale:
        try:
            ch_s, value_s = spec.split(":")
            ch, value = int(ch_s), float(value_s)
        except ValueError:
            p.error(f"Invalid scale: {spec}")
        if ch not in a.channels or not math.isfinite(value) or value <= 0:
            p.error(f"Invalid scale: {spec}")
        a.scales[ch] = value
    if set(a.scales) != set(a.channels):
        p.error("Specify --scale CH:V_DIV for every selected channel")
    window = 10 * a.time_div_us * 1e-6
    dt = window / a.points
    if a.expected_frequency_hz is not None:
        if a.expected_frequency_hz <= 0:
            p.error("Expected frequency must be positive")
        if window * a.expected_frequency_hz < 2:
            print("WARNING: window contains fewer than two nominal periods")
    if a.expected_width_us is not None:
        if a.expected_width_us <= 0:
            p.error("Expected width must be positive")
        if a.expected_width_us * 1e-6 / dt < 10:
            print("WARNING: fewer than ten nominal samples per pulse width")
    plan = {"channels": a.channels, "v_div": a.scales, "time_div_us": a.time_div_us,
            "points": a.points, "nominal_window_s": window, "nominal_dt_s": dt,
            "acquisition_mode": "SAMPLE", "acquisition_stopafter": "SEQUENCE",
            "fast_acquisition": "OFF", "signal_progress": a.signal_progress,
            "trigger": [a.trigger_channel, a.trigger_edge, a.trigger_level],
            "trigger_position_pct": a.trigger_position_pct,
            "horizontal_delay_mode_requested": (
                "OFF" if a.trigger_position_pct is not None else None)}
    print(json.dumps(plan, indent=2))
    if not a.run:
        print("DRY RUN: no hardware accessed.")
        return
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_" + a.label
    root = a.run_dir if a.run_dir is not None else a.output_root / run_id
    (root / "waveforms").mkdir(parents=True, exist_ok=False)
    metadata = {"run_id": root.name, "plan": plan, "status": "started", "waveforms": {},
                "started_utc": datetime.now(timezone.utc).isoformat()}
    git = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
    metadata["git_commit"] = git.stdout.strip() if git.returncode == 0 else None
    try:
        node = resolve_scope_device(a.device)
        metadata["scope_device_resolved"] = str(node)
        print(f"Scope USBTMC node: {node}", flush=True)
        with open(node, "r+b", buffering=0) as f:
            identity = verify_scope_identity(query(f, "*IDN?"))
            metadata["scope_identity_preconfiguration"] = identity
            print(f"Verified scope: {identity}", flush=True)
            configure(f, a)
            metadata["scope_before"] = scope_settings(
                f, a.channels, a.trigger_channel,
                horizontal_position=a.trigger_position_pct is not None)
            verify(metadata["scope_before"], a)
            def notify_armed():
                if a.signal_armed:
                    atomic_json(root / "armed.json", {
                        "run_id": root.name, "process_id": os.getpid(),
                        "utc": datetime.now(timezone.utc).isoformat(),
                        "host_monotonic_s": time.monotonic(),
                        "trigger_source": f"CH{a.trigger_channel}",
                        "record_length": a.points})
                print("SCOPE_ARMED", flush=True)

            def notify_progress(report, *, complete=False):
                # Retain evidence in failed metadata even if a later query blocks
                # or fails. No waveform is downloaded by this notification.
                metadata["acquisition"] = report
                complete = complete or report.get("first_stopped_observation") is not None
                payload = {
                    **report, "schema_version": 1, "run_id": root.name,
                    "process_id": os.getpid(),
                    "utc": datetime.now(timezone.utc).isoformat(),
                    "host_monotonic_s": time.monotonic(),
                    "status": "complete" if complete else "acquiring",
                    "scope_settings_before": metadata["scope_before"],
                }
                atomic_json(root / "acquisition_progress.json", payload)
                if complete:
                    atomic_json(root / "acquisition_complete.json", payload)

            metadata["acquisition"] = acquire(
                f, a.timeout, on_armed=notify_armed,
                on_progress=notify_progress if a.signal_progress else None)
            if a.signal_progress:
                # Parent can observe completion while this process keeps the
                # record frozen and waits for STOP/OFF before any CURVE? transfer.
                notify_progress(metadata["acquisition"], complete=True)
            if a.await_stop_marker:
                deadline = time.monotonic() + a.timeout
                while not (root / "stim_stopped.json").exists():
                    if time.monotonic() > deadline:
                        raise TimeoutError("No stop-command marker; transfer not permitted")
                    time.sleep(0.05)
                metadata["stop_marker"] = json.loads((root / "stim_stopped.json").read_text())
                if not metadata["stop_marker"].get("stop_command_completed"):
                    raise RuntimeError("STOP command not completed; refusing waveform download")
            metadata["scope_after"] = scope_settings(
                f, a.channels, a.trigger_channel,
                horizontal_position=a.trigger_position_pct is not None)
            verify(metadata["scope_after"], a)
            for ch in a.channels:
                file = root / "waveforms" / f"ch{ch}.csv"
                report = capture(f, ch, a.points, file)
                metadata["waveforms"][f"CH{ch}"] = {
                    "path": str(file.relative_to(root)), "transfer": report,
                    "timing": validate_csv(file, a.points)}
            metadata["status"] = "captured_unqualified"
    except BaseException as exc:
        metadata["status"] = "failed"
        metadata["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        metadata["finished_utc"] = datetime.now(timezone.utc).isoformat()
        (root / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print("Saved", root)


if __name__ == "__main__":
    main()
