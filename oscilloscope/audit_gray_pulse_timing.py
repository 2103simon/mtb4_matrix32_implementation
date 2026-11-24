"""Offline CH2 Gray-code physical width and timing analysis.

Uses original CH2 waveforms, approved decoded Gray frame IDs and Nano FRAME
telemetry. Does not alter original clock mappings or CH1 pulse approvals.
"""
from __future__ import annotations

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


def geometry(record, frame_log, pulses):
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
    starts.sort(key=lambda x: x["frame"])
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
            "scope_vs_nominal_us": dt_scope_us-steps*FRAME_S*1e6,
            "nano_vs_nominal_us": dt_nano_us-steps*FRAME_S*1e6,
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
    axes[1].set(title="Frame start spacing relative to 100 ms",
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


def run(run_dir, *, threshold=2.):
    root = Path(run_dir).expanduser().resolve()
    meta = json.loads((root/"metadata.json").read_text(encoding="utf-8"))
    nano = read_nano_frames(root/"nano_serial.jsonl")
    reports = []
    for rec in sorted(meta["records"], key=lambda r: int(r["index"])):
        idx = int(rec["index"])
        ch2 = root/rec["channels"]["CH2"]["path"]
        print(f"[GRAY PHYSICAL {idx}] Reading CH2: {ch2}", flush=True)
        pulses, waveform = edges_from_csv(ch2, threshold=threshold)
        widths, spacing, stat = geometry(rec, nano, pulses)
        folder = root/"analysis"/f"record_{idx:05d}"
        folder.mkdir(parents=True, exist_ok=True)
        save_csv(folder/"gray_pulse_widths.csv", widths, [
            "frame", "role", "group", "scope_frame_s", "rise_s",
            "rise_offset_us", "physical_width_us", "nominal_width_us",
            "width_error_us",
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
    out = root/"analysis"
    payload = {
        "schema": "matrix32_gray_physical_pulse_geometry_v1",
        "records": reports,
        "original_clock_maps_untouched": True,
        "source_locked_CH1_approvals_untouched": True,
        "electrical_gpio_event_timestamp_unverified": True,
    }
    atomic_json(out/"gray_pulse_geometry_summary.json", payload)
    lines = [
        "# CH2 Gray-code pulse width and timebase analysis", "",
        "Measured from the original CH2 scope waveform. No approved CH1 "
        "pulse analysis is changed.", "",
        "The Nano firmware uses micros() to schedule D7 switching in "
        "tickGenerator(). It captures EDGE_US with another micros() call "
        "AFTER the rising-edge digitalWrite(). Both use the same firmware "
        "timer but are not simultaneous hardware events.", "",
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
        "their individual 1-ms widths and rise locations relative to "
        "expected 5-ms slots; arbitrary pulse-to-pulse gaps are not "
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
            f"![Pulse width, frame timing and relative timestamp error]"
            f"(record_{idx:05d}/gray_pulse_geometry.png)", "",
            f"![Measured pulse-width error histograms]"
            f"(record_{idx:05d}/gray_width_histograms.png)", "",
        ]
    lines += [
        "## Hardware limitation", "",
        "This cannot verify a truly simultaneous GPIO edge timestamp. "
        "The Nano Every uses an ATmega4809 clock for its CPU/timer and "
        "software-driven D7, while USB buffering and PC timestamps "
        "constitute another timing domain. A future hardware-timer "
        "capture or compare output and shared electrical marker could "
        "provide independent verification.", "",
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
