"""Offline CH2 Gray-code physical width and timing analysis.

Uses original CH2 waveforms, approved decoded Gray frame IDs and Nano FRAME
telemetry. Does not alter original clock mappings or CH1 pulse approvals.
"""
from __future__ import annotations
import sys as _bootstrap_sys
from pathlib import Path as _bootstrap_path
_bootstrap_sys.path.insert(0, str(_bootstrap_path(__file__).resolve().parents[1]))
from experiment_analysis.paths import analysis_root, ensure_derived_destination

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
import math

import numpy as np

from oscilloscope.align_gray_records import read_nano_frames
from oscilloscope.decode_gray_timecode import (
    edges_from_csv, audit_physical_pulses, FRAME_S,
)
from oscilloscope.record_scope import atomic_json


def summarize(values):
    a = np.asarray(values, dtype=float)
    if not len(a):
        return {"n": 0, "median": None, "mean": None, "std": None,
                "p95_abs": None, "min": None, "max": None}
    return {"n": len(a), "median": float(np.median(a)),
            "mean": float(np.mean(a)), "std": float(np.std(a)),
            "p95_abs": float(np.percentile(np.abs(a), 95)),
            "min": float(a.min()), "max": float(a.max())}


def save_csv(path, rows, fields):
    with Path(path).open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def geometry(record, frame_log, pulses, *, frame_s=FRAME_S):
    frames = record["decoded_gray"]["frames"]
    audit = audit_physical_pulses(pulses, frames)
    widths, starts, missed = [], [], Counter()
    for frame, review in zip(frames, audit["frames"]):
        count = int(frame["counter_mod_65536"])
        matches = [v for k, v in frame_log.items() if k % 65536 == count]
        if len(matches) != 1:
            raise ValueError(f"Gray counter {count}: ambiguous Nano log identity")
        nano = matches[0]
        if int(frame["gray_hex"], 16) != int(nano["gray_hex"], 16):
            raise ValueError("Gray waveform code and Nano log disagree")
        start = None
        t0 = float(frame["scope_start_s"])
        for part in review["pulses"]:
            role = part["role"]
            if not part["matched"]:
                missed[role] += 1
                continue
            nominal = 4000 if role == "START" else (
                7000 if role == "END" else 1000)
            measured = part["width_s"]*1e6
            widths.append({
                "frame": int(nano["counter"]),
                "role": role,
                "group": ("DATA" if role.startswith("BIT_") else role),
                "scope_frame_s": t0,
                "rise_s": part["start_s"],
                "rise_offset_us": part["rise_residual_us"],
                "physical_width_us": measured,
                "nominal_width_us": nominal,
                "width_error_us": measured-nominal,
            })
            if role == "START":
                start = float(part["start_s"])
        starts.append({
            "frame": int(nano["counter"]),
            "scope_start_s": start,
            "nano_start_us": int(nano["nano_unwrapped_us"]),
            "overruns": int(nano["overruns"]),
        })
    # Diagnostic only: see whether START/END-anchored phase rescaling
    # predicts the held-out DATA rise positions. This does not modify any
    # waveform time axis or assume that the scope's clock needs repair.
    by_frame = defaultdict(list)
    for row in widths:
        by_frame[row["frame"]].append(row)
    phase_rows = []
    for frame, group in by_frame.items():
        starts_in_frame = [r for r in group if r["role"] == "START"]
        ends_in_frame = [r for r in group if r["role"] == "END"]
        if len(starts_in_frame) != 1 or len(ends_in_frame) != 1:
            continue
        start = starts_in_frame[0]["rise_s"]
        end = ends_in_frame[0]["rise_s"]
        if end <= start:
            continue
        scale = (end-start)/.090
        for row in group:
            if row["group"] != "DATA":
                continue
            bit_index = int(row["role"].split("_")[1])
            ideal_phase = .007 + bit_index*.005
            raw_error_us = (row["rise_s"]-start-ideal_phase)*1e6
            warped_error_us = (
                row["rise_s"]-start-ideal_phase*scale)*1e6
            row["raw_bit_schedule_error_us"] = raw_error_us
            row["phase_rescaled_bit_error_us"] = warped_error_us
            row["frame_start_to_end_scale"] = scale
            phase_rows.append((raw_error_us, warped_error_us))
    starts.sort(key=lambda x: x["frame"])
    # Distances between successive *actually present* HIGH pulses are
    # content-dependent: Gray zero bits create no physical pulse.
    widths.sort(key=lambda r: (r["frame"], r["rise_s"]))
    for i, row in enumerate(widths):
        previous = widths[i-1] if i and widths[i-1]["frame"] == row["frame"] else None
        if previous:
            row["inter_pulse_rise_gap_us"] = (
                row["rise_s"]-previous["rise_s"])*1e6
            row["inter_pulse_low_gap_us"] = (
                row["rise_s"] - previous["rise_s"] -
                previous["physical_width_us"]/1e6)*1e6
            def nominal_phase_us(item):
                if item["role"] == "START":
                    return 0.
                if item["role"] == "END":
                    return 90000.
                return 7000.+5000.*int(item["role"].split("_")[1])
            expected_rise_gap = nominal_phase_us(row)-nominal_phase_us(previous)
            row["nominal_inter_pulse_rise_gap_us"] = expected_rise_gap
            row["inter_pulse_rise_gap_error_us"] = (
                row["inter_pulse_rise_gap_us"]-expected_rise_gap)
            previous_nominal_width = previous["nominal_width_us"]
            row["nominal_low_gap_us"] = expected_rise_gap-previous_nominal_width
            row["inter_pulse_low_gap_error_us"] = (
                row["inter_pulse_low_gap_us"]-row["nominal_low_gap_us"])
        else:
            for key in (
                "inter_pulse_rise_gap_us", "inter_pulse_low_gap_us",
                "nominal_inter_pulse_rise_gap_us",
                "inter_pulse_rise_gap_error_us", "nominal_low_gap_us",
                "inter_pulse_low_gap_error_us",
            ):
                row[key] = None
    spacing = []
    # Build rate-corrected comparison exclusively from physically observed
    # START edges. Never reconstruct unrecorded pulses or cross scope records.
    valid = [r for r in starts if r["scope_start_s"] is not None]
    if len(valid) >= 3:
        x = np.array([r["scope_start_s"] for r in valid], dtype=float)
        y = np.array([r["nano_start_us"]/1e6 for r in valid], dtype=float)
        slope = float(np.polyfit(x-x.mean(), y-y.mean(), 1)[0])
    else:
        slope = None
    for left, right in zip(starts, starts[1:]):
        if left["scope_start_s"] is None or right["scope_start_s"] is None:
            continue
        steps = right["frame"]-left["frame"]
        if steps <= 0:
            raise ValueError("Non-increasing Gray frame count")
        dt_scope_us = (right["scope_start_s"]-left["scope_start_s"])*1e6
        dt_nano_us = right["nano_start_us"]-left["nano_start_us"]
        spacing.append({
            "frame_from": left["frame"], "frame_to": right["frame"],
            "frame_steps": steps, "scope_time_s": right["scope_start_s"],
            "scope_spacing_us": dt_scope_us,
            "nano_spacing_us": dt_nano_us,
            "scope_vs_nominal_us": dt_scope_us-steps*frame_s*1e6,
            "nano_vs_nominal_us": dt_nano_us-steps*frame_s*1e6,
            "nano_minus_rate_scaled_scope_us": (
                dt_nano_us-slope*dt_scope_us
                if slope is not None else None),
            "nano_overruns": right["overruns"],
        })
    roles = defaultdict(list)
    slot_errors = defaultdict(list)
    for w in widths:
        roles[w["group"]].append(w["physical_width_us"])
        slot_errors[w["group"]].append(w["rise_offset_us"])
    one = [r for r in spacing if r["frame_steps"] == 1]
    report = {
        "decoded_frames": len(frames),
        "nominal_frame_s": frame_s,
        "matched_gray_pulses": len(widths),
        "unassigned_complete_high_pulses": audit["unassigned_pulse_count"],
        "all_complete_high_pulses_accounted_for":
            audit["all_complete_high_pulses_accounted_for"],
        "unmatched_roles": dict(missed),
        "width_start_us": summarize(roles["START"]),
        "width_data_us": summarize(roles["DATA"]),
        "width_end_us": summarize(roles["END"]),
        "slot_rise_start_residual_us": summarize(slot_errors["START"]),
        "slot_rise_data_residual_us": summarize(slot_errors["DATA"]),
        "slot_rise_end_residual_us": summarize(slot_errors["END"]),
        "phase_warp_test": {
            "heldout_data_rises": len(phase_rows),
            "raw_data_rise_error_us": summarize([x[0] for x in phase_rows]),
            "start_end_rescaled_data_error_us": summarize(
                [x[1] for x in phase_rows]),
            "model": "START_END_ANCHORS_PREDICT_HELDOUT_BITS",
            "independent_clock_validation": False,
            "raw_waveform_modified": False,
        },
        "physical_inter_pulse_rise_gap_us": summarize([
            r["inter_pulse_rise_gap_us"] for r in widths
            if r["inter_pulse_rise_gap_us"] is not None]),
        "physical_inter_pulse_low_gap_us": summarize([
            r["inter_pulse_low_gap_us"] for r in widths
            if r["inter_pulse_low_gap_us"] is not None]),
        "physical_inter_pulse_low_gap_error_us": summarize([
            r["inter_pulse_low_gap_error_us"] for r in widths
            if r["inter_pulse_low_gap_error_us"] is not None]),
        "physical_inter_pulse_rise_gap_error_us": summarize([
            r["inter_pulse_rise_gap_error_us"] for r in widths
            if r["inter_pulse_rise_gap_error_us"] is not None]),
        "physical_frame_spacing_error_us": summarize(
            [r["scope_vs_nominal_us"] for r in one]),
        "nano_frame_spacing_error_us": summarize(
            [r["nano_vs_nominal_us"] for r in one]),
        "nano_minus_rate_scaled_scope_spacing_us": summarize([
            r["nano_minus_rate_scaled_scope_us"] for r in one
            if r["nano_minus_rate_scaled_scope_us"] is not None
        ]),
        "nano_seconds_per_scope_second": slope,
        "last_nano_timing_overruns": (
            starts[-1]["overruns"] if starts else None),
        "physical_gpio_edge_hardware_timestamped": False,
    }
    return widths, spacing, report


