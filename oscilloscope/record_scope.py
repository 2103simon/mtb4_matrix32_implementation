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

# Support both direct execution (`python oscilloscope/record_scope.py`)
# and importing as a package (`from oscilloscope.record_scope import ...`).
# The latter is used by offline analysis and unittest discovery.
if __package__:
    from .tek_mdo_usb import capture, query, write
    from .scope_device import resolve_scope_device, verify_scope_identity
else:
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
        # Read-only provenance for voltage/headroom review. The operator still
        # verifies the physical probe, grounding, ratings and calibration.
        # Tektronix programmer manual 077-0510-05, vertical command group.
        for name, command in (
                ("probe_gain", "PROBe:GAIN"), ("coupling", "COUPling"),
                ("termination_ohm", "TERmination"), ("offset_v", "OFFSet"),
                ("position_div", "POSition"), ("inverted", "INVert")):
            result[f"ch{ch}_{name}"] = query(f, f"CH{ch}:{command}?").strip()
        gain = float(result[f"ch{ch}_probe_gain"])
        if not math.isfinite(gain) or gain <= 0:
            raise RuntimeError(f"CH{ch}: invalid probe gain readback {gain}")
        # Gain is output/input (0.1 for a 10x probe). WFMPRE already applies
        # this scale; do not multiply the downloaded voltages by it again.
        result[f"ch{ch}_probe_attenuation"] = 1.0 / gain
    return result


# Compatibility with SCPI values rounded to five significant decimal digits.
# This is a settings-consistency tolerance, not a physical accuracy claim.
# Equivalent spellings (500000 and 5E5) must have the same acceptance bound.
NUMERIC_READBACK_REL_TOL = 5e-5


def numeric_readback_tolerance(token):
    """Small format-independent numeric compatibility bound, plus float slack.

    A short SCPI spelling does not imply an uncertainty interval around its
    last printed digit. Preserve original tokens as provenance only; never
    weaken validation when trailing zeros are omitted.
    """
    try:
        number = float(token)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"Invalid numeric scope readback: {token!r}") from exc
    if not math.isfinite(number):
        raise RuntimeError(f"Non-finite numeric scope readback: {token!r}")
    return max(abs(number) * NUMERIC_READBACK_REL_TOL, 8 * math.ulp(number))


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
    actual_points = float(settings["record_length"])
    if actual_points != args.points:
        raise RuntimeError(
            f"Scope record length mismatch: requested {args.points:,} points, "
            f"scope reports {settings['record_length']}. Select a supported record length; "
            "acquisition was not started."
        )
    expected_rate = args.points / (10 * args.time_div_us * 1e-6)
    actual_rate = float(settings["sample_rate"])
    rate_tolerance = numeric_readback_tolerance(settings["sample_rate"])
    if (not math.isfinite(actual_rate) or actual_rate <= 0 or
            not math.isclose(actual_rate, expected_rate, rel_tol=0,
                             abs_tol=rate_tolerance + 8 * math.ulp(expected_rate))):
        raise RuntimeError(
            f"Sample rate mismatch: expected {expected_rate:g} samples/s for "
            f"{args.points:,} samples over {10 * args.time_div_us * 1e-6:g}s, "
            f"scope reports {settings['sample_rate']}"
        )
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


