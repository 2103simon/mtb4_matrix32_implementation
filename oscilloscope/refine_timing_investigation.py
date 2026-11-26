"""Compare interpolators at realistic and missing-frame gaps, and audit USB bounds.

Offline timing only. Uses the original Gray pairs and per-record USB epochs.
Outputs a separate report; never changes the official clock mapping.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from communication.clock_sync import read_active_exchanges
from communication.clock_estimator_diagnostics import (
    causal_slope_interval, causal_host_envelope, _clock_intersection,
)
from experiment_analysis.paths import (
    analysis_root, ensure_derived_destination, raw_run_dir, sha256,
)
from oscilloscope.align_gray_records import read_nano_frames
from oscilloscope.clock_qualification_diagnostics import _frame_pairs
from oscilloscope.evaluate_clock_models import (
    MODEL_NAMES, _xy, blocked_holdout, model_stability, predict,
)
from oscilloscope.record_scope import atomic_json

CANDIDATES = MODEL_NAMES + ("piecewise_linear",)


def compare_models(x, y, host_slope=None):
    """Same held-out folds for every model; no score on training anchors."""
    evaluations = {
        "single_frame": blocked_holdout(
            x, y, models=CANDIDATES, single_frame=True
        )[0],
        "1s": blocked_holdout(x, y, block_s=1., models=CANDIDATES)[0],
        "2s": blocked_holdout(x, y, block_s=2., models=CANDIDATES)[0],
    }
    result = {}
    for name in CANDIDATES:
        stability = model_stability(name, x, y)
        endpoints = predict(name, x, y, np.array([x[0], x[-1]]))
        average_slope = float((endpoints[1] - endpoints[0]) / (x[-1] - x[0]))
        # Multiplication, not addition, of the two clock rates.
        result[name] = {
            "holdouts": {label: scores[name] for label, scores in evaluations.items()},
            "clock_rate_stability": stability,
            "scope_to_nano_average_drift_ppm": (average_slope - 1) * 1e6,
            "combined_average_drift_ppm": (
                (average_slope * host_slope / 1000 - 1) * 1e6
                if host_slope is not None else None
            ),
        }
    return result


def record_sync_exchanges(record, epoch, by_seq):
    """Resolve and verify the exact saved per-record USB observations."""
    idx = int(record["index"])
    ids = epoch["sync_sequence_ids"]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError(f"Record {idx}: missing or repeated SYNC IDs")
    clock_epoch = record["clock_epoch"]
    expected = (clock_epoch["pre_sync_sequence_ids"] +
                clock_epoch["post_sync_sequence_ids"])
    if ids != expected:
        raise ValueError(f"Record {idx}: original SYNC epoch mismatch")
    return [by_seq[seq] for seq in ids]


def inspect_usb(rows, fit, nano_seconds):
    if not fit:
        return {"status": "missing_original_fit"}
    slopes = causal_slope_interval(rows)
    fixed = _clock_intersection(
        rows, fit["slope_host_ns_per_nano_us"], fit["anchor_nano_us"],
        anchor_host_ns=fit["anchor_host_monotonic_ns"],
    )
    first, last = float(nano_seconds[0]) * 1e6, float(nano_seconds[-1]) * 1e6
    bracketed = (min(r["t2_nano_us"] for r in rows) <= first and
                 max(r["t3_nano_us"] for r in rows) >= last)
    return {
        "status": "diagnostic_only",
        "sample_count": len(rows),
        "nano_frames_bracketed_by_sync": bracketed,
        "midpoint_drift_ppm": fit["drift_ppm_nano_to_host"],
        "midpoint_rms_us": fit["rms_residual_us"],
        "transport_rtt_us": (fit.get("latency") or {}).get("transport_rtt_us"),
        "fixed_slope_offset": fixed,
        "causal_slope_interval": slopes,
        "host_envelopes": {
            name: causal_host_envelope(rows, t, fit, slopes)
            for name, t in (("first_frame", first), ("midpoint", (first+last)/2),
                            ("last_frame", last))
        } if bracketed else None,
    }


def fmt(value):
    return "n/a" if value is None else f"{value:.2f}"


def run(run_dir):
    root = raw_run_dir(run_dir)
    sources = [root / p for p in ("metadata.json", "nano_serial.jsonl",
                                   "epoch_clock_analysis.json")]
    fingerprints = {p.name: sha256(p) for p in sources}
    meta = json.loads(sources[0].read_text(encoding="utf-8"))
    original = json.loads(sources[2].read_text(encoding="utf-8"))
    epochs = {int(r["record_index"]): r for r in original["records"]}
    frames = read_nano_frames(sources[1])
    exchanges = read_active_exchanges(sources[1])
    by_seq = {r["seq"]: r for r in exchanges}
    if len(by_seq) != len(exchanges):
        raise ValueError("Duplicate SYNC sequences")
    records = []
    for rec in sorted(meta["records"], key=lambda r: int(r["index"])):
        idx = int(rec["index"])
        pairs, issue = _frame_pairs(root, rec, frames)
        row = {"record_index": idx, "frame_count": len(pairs), "issue": issue}
        if issue is not None:
            records.append(row)
            continue
        x, y = _xy(pairs)
        epoch = epochs[idx]
        selected = record_sync_exchanges(rec, epoch, by_seq)
        fit = epoch.get("fit")
        row["original_status"] = epoch["status"]
        row["usb"] = inspect_usb(selected, fit, y)
        slope = fit["slope_host_ns_per_nano_us"] if fit else None
        row["models"] = compare_models(x, y, slope)
        row["gray_spacing_median_ms"] = float(np.median(np.diff(x))*1000)
        records.append(row)

    payload = {
        "schema": "timing_refinement_v1",
        "recording": root.name,
        "source_sha256": fingerprints,
        "recorded_nano_info": meta.get("nano_info_fields"),
        "recorded_plan": meta.get("plan"),
        "records": records,
        "absolute_offset_verified": False,
        "clock_map_updated": False,
    }
    lines = [
        "# Timing investigation: interpolation and causal USB bounds", "",
        f"Recording: {root.name}", "",
        "No production clock map or frequency report is changed.",
        "Individual withheld frames test interpolation over a missing frame.",
        "The 1 s and 2 s tests assess longer gaps, not ordinary 100 ms interpolation.",
        "Scores are in-sample cross-validation, not independent calibration.", "",
        "| Record | Model | Single frame RMS / p95 (us) | 1 s RMS (us) | 2 s RMS (us) | Max local rate deviation (ppm) | Combined average drift (ppm) |",
        "| ---: | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in records:
        for name, model in row.get("models", {}).items():
            h = model["holdouts"]
            lines.append(
                f"| {row['record_index']} | {name} | "
                f"{fmt(h['single_frame']['rms_us'])} / {fmt(h['single_frame']['p95_abs_us'])} | "
                f"{fmt(h['1s']['rms_us'])} | {fmt(h['2s']['rms_us'])} | "
                f"{fmt(model['clock_rate_stability']['max_local_slope_deviation_from_median_ppm'])} | "
                f"{fmt(model['combined_average_drift_ppm'])} |"
            )
    lines += ["", "## Nano-PC clock uncertainty", "",
              "| Record | Midpoint drift (ppm) | Feasible drift min/max (ppm) | Fixed-slope offset width (us) | Joint conservative width at first/mid/last frame (us) |",
              "| ---: | ---: | ---: | ---: | ---: |"]
    for row in records:
        usb = row.get("usb", {})
        s = usb.get("causal_slope_interval", {})
        fixed = usb.get("fixed_slope_offset", {})
        e = usb.get("host_envelopes") or {}
        widths = " / ".join(fmt((e.get(k) or {}).get("width_us"))
                            for k in ("first_frame", "midpoint", "last_frame"))
        lines.append(
            f"| {row['record_index']} | {fmt(usb.get('midpoint_drift_ppm'))} | "
            f"{fmt(s.get('drift_lower_ppm'))} / {fmt(s.get('drift_upper_ppm'))} | "
            f"{fmt(fixed.get('intersection_width_us'))} | {widths} |"
        )
    lines += ["", "## Interpretation", "",
              "- A piecewise map passes through every training anchor by construction; only withheld errors are informative.",
              "- All seven models use the same folds; counts and maximum errors are in the JSON.",
              "- Combined average drift is a product of estimated clock rates, not a measurement of instantaneous scope-PC accuracy.",
              "- USB feasible slopes assume one affine clock and nonnegative software transport delays; the host envelopes are conservative and exclude Gray/GPIO timestamp error.",
              "- No model resolves unknown constant USB asymmetry or independently measures physical command application."]
    for row in records:
        if row["issue"]:
            lines.append(f"- Record {row['record_index']}: {row['issue']}")
        elif row["usb"].get("nano_frames_bracketed_by_sync") is False:
            lines.append(f"- Record {row['record_index']}: Gray and SYNC time domains not bracketed; inspect rollover or missing observations.")
    if fingerprints != {p.name: sha256(p) for p in sources}:
        raise RuntimeError("Source clock evidence changed during analysis")
    dest = ensure_derived_destination(
        analysis_root(root, legacy_ok=False) / "timing_refinement", root
    )
    dest.mkdir(parents=True, exist_ok=False)
    atomic_json(dest / "timing_refinement_summary.json", payload)
    report = dest / "timing_refinement_report.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[TIMING] Report: {report}")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    return run(parser.parse_args(argv).run_dir)


if __name__ == "__main__":
    main()