def plot(folder, widths, frames, index):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(3, 1, figsize=(14, 10), layout="constrained")
    for group in ("START", "DATA", "END"):
        sel = [r for r in widths if r["group"] == group]
        if sel:
            axes[0].scatter(
                [r["scope_frame_s"] for r in sel],
                [r["width_error_us"] for r in sel],
                s=8, alpha=.65, label=group)
    axes[0].set(title=f"Record {index}: electrical Gray pulse-width errors",
                ylabel="Measured high width minus nominal (us)")
    one = [r for r in frames if r["frame_steps"] == 1]
    for label, field in [
        ("CH2 physical", "scope_vs_nominal_us"),
        ("Nano EDGE_US", "nano_vs_nominal_us"),
    ]:
        axes[1].plot([r["scope_time_s"] for r in one],
                     [r[field] for r in one], marker=".",
                     markersize=2, lw=.75, label=label)
    axes[1].set(title="Frame start spacing relative to nominal period",
                ylabel="Inter-frame error (us)")
    rate = [r for r in one
            if r["nano_minus_rate_scaled_scope_us"] is not None]
    axes[2].plot([r["scope_time_s"] for r in rate],
                 [r["nano_minus_rate_scaled_scope_us"] for r in rate],
                 marker=".", markersize=2, lw=.8)
    axes[2].axhline(0, color="black", ls="--", alpha=.4)
    axes[2].set(
        title="Nano timestamp spacing minus clock-rate-adjusted CH2 spacing",
        xlabel="Scope-local time (s)", ylabel="Residual (us)")
    for ax in axes:
        ax.grid(alpha=.2)
        if ax.get_legend_handles_labels()[0]:
            ax.legend(fontsize=8)
    fig.savefig(folder/"gray_pulse_geometry.png", dpi=145)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(12, 5), layout="constrained")
    for group in ("START", "DATA", "END"):
        errors = [r["width_error_us"] for r in widths if r["group"] == group]
        if errors:
            ax.hist(errors, bins=40, alpha=.5, label=group)
    ax.set(title=f"Record {index}: CH2 physical pulse width distribution",
           xlabel="Width minus nominal (us)", ylabel="Count")
    ax.legend()
    ax.grid(alpha=.2)
    fig.savefig(folder/"gray_width_histograms.png", dpi=145)
    plt.close(fig)
    fig, axes = plt.subplots(2, 1, figsize=(13, 8),
                             layout="constrained")
    gaps = [r for r in widths
            if r["inter_pulse_rise_gap_error_us"] is not None]
    if gaps:
        axes[0].scatter(
            [r["rise_s"] for r in gaps],
            [r["inter_pulse_rise_gap_error_us"] for r in gaps],
            s=6, alpha=.55, label="Observed rise gap minus encoded expectation")
        axes[1].scatter(
            [r["rise_s"] for r in gaps],
            [r["inter_pulse_low_gap_error_us"] for r in gaps],
            s=6, alpha=.55, label="Observed LOW gap minus encoded expectation")
    axes[0].set(title=f"Record {index}: rise spacing error, Gray aware",
                ylabel="Rise-to-rise gap error (us)")
    axes[1].set(xlabel="Scope-local time (s)",
                ylabel="LOW gap error (us)")
    for ax in axes:
        ax.axhline(0, color="black", ls="--", alpha=.5)
        ax.grid(alpha=.2)
        if ax.get_legend_handles_labels()[0]:
            ax.legend(fontsize=8)
    fig.savefig(folder/"gray_inter_pulse_gap_errors.png", dpi=145)
    plt.close(fig)


