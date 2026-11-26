"""Per-SF-ACK observability with the independently derived piecewise clock map.

One scope record per run. All successful live SF ACKs are inventoried, including
muted scheduler commands, restore commands, and ACKs outside Gray support.
Offline exploratory compatibility only; no physical application proof.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

import numpy as np

from communication.clock_sync import read_active_exchanges
from experiment_analysis.audit_ack_physical_observability import (
    verify_approved_record, event_stream, MIN_PREVIOUS_CYCLE_GUARD_S,
    MATCH_ABSOLUTE_HZ, MATCH_RELATIVE_FRACTION, MAX_REGULAR_PERIOD_S,
)
from experiment_analysis.analyze_command_chain import pair_transactions
from experiment_analysis.analyze_integrated_matrix32 import acknowledged_frequency_states
from experiment_analysis.offset_robust_command_matching import refined_onset_envelopes, _csv
from experiment_analysis.paths import analysis_root, ensure_derived_destination, raw_run_dir, sha256
from oscilloscope.align_gray_records import read_nano_frames
from oscilloscope.clock_qualification_diagnostics import _frame_pairs
from oscilloscope.evaluate_clock_models import _xy
from oscilloscope.refine_timing_investigation import record_sync_exchanges
from oscilloscope.record_scope import atomic_json


def longest_consecutive(indices):
    longest = run = 0
    previous = None
    for i in sorted(set(indices)):
        run = run+1 if previous is not None and i == previous+1 else 1
        longest = max(longest, run)
        previous = i
    return longest


def assess_window(periods, lower, upper, valid, start, end, hz, guard_s):
    """Full continuous tested host envelope must fit within a control window."""
    possible = np.flatnonzero(valid & (lower[:-1] >= start) &
                              (upper[1:] < end) &
                              (periods <= MAX_REGULAR_PERIOD_S))
    eligible = possible[lower[possible] >= start+guard_s*1e9]
    tolerance = max(MATCH_ABSOLUTE_HZ, MATCH_RELATIVE_FRACTION*hz)
    matching = eligible[np.abs(1/periods[eligible]-hz) <= tolerance]
    relaxed = possible[np.abs(1/periods[possible]-hz) <= tolerance]
    return {
        "eligible": eligible.tolist(), "matching": matching.tolist(),
        "relaxed_matching": relaxed.tolist(),
        "nonmatching": sorted(set(eligible.tolist())-set(matching.tolist())),
        "consecutive_matching": longest_consecutive(matching.tolist()),
        "available_ms": max(0., end-start-guard_s*1e9)/1e6,
    }


def classify(events, times, onset_ids, mapped, support):
    """Associate each active state with the most recent successful SF identity."""
    ordered = sorted(events, key=lambda e: int(e["pc_monotonic_ns"]))
    pairs, issues = pair_transactions(ordered)
    if issues:
        raise ValueError(f"Ambiguous request/terminal pairing: {issues}")
    sf_pairs = {int(done["pc_monotonic_ns"]): req
                for family, req, done in pairs if family == "sf"
                and done["event"] == "sf_ack" and done.get("status") == "OK"}
    states = acknowledged_frequency_states(ordered)
    sf_events = [e for e in ordered if e.get("event")=="sf_ack" and e.get("status")=="OK"]
    if len(sf_pairs) != len(sf_events):
        raise ValueError("Successful SF ACKs not uniquely paired")
    if any(e.get("event") in ("sf_failed","sc_failed","configuration_failed",
                              "stop_failed","ble_lost","start_failed")
           for e in ordered):
        raise ValueError("Control failure present: investigate separately before per-ACK audit")
    by_ack = {}
    previous_hz = None
    latest = None
    for event in ordered:
        kind = event["event"]
        at = int(event["pc_monotonic_ns"])
        if kind == "start_write_completed":
            latest = None
            previous_hz = event.get("frequency_hz")
        elif kind == "sf_ack" and event.get("status")=="OK":
            hz = int(event["frequency_hz"])
            if not 1 <= hz <= 400:
                raise ValueError("Invalid acknowledged frequency")
            req = sf_pairs[at]
            latest = at
            scheduler = bool(req.get("zero_current_workaround") or
                             req.get("fast_zero_current_scheduler"))
            by_ack[at] = {"ack_ns": at, "request_ns": int(req["pc_monotonic_ns"]),
                          "source_sample_ns": req.get("source_sample_ns"),
                          "ack_hz": hz, "previous_sf_hz": previous_hz,
                          "purpose": "muted_scheduler" if scheduler else
                                     "restore" if req.get("restoring_current") else "ordinary",
                          "windows": []}
            previous_hz = hz
        elif kind in ("stop_write_completed","shutdown_stop_write_completed","ble_lost"):
            latest = None
        # Attach an identity to each active ACK state, including SC restoration.
        for state in states:
            if int(state["pc_monotonic_ns"]) != at or not state["output_active_assumed"]:
                continue
            if latest is not None and state["source"] in ("sf_ack","sc_ack_restored"):
                if state["expected_hz"] != by_ack[latest]["ack_hz"]:
                    raise ValueError("SF identity differs from reconstructed active state")
                by_ack[latest]["windows"].append((at,state["source"]))
    periods = np.diff(times)
    lower, upper = mapped["lower"], mapped["upper"]
    start_lower, start_upper, end_lower, end_upper = support
    controls = sorted(int(e["pc_monotonic_ns"]) for e in ordered
                      if e["event"] in ("sf_requested","sc_requested","configuration_requested",
                                         "stop_requested","shutdown_stop_requested","start_requested"))
    rows = []
    for item in by_ack.values():
        at, hz = item["ack_ns"], item["ack_hz"]
        scope = ("outside" if at < start_lower or at > end_upper else
                 "inside" if start_upper <= at <= end_lower else "edge_uncertain")
        all_eligible, all_matching, all_relaxed, all_bad = set(),set(),set(),set()
        max_run = 0
        available = 0.
        details = []
        prev = item["previous_sf_hz"]
        for activation, activation_kind in item["windows"]:
            later = [t for t in controls if t > activation]
            end = min(end_lower, later[0]) if later else end_lower
            # Use previous SF period, and for current restore also the
            # restored SF period. These are settling assumptions, not bounds.
            guard = max(MIN_PREVIOUS_CYCLE_GUARD_S, 1/float(prev)) if prev else None
            if activation_kind == "sc_ack_restored":
                guard = max(guard or 0., MIN_PREVIOUS_CYCLE_GUARD_S, 1/hz)
            if guard is None or end <= max(start_upper,activation):
                continue
            # Apply the guard to the activation, not to the scope boundary.
            effective_start = max(start_upper, activation+guard*1e9)
            w = assess_window(periods, lower, upper, mapped["interval_valid"],
                              effective_start,end,hz,0.)
            relaxed = assess_window(periods, lower, upper, mapped["interval_valid"],
                                    max(start_upper,activation),end,hz,0.)
            all_eligible.update(w["eligible"])
            all_matching.update(w["matching"])
            all_bad.update(w["nonmatching"])
            all_relaxed.update(relaxed["matching"])
            available += w["available_ms"]
            max_run = max(max_run,w["consecutive_matching"])
            details.append({"activation_ns":activation,"activation_kind":activation_kind,
                            "end_ns":end,"guard_ms":guard*1000,
                            "available_ms":w["available_ms"]})
        if scope != "inside":
            status = "OUTSIDE_GRAY_SUPPORT" if scope=="outside" else "GRAY_EDGE_UNCERTAIN"
        elif item["purpose"]=="muted_scheduler" and not details:
            status = "MUTED_SCHEDULER_NO_ACTIVE_WINDOW"
        elif not details:
            status = "NO_ACTIVE_WINDOW_IN_SUPPORTED_CAPTURE"
        elif all_matching and all_bad:
            status = "MIXED_MATCHING_AND_NONMATCHING"
        elif all_matching:
            status = "MATCHING_COMPLETE_PERIOD"
        elif all_bad:
            status = "NONMATCHING_ELIGIBLE_PERIOD_REVIEW"
        elif available < 1000/hz:
            status = "INSUFFICIENT_SETTLED_WINDOW"
        else:
            status = "NO_ISOLATED_COMPLETE_PERIOD"
        rows.append({**{k:v for k,v in item.items() if k!="windows"},
                     "gray_scope_membership":scope, "classification":status,
                     "active_window_count":len(details),
                     "active_windows_json":json.dumps(details),
                     "settled_window_ms":available,
                     "eligible_periods":len(all_eligible),
                     "matching_periods":len(all_matching),
                     "nonmatching_periods":len(all_bad),
                     "max_consecutive_matching_periods":max_run,
                     "matching_without_guard":len(all_relaxed),
                     "witness_ids":";".join(f"{onset_ids[i]}->{onset_ids[i+1]}"
                                             for i in sorted(all_matching)),
                     "physical_application_proven":False})
    return rows


def summarize(rows):
    inside = [r for r in rows if r["gray_scope_membership"]=="inside"]
    targets = [r for r in inside if r["purpose"]!="muted_scheduler"]
    def counts(items):
        return {"n":len(items), "one_or_more_matching":sum(r["matching_periods"]>0 for r in items),
                "two_or_more_consecutive_matching":sum(r["max_consecutive_matching_periods"]>=2 for r in items),
                "matching_without_guard":sum(r["matching_without_guard"]>0 for r in items),
                "any_eligible_period":sum(r["eligible_periods"]>0 for r in items),
                "classification_counts":dict(Counter(r["classification"] for r in items))}
    return {"all_sf_acks":len(rows), "all_inside":counts(inside),
            "target_sf_inside":counts(targets),
            "ordinary_inside":counts([r for r in targets if r["purpose"]=="ordinary"]),
            "restore_inside":counts([r for r in targets if r["purpose"]=="restore"]),
            "scheduler_inside":counts([r for r in inside if r["purpose"]=="muted_scheduler"]),
            "scope_membership_counts":dict(Counter(r["gray_scope_membership"] for r in rows))}


def run(run_dir):
    root = raw_run_dir(run_dir)
    analysis = analysis_root(root, legacy_ok=False)
    sources = [root/name for name in ("metadata.json","nano_serial.jsonl","epoch_clock_analysis.json")]
    hashes = {p.name:sha256(p) for p in sources}
    meta = json.loads(sources[0].read_text())
    epochs = json.loads(sources[2].read_text())["records"]
    refinement_path = analysis/"timing_refinement"/"timing_refinement_summary.json"
    refinement = json.loads(refinement_path.read_text())
    if (refinement["schema"]!="timing_refinement_v1" or refinement["recording"]!=root.name
            or refinement["source_sha256"]!=hashes):
        raise ValueError("Timing refinement provenance mismatch")
    if len(meta["records"])!=1 or len(epochs)!=1 or len(refinement["records"])!=1:
        raise ValueError("This diagnostic requires one scope record")
    record, epoch, refined = meta["records"][0], epochs[0], refinement["records"][0]
    if not int(record["index"])==int(epoch["record_index"])==int(refined["record_index"]):
        raise ValueError("Record identity mismatch")
    approval, onsets, times, excluded = verify_approved_record(root, record)
    trace, events = event_stream(root)
    pairs, issue = _frame_pairs(root, record, read_nano_frames(sources[1]))
    if issue:
        raise ValueError(issue)
    x,y = _xy(pairs)
    if np.any(np.diff(x)>2.):
        raise ValueError("Internal unsupported Gray gap; separate support windows required")
    exchange_list = read_active_exchanges(sources[1])
    by_seq = {r["seq"]:r for r in exchange_list}
    if len(by_seq)!=len(exchange_list):
        raise ValueError("Duplicate SYNC sequence")
    exchanges = record_sync_exchanges(record,epoch,by_seq)
    dest = ensure_derived_destination(analysis/"refined_ack_observability", root)
    if dest.exists():
        raise FileExistsError(dest)
    scenarios, results = {}, {}
    for scale in (1.,2.,3.):
        # Map support endpoints together with the signed physical onsets.
        all_times = np.r_[x[0],times,x[-1]]
        mapped = refined_onset_envelopes(all_times,x,y,epoch,refined,exchanges,scale=scale)
        lo,hi = mapped["lower"],mapped["upper"]
        support = (lo[0],hi[0],lo[-1],hi[-1])
        trimmed = {"lower":lo[1:-1],"upper":hi[1:-1],
                   "interval_valid":mapped["interval_valid"][1:-1]}
        rows = classify(events,times,[r["id"] for r in onsets],trimmed,support)
        results[str(scale)] = rows
        scenarios[str(scale)] = {**summarize(rows), "clock":mapped["scenario"]}
    dest.mkdir(parents=True,exist_ok=False)
    for scale,rows in results.items():
        if rows:
            _csv(dest/f"sf_ack_scale_{scale}.csv",rows,list(rows[0]))
    payload = {"schema":"matrix32_refined_per_ack_v1","run":root.name,
               "source_sha256":hashes,"trace_sha256":sha256(trace),
               "timing_refinement_sha256":sha256(refinement_path),
               "approved_onsets_sha256":approval["approved_onsets_sha256"],
               "approved_onsets":len(onsets),"excluded_negative_only":excluded,
               "physical_application_proven":False,"absolute_alignment_calibrated":False,
               "scenarios":scenarios}
    atomic_json(dest/"summary.json",payload)
    lines = ["# Exploratory per-ACK physical observability","",f"Recording: {root.name}","",
             "Counts refer to SF ACK events, not unique numeric frequencies.",
             "Denominator: successful live SF ACKs inside supported Gray window.",
             "Target denominator excludes muted-scheduler SF; restore SF counts only with active evidence.",
             "All eligible complete periods must fit in the full tested host envelope after a",
             "prior-cycle guard and before the next control request (not merely its ACK).",
             "Tolerance: max(1 Hz, 2.5% of SF). Missing witnesses do not prove non-execution.",
             "Two consecutive matching periods are a stronger witness, not unique proof.",
             "The refined clock is not calibrated; Gray holdout p95 margins are heuristic.","",
             "| Gray margin scale | Target ACKs | >=1 matching period | >=2 consecutive | No-guard matching |",
             "| --- | ---: | ---: | ---: | ---: |"]
    for scale,s in scenarios.items():
        g=s["target_sf_inside"]
        lines.append(f"| {scale} | {g['n']} | {g['one_or_more_matching']} | {g['two_or_more_consecutive_matching']} | {g['matching_without_guard']} |")
    lines += ["","## Primary scenario (1x)","",json.dumps(scenarios["1.0"],indent=2)]
    (dest/"report.md").write_text("\n".join(lines)+"\n")
    print(f"[ACK OBSERVABILITY] {dest/'report.md'}")


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir",required=True,type=Path)
    run(p.parse_args(argv).run_dir)


if __name__=="__main__":
    main()
