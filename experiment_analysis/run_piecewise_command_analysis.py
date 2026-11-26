"""Exploratory MCU/Matrix32 frequency matching using the timing refinement.

Reads raw evidence in place and reuses signed pulse reviews. No official report,
clock map or previously generated analysis is changed.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path

import numpy as np

from communication.clock_sync import read_active_exchanges
from experiment_analysis.paths import (
    analysis_root, ensure_derived_destination, raw_run_dir, sha256,
)
from experiment_analysis.offset_robust_command_matching import (
    INTERVAL_COLUMNS, _csv, analyze_record, load_command_inputs,
    plot_command_agreement, plot_tracking,
)
from oscilloscope.align_gray_records import read_nano_frames
from oscilloscope.refine_timing_investigation import record_sync_exchanges
from oscilloscope.record_scope import atomic_json

TARGET_FIELDS = {
    "mcu_assessment": "controller_target_assessment",
    "mcu_requested_hz_if_stable": "controller_target_hz_if_stable",
    "mcu_error_hz_if_stable": "controller_target_error_hz_if_stable",
}
COLUMNS = [TARGET_FIELDS.get(k, k) for k in INTERVAL_COLUMNS] + [
    "raw_mcu_assessment", "raw_mcu_rate_hz_if_stable",
    "start_host_lower_ns", "start_host_upper_ns",
    "end_host_lower_ns", "end_host_upper_ns",
]


def agreement_statistics(rows, source):
    stable = [r for r in rows if r.get(f"{source}_assessment") ==
              "STABLE_ACROSS_TESTED_OFFSETS"]
    errors = [abs(r[f"{source}_error_hz_if_stable"]) for r in stable]
    return {
        "stable_intervals": len(stable),
        "total_approved_intervals": len(rows),
        "stable_fraction": len(stable)/len(rows) if rows else None,
        "assessment_counts": dict(Counter(r.get(f"{source}_assessment") for r in rows)),
        "median_abs_error_hz": float(np.median(errors)) if errors else None,
        "p95_abs_error_hz": float(np.percentile(errors, 95)) if errors else None,
        "max_abs_error_hz": float(max(errors)) if errors else None,
    }


def run(run_dir, *, scale=1.):
    if not math.isfinite(scale) or not 0 <= scale <= 10:
        raise ValueError("Invalid Gray timing sensitivity scale")
    root = raw_run_dir(run_dir)
    analysis = analysis_root(root, legacy_ok=False)
    refinement_path = analysis/"timing_refinement"/"timing_refinement_summary.json"
    ref = json.loads(refinement_path.read_text(encoding="utf-8"))
    if ref["schema"] != "timing_refinement_v1" or ref["recording"] != root.name:
        raise ValueError("Timing refinement does not identify this recording")
    sources = [root/name for name in (
        "metadata.json", "nano_serial.jsonl", "epoch_clock_analysis.json")]
    fingerprints = {p.name: sha256(p) for p in sources}
    if fingerprints != ref["source_sha256"]:
        raise ValueError("Timing refinement source hashes do not match")
    meta = json.loads(sources[0].read_text(encoding="utf-8"))
    epochs = {int(r["record_index"]): r for r in
              json.loads(sources[2].read_text(encoding="utf-8"))["records"]}
    refined = {int(r["record_index"]): r for r in ref["records"]}
    exchanges = read_active_exchanges(sources[1])
    by_seq = {r["seq"]: r for r in exchanges}
    if len(by_seq) != len(exchanges):
        raise ValueError("Duplicate USB SYNC IDs")
    nano_frames = read_nano_frames(sources[1])
    mcu, ack, taxel, encoding = load_command_inputs(root)
    destination = ensure_derived_destination(
        analysis/"piecewise_command_analysis_integer", root)
    if destination.exists():
        raise FileExistsError(f"Preserving existing analysis: {destination}")
    summaries, all_rows = [], []
    for record in meta["records"]:
        idx = int(record["index"])
        epoch = epochs[idx]
        sync = record_sync_exchanges(record, epoch, by_seq)
        summary, rows, _ = analyze_record(
            root=root, record=record, epoch=epoch, cv={}, mcu=mcu,
            states=ack, taxel=taxel, encoding=encoding, scale=scale,
            refinement=refined[idx], exchanges=sync, nano_frames=nano_frames,
            integer_targets=True)
        if len(rows) != summary["approved_intervals"]:
            raise ValueError(f"Record {idx}: incomplete interval analysis: {summary.get('reason')}")
        for row in rows:
            for old, new in TARGET_FIELDS.items():
                if old in row:
                    row[new] = row.pop(old)
        for old in ("mcu_assessment_counts",
                    "mcu_stable_median_absolute_frequency_difference_hz"):
            if old in summary:
                summary["controller_target" + old[3:]] = summary.pop(old)
        summary["controller_target"] = agreement_statistics(rows, "controller_target")
        summary["ack"] = agreement_statistics(rows, "ack")
        summary["raw_mcu_assessment_counts"] = dict(Counter(
            r["raw_mcu_assessment"] for r in rows))
        summaries.append(summary)
        all_rows.extend(rows)
    payload = {
        "schema": "matrix32_piecewise_command_association_integer_v2",
        "controller_target_policy": "run_tactile_stimulation.requested_frequency",
        "source_run": root.name,
        "source_sha256": fingerprints,
        "timing_refinement_sha256": sha256(refinement_path),
        "taxel": taxel, "encoding": encoding,
        "gray_p95_sensitivity_scale": scale,
        "records": summaries,
        "total_approved_intervals": len(all_rows),
        "controller_target": agreement_statistics(all_rows, "controller_target"),
        "raw_mcu_assessment_counts": dict(Counter(r["raw_mcu_assessment"] for r in all_rows)),
        "ack": agreement_statistics(all_rows, "ack"),
        "both_stable_intervals": sum(bool(r.get("all_scenarios_agree")) for r in all_rows),
        "absolute_alignment_calibrated": False,
        "physical_apply_timestamp_validated": False,
        "original_clock_maps_unchanged": True,
        "original_frequency_reports_unchanged": True,
    }
    both = [r for r in all_rows if r.get("all_scenarios_agree")]
    payload["same_interval_comparison"] = {
        source: agreement_statistics(both, source)
        for source in ("controller_target", "ack")
    }
    payload["stable_mute_target_intervals"] = sum(
        r.get("controller_target_assessment") == "STABLE_MUTE_TARGET" for r in all_rows)
    # Physical frequency always remains 1 / raw scope-clock onset period.
    # Only event attribution uses the piecewise clock mapping.
    destination.mkdir(parents=True, exist_ok=False)
    _csv(destination/"all_intervals.csv", all_rows, COLUMNS)
    lines = [
        "# Exploratory piecewise timing: integer controller targets and Matrix32 output", "",
        f"Recording: {root.name}; taxel {taxel}; encoding {encoding}.", "",
        "**Not a calibrated timing or physical application-latency report.**",
        "Approved CH1 periods are unchanged; the piecewise clock map is used only for attribution.",
        "The candidate exceeds the previous local-rate stability screen.",
        "The USB envelope includes all feasible affine rates conservatively,",
        f"expanded by {scale:g} times the largest 1-frame, 1 s or 2 s held-out Gray p95.",
        "That margin is heuristic; firmware timestamp error, non-affine USB drift",
        "and physical Matrix32 application latency are not bounded.",
        "Controller targets are reconstructed from logged MCU rates with the live requested_frequency() function.",
        "Raw MCU rates are preserved as diagnostics, not transmitted SF commands.",
        "Target stability is checked after quantization. Zero targets mean mute, not SF=0;",
        "their physical pulse intervals are counted separately and excluded from positive-frequency errors.",
        "Reconstructed targets use PC-domain MCU snapshot timestamps and are not evidence of actual transmission.",
        "ACK associations require a stable acknowledged state and prior-cycle guard.",
        "Intervals outside supported Gray anchors or crossing gaps >2 s are unclassified.", "",
        "| Source | Stable intervals / all | Median abs error (Hz) | p95 abs error (Hz) | Max abs error (Hz) |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    def fmt(v):
        return "n/a" if v is None else f"{v:.3f}"
    for source in ("controller_target", "ack"):
        item = payload[source]
        lines.append(
            f"| {source.upper()} | {item['stable_intervals']} / {len(all_rows)} | "
            f"{fmt(item['median_abs_error_hz'])} | {fmt(item['p95_abs_error_hz'])} | "
            f"{fmt(item['max_abs_error_hz'])} |"
        )
    lines += ["", f"Both stable: {payload['both_stable_intervals']} / {len(all_rows)}.",
              "Stable refers to attribution, not agreement or proof of command execution.",
              "Errors describe selected subsets only.",
              f"Physical intervals overlapping stable mute targets: {payload['stable_mute_target_intervals']}.",
              "These are not by themselves proof of a mute failure.", "",
              "## Same-interval comparison", "",
              "| Source | Intervals | Median abs error (Hz) | p95 abs error (Hz) | Max abs error (Hz) |",
              "| --- | ---: | ---: | ---: | ---: |"]
    for source, item in payload["same_interval_comparison"].items():
        lines.append(f"| {source} | {item['stable_intervals']} | {fmt(item['median_abs_error_hz'])} | {fmt(item['p95_abs_error_hz'])} | {fmt(item['max_abs_error_hz'])} |")
    for summary in summaries:
        idx = summary["record"]
        folder = destination/f"record_{idx:05d}"
        folder.mkdir()
        rows = [r for r in all_rows if r["record"] == idx]
        plot_tracking(folder/"tracking.png", rows, index=idx)
        for source in ("controller_target", "ack"):
            plot_command_agreement(folder/f"{source}_agreement.png", rows,
                                   index=idx, source=source)
        lines += [
            f"## Record {idx}", "",
            f"Approved intervals: {len(rows)}.",
            f"Maximum tested host envelope width (ms): {fmt(summary['offset_scenarios']['max_tested_host_envelope_width_ms'])}.",
            f"Gray p95 (us): {fmt(summary['clock_holdout_p95_us'])}.",
            f"Controller target coverage: {summary['controller_target']['assessment_counts']}.",
            f"Raw MCU rate coverage: {summary['raw_mcu_assessment_counts']}.",
            f"ACK coverage: {summary['ack']['assessment_counts']}.", "",
            f"![Integer target agreement](record_{idx:05d}/controller_target_agreement.png)", "",
            f"![ACK agreement](record_{idx:05d}/ack_agreement.png)", "",
            f"![Physical interval classification](record_{idx:05d}/tracking.png)", "",
        ]
    atomic_json(destination/"summary.json", payload)
    report = destination/"report.md"
    report.write_text("\n".join(lines)+"\n", encoding="utf-8")
    print(f"[PIECEWISE] {report}")
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--gray-error-scale", type=float, default=1.)
    args = parser.parse_args(argv)
    return run(args.run_dir, scale=args.gray_error_scale)


if __name__ == "__main__":
    main()
