"""Analyze three long Gray scope captures with independent Nano/host SYNC epochs.

Requires the scope acquisition's metadata.json and nano_serial.jsonl.
Rebuilds the physical scope->Nano alignment (one fit per scope record),
then fits Nano->PC monotonic separately using each record's pre/post SYNC
exchanges. No physical sample-gap reconstruction or offset calibration.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from communication.clock_sync import (
    active_clock_fit, nano_us_to_host_ns, read_active_exchanges,
)
from communication.clock_estimator_diagnostics import (
    _clock_intersection, diagnose_active_clock,
)
from align_gray_records import build_alignment, read_nano_frames
from record_scope import atomic_json


def analyze(run_dir, *, max_scope_rms_us=100.0, max_host_rms_us=1000.0):
    root = Path(run_dir)
    meta = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    plan = meta.get("plan", {})
    if not (plan.get("per_record_epochs")
            and plan.get("experiment") == "GRAY_TC_V1"):
        raise ValueError("Expected Gray timecode run with per-record SYNC epochs")
    records = meta.get("records", [])
    if not records:
        raise ValueError("No oscilloscope records in metadata")
    frames = read_nano_frames(root / "nano_serial.jsonl")
    alignment = build_alignment(meta, frames, max_rms_us=max_scope_rms_us)
    exchanges = read_active_exchanges(root / "nano_serial.jsonl")
    by_seq = {entry["seq"]: entry for entry in exchanges}
    if len(by_seq) != len(exchanges):
        raise ValueError("Duplicate Nano SYNC sequence identifiers")
    used = set()
    reports = []

    for record, scope in zip(records, alignment["records"]):
        index = int(record["index"])
        if index != scope["record_index"]:
            raise ValueError("Scope alignment record index mismatch")
        epoch = record.get("clock_epoch")
        if not epoch or epoch.get("epoch_id") != index:
            raise ValueError(f"Record {index}: missing independent clock epoch")
        before_ids = epoch.get("pre_sync_sequence_ids", [])
        after_ids = epoch.get("post_sync_sequence_ids", [])
        ids = before_ids + after_ids
        if len(ids) != len(set(ids)) or any(seq in used for seq in ids):
            raise ValueError(f"Record {index}: overlapping or duplicate SYNC IDs")
        used.update(ids)
        if any(seq not in by_seq for seq in ids):
            raise ValueError(f"Record {index}: SYNC ID missing from serial log")
        pre = [by_seq[x] for x in before_ids]
        post = [by_seq[x] for x in after_ids]
        if not pre or not post:
            raise ValueError(f"Record {index}: both pre and post SYNC batches are needed")
        if max(x["t4_host_ns"] for x in pre) > record["arm_request_monotonic_ns"]:
            raise ValueError(f"Record {index}: pre-SYNC overlaps acquisition")
        if min(x["t1_host_ns"] for x in post) < record["acquisition_completed_monotonic_ns"]:
            raise ValueError(f"Record {index}: post-SYNC overlaps acquisition")
        if any(x["t1_host_ns"] >= post[0]["t1_host_ns"] for x in pre):
            raise ValueError(f"Record {index}: SYNC batches not chronologically ordered")
        observations = sorted(pre + post, key=lambda x: x["t1_host_ns"])
        entry = {
            "record_index": index,
            "epoch_id": index,
            "pre_sync_count": len(pre),
            "post_sync_count": len(post),
            "sync_sequence_ids": ids,
            "status": "unqualified",
            "scope_edge_fit_rms_us": scope["clock_fit"]["edge_fit_rms_us"],
            "nominal_scope_capture_s": (
                scope["nano_time_at_last_sample_s"]
                - scope["nano_time_at_first_sample_s"]
            ),
            "pause_after_transfer": record.get("pause_after_transfer"),
            "warnings": [],
        }
        try:
            fit = active_clock_fit(observations, min_samples=16, min_span_s=10)
            quality = diagnose_active_clock(
                observations, min_samples=8, min_span_s=1,
                bins=6, keep_fraction=0.25
            )
            intersection = _clock_intersection(
                observations, fit["slope_host_ns_per_nano_us"],
                fit["anchor_nano_us"],
                anchor_host_ns=fit["anchor_host_monotonic_ns"],
            )
            entry["fit"] = fit
            entry["diagnostics"] = quality
            entry["offset_constraint"] = intersection
            entry["first_sample_host_monotonic_ns_est"] = round(
                nano_us_to_host_ns(scope["nano_time_at_first_sample_s"]*1e6, fit)
            )
            entry["last_sample_host_monotonic_ns_est"] = round(
                nano_us_to_host_ns(scope["nano_time_at_last_sample_s"]*1e6, fit)
            )
            if not intersection["feasible"]:
                entry["warnings"].append(
                    "No common causal offset at local fitted slope"
                )
            if fit["rms_residual_us"] > max_host_rms_us:
                entry["warnings"].append(
                    f"Local midpoint fit RMS > {max_host_rms_us:.0f} us"
                )
            if quality["absolute_slope_sensitivity_ppm"] > 50:
                entry["warnings"].append(
                    "Local clock drift changes by >50 ppm after RTT selection"
                )
            if entry["warnings"]:
                entry["status"] = "unqualified"
            else:
                entry["status"] = "provisional_local_fit"
        except ValueError as exc:
            entry["warnings"].append(str(exc))
        reports.append(entry)

    result = {
        "schema_version": 1,
        "experiment": "GRAY_PER_RECORD_SYNC_EPOCH_V1",
        "run_id": meta["run_id"],
        "time_domain": "PC monotonic ns, not UTC",
        "scope_to_nano_alignment": {
            "decoded_frames_total": alignment["decoded_frames_total"],
            "scope_record_count": alignment["source_record_count"],
            "all_fit_rms_below_threshold": alignment["all_fit_rms_below_threshold"],
        },
        "records": reports,
        "qualified_provisional_records": sum(
            row["status"] == "provisional_local_fit" for row in reports
        ),
        "host_clock_synchronization_verified": False,
        "physical_sample_gaps_recovered": False,
        "warnings": [
            "Per-record fits reset estimator observations only. Nano firmware "
            "and Gray frame counter are continuous through the pauses.",
            "Four-timestamp software USB estimates may contain asymmetric "
            "transport delays. A provisional fit is NOT an independently "
            "calibrated physical event timestamp.",
        ],
    }
    return alignment, result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_dir", type=Path)
    p.add_argument("--max-scope-rms-us", type=float, default=100.0)
    p.add_argument("--max-host-rms-us", type=float, default=1000.0)
    args = p.parse_args(argv)
    if not (math.isfinite(args.max_scope_rms_us) and args.max_scope_rms_us > 0
            and math.isfinite(args.max_host_rms_us) and args.max_host_rms_us > 0):
        p.error("Fit RMS limits must be finite positive values")
    alignment, report = analyze(
        args.run_dir, max_scope_rms_us=args.max_scope_rms_us,
        max_host_rms_us=args.max_host_rms_us,
    )
    atomic_json(args.run_dir / "gray_alignment.json", alignment)
    output = args.run_dir / "epoch_clock_analysis.json"
    atomic_json(output, report)
    print(f"[EPOCH] {report['run_id']}: {len(report['records'])} records, "
          f"{report['qualified_provisional_records']} provisional local fits")
    for row in report["records"]:
        fit = row.get("fit")
        summary = (f"drift {fit['drift_ppm_nano_to_host']:.1f} ppm, "
                   f"RMS {fit['rms_residual_us']:.1f} us"
                   if fit else "no valid clock fit")
        print(f"[EPOCH {row['record_index']}] {row['status']}, "
              f"{row['pre_sync_count']}+{row['post_sync_count']} SYNC, "
              f"{summary}")
        if row["warnings"]:
            print("    WARN: " + "; ".join(row["warnings"]))
    print(f"[EPOCH] Saved {output}")


if __name__ == "__main__":
    main()