def acquire(f, timeout, on_armed=None, *, on_progress=None, on_run_written=None):
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
    on_run_written is an optional host timer hook immediately after the RUN
    write, before any query. It does not certify a hardware trigger or replace
    the existing verified-running on_armed callback.
    """
    write(f, "ACQuire:STATE STOP")
    write(f, "ACQuire:STOPAfter SEQuence")
    run_begin = time.monotonic()
    write(f, "ACQuire:STATE RUN")
    run_end = time.monotonic()
    if on_run_written is not None:
        on_run_written({"run_write_begin_monotonic_s": run_begin,
                        "run_write_end_monotonic_s": run_end})
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
    last = snapshot()
    if on_progress is not None:
        on_progress(last)
    last_state = observations[-1]["value"] if observations else "UNKNOWN"
    trigger_state = (
        trigger_observations[-1]["value"] if trigger_observations else "NOT_QUERIED"
    )
    raise TimeoutError(
        f"No completed single-sequence acquisition after {last['elapsed_s']:.2f}s; "
        f"last ACQuire:STATE={last_state}, TRIGger:STATE={trigger_state}. "
        "The instrument may be waiting for a trigger or processing a long record. "
        "Review the per-record acquisition observations."
    )


def validate_csv(path, points):
    """Validate original samples in bounded memory, including their time grid."""
    if points < 2:
        raise ValueError("At least two waveform samples are required")
    count = 0
    first = previous = first_interval = None
    min_interval = float("inf")
    max_interval = 0.0
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        if not {"time_s", "voltage_v"}.issubset(reader.fieldnames or []):
            raise RuntimeError(f"{path}: waveform needs time_s and voltage_v columns")
        for row in reader:
            timestamp = float(row["time_s"])
            voltage = float(row["voltage_v"])
            if not math.isfinite(timestamp) or not math.isfinite(voltage):
                raise RuntimeError(f"{path}: non-finite waveform sample {count + 1}")
            if previous is None:
                first = timestamp
            else:
                interval = timestamp - previous
                if interval <= 0:
                    raise RuntimeError("Non-increasing waveform times")
                if first_interval is None:
                    first_interval = interval
                min_interval = min(min_interval, interval)
                max_interval = max(max_interval, interval)
            previous = timestamp
            count += 1
    if count != points:
        raise RuntimeError(f"{path}: expected {points} samples, received {count}")
    span = previous - first
    mean_interval = span / (count - 1)
    # The CSV writes xzero + (i - ptoff) * xincr in double precision.
    # Allow arithmetic roundoff, not missing samples or an irregular grid.
    rounding = 16 * max(math.ulp(first), math.ulp(previous),
                        math.ulp(span), math.ulp(mean_interval))
    if max(abs(min_interval - mean_interval),
           abs(max_interval - mean_interval)) > rounding:
        raise RuntimeError(f"{path}: non-uniform waveform time grid")
    return {"samples": count, "sample_interval_s": first_interval,
            "mean_sample_interval_s": mean_interval,
            "span_s": span, "first_time_s": first, "last_time_s": previous,
            "min_sample_interval_s": min_interval,
            "max_sample_interval_s": max_interval}


def verify_waveform_timing(timing, settings, args, transfer=None):
    """Check actual CSV duration against the requested finite scope record.

    The nominal window is N*dt; first-to-last sample span is (N-1)*dt.
    Tolerance covers numeric formatting and float arithmetic, never a fitted
    cross-device clock residual or physical oscillator calibration.
    """
    if timing["samples"] != args.points:
        raise RuntimeError("Waveform sample count differs from the requested record")
    window = 10 * args.time_div_us * 1e-6
    expected_interval = window / args.points
    rate = float(settings["sample_rate"])
    rate_tolerance = numeric_readback_tolerance(settings["sample_rate"])
    if not math.isfinite(rate) or rate <= rate_tolerance:
        raise RuntimeError("Invalid sample rate for waveform timing verification")
    interval_tolerance = max(
        abs(1 / (rate - rate_tolerance) - 1 / rate),
        abs(1 / (rate + rate_tolerance) - 1 / rate))
    if transfer is not None:
        preamble = transfer["waveform_preamble"]
        xincr = preamble["xincr_s"]
        interval_tolerance += numeric_readback_tolerance(
            preamble.get("xincr_scpi", str(xincr)))
        if not math.isfinite(xincr) or xincr <= 0:
            raise RuntimeError("Invalid waveform preamble sample interval")
    rounding = 16 * max(math.ulp(timing["first_time_s"]),
                        math.ulp(timing["last_time_s"]),
                        math.ulp(window))
    expected_span = (args.points - 1) * expected_interval
    span_tolerance = (args.points - 1) * interval_tolerance + rounding
    if (not math.isfinite(timing["span_s"]) or
            abs(timing["span_s"] - expected_span) > span_tolerance):
        raise RuntimeError(
            f"Waveform duration mismatch: expected first-to-last span "
            f"{expected_span:g}s for a {window:g}s record, "
            f"received {timing['span_s']:g}s"
        )
    if transfer is not None:
        actual_interval = timing["span_s"] / (args.points - 1)
        if abs(actual_interval - xincr) > rounding:
            raise RuntimeError("CSV time interval differs from waveform preamble")
    return {"nominal_window_s": window,
            "nominal_sample_interval_s": expected_interval,
            "expected_first_to_last_span_s": expected_span,
            "numeric_span_tolerance_s": span_tolerance,
            "settings_consistent": True,
            "physical_clock_calibration_verified": False}


def verify_waveform_scaling(transfer, settings):
    """Compare preamble scaling with channel readback, without altering data."""
    preamble = transfer["waveform_preamble"]
    source = preamble["source"]
    if not re.fullmatch(r"CH[1-4]", source):
        raise RuntimeError(f"Unexpected waveform source: {source!r}")
    if preamble["voltage_units"] not in ("V", "VOLTS"):
        raise RuntimeError("Waveform is not expressed in volts")
    ch = source[2:]
    scale_token = settings[f"ch{ch}_v_div"]
    scale = float(scale_token)
    ymult = float(preamble["ymult_v_per_code"])
    # Tektronix 077-0510-05, WFMOutpre:YOFF (pp.2-860/861): 25
    # digitizing levels/div for one-byte data, 6400 for two-byte data.
    width = preamble["bytes_per_sample"]
    if width not in (1, 2):
        raise RuntimeError(f"Unsupported waveform byte width: {width}")
    levels_per_div = 25 if width == 1 else 6400
    preamble_scale = abs(ymult) * levels_per_div
    tolerance = numeric_readback_tolerance(scale_token)
    tolerance += levels_per_div * numeric_readback_tolerance(
        preamble.get("ymult_scpi", str(ymult)))
    if (not math.isfinite(scale) or scale <= 0 or
            not math.isfinite(preamble_scale) or preamble_scale <= 0 or
            abs(preamble_scale - scale) > tolerance):
        raise RuntimeError(
            f"{source} waveform scale mismatch: preamble gives "
            f"{preamble_scale:g} V/div, channel readback is {scale:g} V/div"
        )
    gain = float(settings[f"ch{ch}_probe_gain"])
    if not math.isfinite(gain) or gain <= 0:
        raise RuntimeError(f"{source} invalid probe gain readback")
    return {"source": source, "scope_v_div": scale,
            "preamble_v_div": preamble_scale,
            "scope_reported_probe_gain": gain,
            "scope_reported_probe_attenuation": 1.0 / gain,
            "settings_consistent": True,
            "physical_probe_calibration_verified": False}


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
                channel_record = {"path": str(file.relative_to(root)), "transfer": report}
                metadata["waveforms"][f"CH{ch}"] = channel_record
                timing = validate_csv(file, a.points)
                channel_record["timing"] = timing
                timing["verification"] = verify_waveform_timing(
                    timing, metadata["scope_after"], a, report)
                channel_record["scaling"] = verify_waveform_scaling(
                    report, metadata["scope_after"])
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
