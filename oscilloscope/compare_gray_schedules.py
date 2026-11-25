"""Compare 10Hz vs 5Hz Nano clock-only runs, no physical stimulation.

Prerequisites:
  python -m oscilloscope.audit_gray_pulse_timing --run-dir BASE
  python -m oscilloscope.audit_gray_pulse_timing --run-dir TEST

Reports compare physical width and frame spacing, Gray/Nano affine RMS,
Nano/PC SYNC quality and offset feasibility. A slow clock may improve
physical waveform geometry while hurting USB SYNC because serial processing
is deferred to idle; these separate effects must not be conflated.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from experiment_analysis.paths import analysis_root

import numpy as np

from oscilloscope.clock_qualification_diagnostics import _frame_pairs
from oscilloscope.align_gray_records import read_nano_frames, fit_affine
from oscilloscope.record_scope import atomic_json


def _load(root):
    root = Path(root).resolve()
    meta = json.loads((root/"metadata.json").read_text())
    report = json.loads((analysis_root(root)/"gray_pulse_geometry_summary.json").read_text())
    frames = read_nano_frames(root/"nano_serial.jsonl")
    clock = root/"epoch_clock_analysis.json"
    epoch = (json.loads(clock.read_text()) if clock.exists() else None)
    return root, meta, report, frames, epoch


def rows_for_run(name, root, meta, report, frames, epoch):
    if meta.get("status") != "complete":
        raise ValueError(f"{name}: incomplete recording")
    nominal = float(meta.get("plan", {}).get("nominal_frame_s", .1))
    if abs(float(report["nominal_frame_s"])-nominal) > 1e-10:
        raise ValueError(f"{name}: geometry nominal period does not match capture plan")
    original = {int(r["record_index"]): r for r in (epoch or {}).get("records", [])}
    geometry = {int(r["record"]): r for r in report["records"]}
    rows = []
    for record in meta["records"]:
        idx = int(record["index"])
        pairs, issue = _frame_pairs(root, record, frames)
        if issue:
            raise ValueError(f"{name} record {idx}: {issue}")
        fit = fit_affine([(p[0], p[1]) for p in pairs])
        g = geometry[idx]
        e = original.get(idx, {})
        host = e.get("fit") or {}
        offset = e.get("offset_constraint") or {}
        rows.append({
            "run": name, "record": idx,
            "nominal_frame_ms": nominal*1000,
            "matched_gray_frames": len(pairs),
            "start_width_std_us": g["width_start_us"]["std"],
            "data_width_std_us": g["width_data_us"]["std"],
            "end_width_std_us": g["width_end_us"]["std"],
            "physical_frame_spacing_std_us":
                g["physical_frame_spacing_error_us"]["std"],
            "nano_frame_spacing_std_us":
                g["nano_frame_spacing_error_us"]["std"],
            "nano_minus_rate_scaled_physical_spacing_std_us":
                g["nano_minus_rate_scaled_scope_spacing_us"]["std"],
            "scope_to_nano_affine_rms_us": fit["edge_fit_rms_us"],
            "nano_to_host_midpoint_rms_us": host.get("rms_residual_us"),
            "usb_offset_interval_width_us": offset.get("intersection_width_us"),
            "usb_offset_feasible": offset.get("feasible"),
            "scope_to_nano_100us_gate_passes": fit["edge_fit_rms_us"] <= 100,
            "unassigned_complete_high_pulses":
                g["unassigned_complete_high_pulses"],
        })
    return rows


def run(baseline, experiment, *, output=None):
    b = _load(baseline)
    e = _load(experiment)
    if not (abs(float(b[1]["plan"].get("nominal_frame_s", .1))-.1) < 1e-9
            and abs(float(e[1]["plan"].get("nominal_frame_s", .1))-.2) < 1e-9):
        raise ValueError("Comparison requires 10Hz baseline and 5Hz candidate")
    rows = rows_for_run("10Hz_original", *b) + rows_for_run("5Hz_deferred", *e)
    out = Path(output).expanduser().resolve() if output else analysis_root(e[0])
    for raw in (b[0], e[0]):
        if out == raw or raw in out.parents:
            raise ValueError("Refusing to write Gray schedule analysis into raw recording")
    out.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0])
    with (out/"gray_schedule_comparison.csv").open("w", newline="",
                                                   encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    metrics = [
        "start_width_std_us", "data_width_std_us",
        "end_width_std_us", "physical_frame_spacing_std_us",
        "nano_frame_spacing_std_us",
        "nano_minus_rate_scaled_physical_spacing_std_us",
        "scope_to_nano_affine_rms_us",
        "nano_to_host_midpoint_rms_us",
        "usb_offset_interval_width_us",
    ]
    agg = {}
    for name in ("10Hz_original", "5Hz_deferred"):
        group = [r for r in rows if r["run"] == name]
        agg[name] = {
            key: float(np.median([float(r[key]) for r in group if r[key] is not None]))
            if any(r[key] is not None for r in group) else None
            for key in metrics
        }
        agg[name]["records"] = len(group)
        agg[name]["scope_to_nano_100us_pass_count"] = sum(
            r["scope_to_nano_100us_gate_passes"] for r in group)
    payload = {
        "schema": "nano_5hz_10hz_schedule_comparison_v1",
        "baseline_run": b[0].name, "candidate_run": e[0].name,
        "record_rows": rows, "median_per_run": agg,
        "causal_offset_not_absolute_calibration": True,
        "different_run_conditions_may_confound_causal_inference": True,
        "physically_unobserved_gaps_not_interpolated": True,
    }
    atomic_json(out/"gray_schedule_comparison.json", payload)
    md = [
        "# Nano Gray-only schedule comparison: 10Hz vs 5Hz", "",
        "Separate finite physical CH2 captures with NO Matrix32 output. "
        "The 5Hz test preserves the 97ms encoded waveform but defers "
        "serial FRAME reports until idle; it also processes serial "
        "commands during idle only. It changes both rate and serial "
        "scheduling, so a gain cannot distinguish which change helped.",
        "",
        "| Metric (median across captures) | 10Hz original | 5Hz deferred |",
        "| --- | ---: | ---: |",
    ]
    for key in metrics:
        md.append(f"| {key} | {agg['10Hz_original'][key]} | "
                  f"{agg['5Hz_deferred'][key]} |")
    md.extend([
        f"| Scope/Nano affine 100us pass count | "
        f"{agg['10Hz_original']['scope_to_nano_100us_pass_count']} | "
        f"{agg['5Hz_deferred']['scope_to_nano_100us_pass_count']} |",
        "", "## Interpretation", "",
        "- If physical width and frame-spacing jitter decrease, slower "
        "or less-interfered generation improved the electrical marker.",
        "- If physical spacing is steady but relative Nano/CH2 "
        "residuals decrease, post-edge timestamping may have improved.",
        "- The Nano and scope have independent clocks. A 5Hz frame "
        "period does not guarantee a more affine mapping between them.",
        "- Deferred serial servicing may INCREASE Nano/PC SYNC RTT and "
        "offset width, even when D7 pulses improve. Report both.",
        "- Comparing entire recordings by replacing physical times with "
        "nominal 100ms/200ms spacing would erase genuine measured "
        "scheduling jitter. Do not silently stretch/squeeze CH1 samples.",
        "- No saved CH1 source review or official clock mapping is changed.",
        "", "[All per-record metrics](gray_schedule_comparison.csv)",
    ])
    (out/"gray_schedule_comparison.md").write_text("\n".join(md)+"\n")
    print("[GRAY A/B] "+str(out/"gray_schedule_comparison.md"))
    return payload


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--baseline", required=True, type=Path)
    p.add_argument("--candidate", required=True, type=Path)
    p.add_argument("--output", type=Path, default=None)
    args = p.parse_args(argv)
    return run(args.baseline, args.candidate, output=args.output)


if __name__ == "__main__":
    main()
