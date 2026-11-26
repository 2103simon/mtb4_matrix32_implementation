"""Exploratory resistor-branch charge audit using source-locked pulse candidates.

No new pulse detector, waveform copies, approval changes or hardware actions.
Charge is baseline-subtracted voltage integrated over declared finite windows,
not total stimulator charge. Storage-rail checks alone do not prove analog
headroom, correct probing, or electrical safety.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path

import numpy as np

from experiment_analysis.paths import (
    analysis_root, ensure_derived_destination, raw_run_dir, sha256,
)
from oscilloscope.pulse_review_workflow import csv_rows, write_csv
from oscilloscope.record_scope import atomic_json

PRE_S = .0001
BASELINE_START_S = .0005
POST_WINDOWS_S = (.00055, .001, .0015)
PRIMARY_POST_S = .001
RAIL = 32766


def integral(t, v, resistance):
    # Integrate each linear sample segment exactly, splitting sign changes.
    dt = np.diff(t)
    a, b = v[:-1], v[1:]
    crossing = a*b < 0
    fraction = np.zeros_like(dt)
    fraction[crossing] = -a[crossing]/(b[crossing]-a[crossing])
    negative = dt*(np.minimum(a, 0)+np.minimum(b, 0))/2
    positive = dt*(np.maximum(a, 0)+np.maximum(b, 0))/2
    neg_first = crossing & (a < 0)
    pos_first = crossing & (a > 0)
    negative[neg_first] *= fraction[neg_first]
    positive[neg_first] *= 1-fraction[neg_first]
    negative[pos_first] *= 1-fraction[pos_first]
    positive[pos_first] *= fraction[pos_first]
    neg, pos = float(negative.sum()/resistance*1e9), float(positive.sum()/resistance*1e9)
    return {"negative_charge_nc": neg, "positive_charge_nc": pos,
            "net_charge_nc": neg+pos}


def measure(t, v, raw, onset, post_s, resistance, *, neighbor_times=(), quiet_limit_v=1.):
    start, end = onset-PRE_S, onset+post_s
    base_start = onset-BASELINE_START_S
    row = {"window_pre_us": PRE_S*1e6, "window_post_us": post_s*1e6,
           "status": "OK"}
    if base_start < t[0] or end > t[-1]:
        return {**row, "status": "BOUNDARY"}
    # Overlapping candidate or baseline windows are not isolated events.
    if any(base_start <= other <= end+PRE_S for other in neighbor_times):
        return {**row, "status": "NEIGHBOR_OVERLAP"}
    b0, b1 = np.searchsorted(t, [base_start, start], side="left")
    lo, hi = np.searchsorted(t, [start, end], side="left")
    if b1-b0 < 3 or hi-lo < 3 or lo == 0 or hi == len(t):
        return {**row, "status": "INSUFFICIENT_SAMPLES"}
    if np.any(np.abs(raw[b0:hi+1]) >= RAIL):
        return {**row, "status": "STORAGE_RAIL"}
    baseline = float(np.median(v[b0:b1]))
    baseline_span = float(np.max(np.abs(v[b0:b1]-baseline)))
    if baseline_span > quiet_limit_v:
        return {**row, "status": "NONQUIET_BASELINE", "baseline_v": baseline,
                "baseline_max_deviation_v": baseline_span}
    # Interpolate exact integration endpoints; do not resample the interior.
    interior = (t[lo:hi] > start) & (t[lo:hi] < end)
    x = np.r_[start, t[lo:hi][interior], end]
    y = np.r_[np.interp(start, t[lo-1:lo+1], v[lo-1:lo+1]),
              v[lo:hi][interior],
              np.interp(end, t[hi-1:hi+1], v[hi-1:hi+1])]-baseline
    # Window ending outside quiet band may truncate a recovery tail.
    status = "END_NOT_QUIET" if abs(y[-1]) > quiet_limit_v else "OK"
    return {**row, "status": status, "baseline_v": baseline,
            "baseline_max_deviation_v": baseline_span,
            "negative_peak_v": float(y.min()), "positive_peak_v": float(y.max()),
            "negative_excursion_duration_us": float(np.sum(
                np.diff(x)*((y[:-1] < -quiet_limit_v).astype(float)
                            +(y[1:] < -quiet_limit_v).astype(float))/2)*1e6),
            "end_relative_v": float(y[-1]),
            **integral(x, y, resistance)}


def load_candidates(review, waveform, record_index):
    manifest = json.loads((review/"review_manifest.json").read_text())
    if (manifest["schema"] != "matrix32_pulse_review_v1"
            or manifest["record_index"] != record_index
            or manifest["source_ch1_sha256"] != sha256(waveform)):
        raise ValueError("Pulse review does not identify original waveform")
    for name, digest in manifest["source_tables_sha256"].items():
        if sha256(review/name) != digest:
            raise ValueError("Source detector table changed: "+name)
    events = list(manifest["candidates"])
    # Sensitivity-only candidates are reported separately; duplicates near
    # primary candidates are deliberately not added as distinct events.
    primary = [float(e["time_s"]) for e in events]
    for q in csv_rows(review/"threshold_sensitive_onsets.csv"):
        if q["source"] == "alternative_threshold_only":
            when = float(q["time_s"])
            if not any(abs(when-p) <= .0001 for p in primary):
                events.append({"id": q["id"], "kind": "threshold_only",
                               "time_s": when, "detector_status": q["status"]})
                primary.append(when)
    return events


def run(run_dir, resistance, quiet_limit_v=1.):
    if not math.isfinite(resistance) or resistance <= 0:
        raise ValueError("Specify independently verified positive resistance")
    if not math.isfinite(quiet_limit_v) or quiet_limit_v <= 0:
        raise ValueError("Invalid quiet band")
    root = raw_run_dir(run_dir)
    meta_path = root/"metadata.json"
    meta = json.loads(meta_path.read_text())
    analysis = analysis_root(root, legacy_ok=False)
    dest = ensure_derived_destination(analysis/"transient_charge_diagnostic", root)
    if dest.exists():
        raise FileExistsError(dest)
    rows, sources, record_stats = [], {}, []
    for record in meta["records"]:
        idx = int(record["index"])
        if record["status"] != "captured":
            raise ValueError("Incomplete waveform")
        wave = root/record["channels"]["CH1"]["path"]
        review = analysis/f"record_{idx:05d}"/"pulse_review"
        events = load_candidates(review, wave, idx)
        sources[str(wave.relative_to(root))] = sha256(wave)
        sources[f"record_{idx:05d}/pulse_review/review_manifest.json"] = sha256(review/"review_manifest.json")
        # Raw ADC code is required: no quantitative inference without rail check.
        with wave.open() as f:
            if f.readline().strip().split(",") != ["time_s","voltage_v","raw"]:
                raise ValueError("Expected original scope waveform CSV with raw codes")
        data = np.loadtxt(wave, delimiter=",", skiprows=1)
        t, v, raw = data.T
        if not np.all(np.isfinite(data)) or not np.all(np.diff(t)>0):
            raise ValueError("Invalid CH1 samples")
        rails = int(np.count_nonzero(np.abs(raw)>=RAIL))
        record_stats.append({"record": idx, "storage_rail_samples": rails,
                             "voltage_min_v": float(v.min()), "voltage_max_v": float(v.max()),
                             "sample_interval_us": float(np.median(np.diff(t))*1e6)})
        times = [float(e["time_s"]) for e in events]
        for i, event in enumerate(events):
            onset = times[i]
            neighbors = times[:i]+times[i+1:]
            previous = [float(e["time_s"]) for e in events
                        if e["kind"]=="accepted" and float(e["time_s"])<onset]
            for post in POST_WINDOWS_S:
                measured = measure(t,v,raw,onset,post,resistance,
                                   neighbor_times=neighbors,quiet_limit_v=quiet_limit_v)
                rows.append({"record": idx, **event,
                             "after_previous_biphasic_ms": (onset-max(previous))*1000 if previous else None,
                             **measured})
        del t,v,raw,data
    grouped = defaultdict(list)
    for row in rows:
        if row["window_post_us"] == PRIMARY_POST_S*1e6:
            grouped[row["kind"]].append(row)
    summary = {}
    for kind, items in sorted(grouped.items()):
        eligible = [r for r in items if r["status"]=="OK"]
        summary[kind] = {"candidate_count":len(items),
                         "status_counts":dict(Counter(r["status"] for r in items)),
                         "eligible":len(eligible),
                         "median":{key:float(np.median([r[key] for r in eligible])) if eligible else None
                                   for key in ("negative_charge_nc","positive_charge_nc","net_charge_nc",
                                               "negative_peak_v","positive_peak_v","negative_excursion_duration_us")}}
    dest.mkdir(parents=True, exist_ok=False)
    fields = list(dict.fromkeys(k for row in rows for k in row))
    if rows:
        write_csv(dest/"event_charge_windows.csv", rows, fields)
    atomic_json(dest/"summary.json", {
        "schema":"resistor_branch_transient_charge_v1", "run":root.name,
        "resistance_ohm":resistance, "quiet_limit_v":quiet_limit_v,
        "pre_us":PRE_S*1e6, "post_windows_us":[p*1e6 for p in POST_WINDOWS_S],
        "primary_post_us":PRIMARY_POST_S*1e6, "records":record_stats,
        "groups":summary, "source_sha256":sources, "metadata_sha256":sha256(meta_path),
        "total_device_charge_verified":False, "electrical_safety_verified":False,
        "physical_command_timing_verified":False,
        "interpretation":"Candidate-based finite-window resistor-branch estimate; only OK windows enter summaries. No storage rail does not exclude analog clipping. No proof of pulse balance, complete discharge or safety."})
    print(f"[CHARGE] {dest/'summary.json'}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--resistance-ohm", type=float, required=True)
    p.add_argument("--quiet-limit-v", type=float, default=1.)
    args = p.parse_args(argv)
    run(args.run_dir, args.resistance_ohm, args.quiet_limit_v)


if __name__ == "__main__":
    main()