def run(run_dir, *, threshold=2., output_dir=None):
    root = Path(run_dir).expanduser().resolve()
    destination = (analysis_root(root) if output_dir is None else
                   ensure_derived_destination(output_dir, root))
    destination.mkdir(parents=True, exist_ok=True)
    meta = json.loads((root/"metadata.json").read_text(encoding="utf-8"))
    nano = read_nano_frames(root/"nano_serial.jsonl")
    # Use the observed INFO response, not a requested mode or an assumption
    # about the firmware version used for a historical recording.
    frame_mode = meta.get("nano_info_fields", {}).get("FRAME_MODE")
    if frame_mode == "10HZ_HW_TCA_BUFFERED":
        firmware_timing = (
            "The recorded Nano mode is `10HZ_HW_TCA_BUFFERED`. D7 is driven "
            "by TCA0-WO1 hardware PWM. The frame-start ISR samples micros() "
            "for EDGE_US and buffers that timestamp until after acquisition. "
            "The ISR timestamp is not an independently latched electrical "
            "edge time."
        )
    else:
        firmware_timing = (
            f"Recorded Nano frame mode: `{frame_mode or 'not recorded'}`. "
            "CH2 supplies the physical threshold crossings; EDGE_US supplies "
            "the separate Nano-reported frame timestamp. Check the firmware "
            "used for this recording before attributing a timing difference "
            "to edge scheduling or timestamp placement."
        )
    frame_s = float(meta.get("plan", {}).get("nominal_frame_s", FRAME_S))
    if not math.isfinite(frame_s) or frame_s < .097:
        raise ValueError("Invalid nominal Gray frame period in metadata")
    reports = []
    for rec in sorted(meta["records"], key=lambda r: int(r["index"])):
        idx = int(rec["index"])
        ch2 = root/rec["channels"]["CH2"]["path"]
        print(f"[GRAY PHYSICAL {idx}] Reading CH2: {ch2}", flush=True)
        pulses, waveform = edges_from_csv(ch2, threshold=threshold)
        widths, spacing, stat = geometry(rec, nano, pulses, frame_s=frame_s)
        folder = destination/f"record_{idx:05d}"
        folder.mkdir(parents=True, exist_ok=True)
        save_csv(folder/"gray_pulse_widths.csv", widths, [
            "frame", "role", "group", "scope_frame_s", "rise_s",
            "rise_offset_us", "physical_width_us", "nominal_width_us",
            "width_error_us", "inter_pulse_rise_gap_us",
            "inter_pulse_low_gap_us",
            "nominal_inter_pulse_rise_gap_us",
            "inter_pulse_rise_gap_error_us", "nominal_low_gap_us",
            "inter_pulse_low_gap_error_us", "raw_bit_schedule_error_us",
            "phase_rescaled_bit_error_us", "frame_start_to_end_scale",
        ])
        save_csv(folder/"gray_frame_spacing_physical_vs_nano.csv", spacing, [
            "frame_from", "frame_to", "frame_steps", "scope_time_s",
            "scope_spacing_us", "nano_spacing_us", "scope_vs_nominal_us",
            "nano_vs_nominal_us", "nano_minus_rate_scaled_scope_us",
            "nano_overruns",
        ])
        plot(folder, widths, spacing, idx)
        stat.update({
            "record": idx, "threshold_v": threshold,
            "waveform": waveform,
        })
        reports.append(stat)
        print(
            f"[GRAY PHYSICAL {idx}] width std (START/DATA/END): "
            f"{stat['width_start_us']['std']}, "
            f"{stat['width_data_us']['std']}, "
            f"{stat['width_end_us']['std']} us; "
            f"physical frame jitter std "
            f"{stat['physical_frame_spacing_error_us']['std']} us; "
            f"relative spacing std "
            f"{stat['nano_minus_rate_scaled_scope_spacing_us']['std']} us",
            flush=True,
        )
    out = destination
    payload = {
        "schema": "matrix32_gray_physical_pulse_geometry_v1",
        "records": reports,
        "recorded_nano_frame_mode": frame_mode,
        "original_clock_maps_untouched": True,
        "source_locked_CH1_approvals_untouched": True,
        "nominal_frame_s": frame_s,
        "electrical_gpio_event_timestamp_unverified": True,
    }
    atomic_json(out/"gray_pulse_geometry_summary.json", payload)
    lines = [
        "# CH2 Gray-code pulse width and timebase analysis", "",
        "Measured from the original CH2 scope waveform. No approved CH1 "
        "pulse analysis is changed.", "",
        firmware_timing + " The analyzed "
        f"nominal frame period is {frame_s*1000:g} ms.", "",
        "| Record | Gray frames | START width median ± std (us) | "
        "DATA width median ± std (us) | END width median ± std (us) | "
        "CH2 frame spacing std (us) | Nano-CH2 rate-corrected spacing "
        "std (us) | Unassigned pulses |",
        "| ---: | ---: | --- | --- | --- | ---: | ---: | ---: |",
    ]
    for r in reports:
        def width(group):
            stat = r[f"width_{group}_us"]
            return (f"{stat['median']:.1f} ± {stat['std']:.1f}"
                    if stat["n"] else "n/a")
        lines.append(
            f"| {r['record']} | {r['decoded_frames']} | "
            f"{width('start')} | {width('data')} | {width('end')} | "
            f"{r['physical_frame_spacing_error_us']['std']} | "
            f"{r['nano_minus_rate_scaled_scope_spacing_us']['std']} | "
            f"{r['unassigned_complete_high_pulses']} |"
        )
    lines += [
        "", "## What does the measurement distinguish?", "",
        "- Consistent pulse widths but variable physical frame spacing "
        "suggest frame scheduling changes, not just data pulse-width jitter.",
        "- Physical and digital frame spacing varying together suggests "
        "shared scheduling/timing variation (not necessarily oscillator drift).",
        "- Stable physical spacing but varying digital EDGE_US spacing "
        "suggests variable timestamp placement or logging timing.",
        "- DATA HIGH pulses are content-dependent. The script measures "
        "their individual 1-ms widths, physical inter-pulse gaps (both "
        "rise-to-rise and low-gap durations), and rise locations relative "
        "to expected 5-ms slots; arbitrary pulse-to-pulse gaps are not "
        "expected to remain constant.",
        "- All widths depend on scope threshold crossing, sampling "
        "resolution and signal slew. A passing Gray decoder is not "
        "proof that physical pulse widths are constant.",
        "",
    ]
    for r in reports:
        idx = r["record"]
        lines += [
            f"## Record {idx}", "",
            f"Unmatched expected roles: {r['unmatched_roles']}; "
            f"unassigned pulses: "
            f"{r['unassigned_complete_high_pulses']}.", "",
            f"Held-out DATA bit rises for phase-rescale test: "
            f"{r['phase_warp_test']['heldout_data_rises']}; "
            f"raw RMS-like error scale (std) "
            f"{r['phase_warp_test']['raw_data_rise_error_us']['std']} us, "
            f"rescaled error scale (std) "
            f"{r['phase_warp_test']['start_end_rescaled_data_error_us']['std']} us.", "",
            f"![Pulse width, frame timing and relative timestamp error]"
            f"(record_{idx:05d}/gray_pulse_geometry.png)", "",
            f"![Measured pulse-width error histograms]"
            f"(record_{idx:05d}/gray_width_histograms.png)", "",
            f"![Physical rise-to-rise and LOW gap timing errors]"
            f"(record_{idx:05d}/gray_inter_pulse_gap_errors.png)", "",
        ]
    lines += [
        "## Diagnostic START/END anchored local time rescaling", "",
        "Each frame offers two physical reference points: its START and "
        "END rising edges (nominally 90 ms apart). As an explicitly "
        "exploratory test, rescale the intermediate Gray phase to this "
        "observed separation and predict the DATA rising edges, which "
        "are NOT used to calculate the scaling. A reduction in residuals "
        "suggests that intra-frame phase stretch may explain part of "
        "the scheduling pattern, not that the oscilloscope timebase "
        "needs alteration. No raw waveform is edited.", "",
        "The per-bit raw and phase-rescaled prediction errors are saved "
        "in gray_pulse_widths.csv. Each frame's two anchors are "
        "training observations; the DATA bit edges are held out. "
        "This does NOT calibrate Nano EDGE_US or PC timing.", "",
        "## Timestamp limitation", "",
        "Physical D7 crossings, Nano FRAME timestamps and USB/PC timestamps "
        "remain distinct observations. Hardware PWM does not by itself "
        "make an ISR micros() reading a hardware-latched edge timestamp. "
        "This geometry audit does not calibrate their absolute offsets "
        "or verify Matrix32 command application latency.", "",
    ]
    report = out/"gray_pulse_geometry_report.md"
    report.write_text("\n".join(lines)+"\n", encoding="utf-8")
    print(f"[GRAY PHYSICAL] Report: {report}")
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--threshold", type=float, default=2.)
    args = parser.parse_args(argv)
    if not math.isfinite(args.threshold):
        parser.error("threshold must be finite")
    return run(args.run_dir, threshold=args.threshold)


if __name__ == "__main__":
    main()
