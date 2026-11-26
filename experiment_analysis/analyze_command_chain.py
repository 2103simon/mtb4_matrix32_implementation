"""Offline MCU arrival -> integer target -> request -> BLE write -> ACK audit.

Reuse the integer piecewise interval results; no waveform reads or copies.
Host timestamps are observations, not physical application times.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import json
import math
from pathlib import Path

import numpy as np

from analyze_mcu_recording import choose_recording, load_recordings
from experiment_analysis.analyze_integrated_matrix32 import read_events
from experiment_analysis.offset_robust_command_matching import controller_target_rates, _csv
from experiment_analysis.paths import (
    analysis_root, ensure_derived_destination, raw_run_dir, sha256,
)
from oscilloscope.record_scope import atomic_json


def stats(values):
    a = np.asarray([v for v in values if v is not None], dtype=float)
    return {"n": len(a), "median": float(np.median(a)) if len(a) else None,
            "p95": float(np.percentile(a, 95)) if len(a) else None,
            "max": float(np.max(a)) if len(a) else None}


def pair_transactions(events):
    """Pair only unique pending transactions of the same command family."""
    pending = {}
    pairs = []
    issues = []
    families = ("sf", "sc", "configuration")
    for event in sorted(events, key=lambda e: int(e["pc_monotonic_ns"])):
        name = event["event"]
        for family in families:
            if name == family + "_requested":
                if family in pending:
                    issues.append({"event": name, "reason": "overlapping_requests",
                                   "pc_monotonic_ns": event["pc_monotonic_ns"]})
                    pending[family] = None
                else:
                    pending[family] = event
            elif name in (family + "_ack", family + "_failed"):
                req = pending.pop(family, None)
                if req is None:
                    issues.append({"event": name, "reason": "no_unique_request",
                                   "pc_monotonic_ns": event["pc_monotonic_ns"]})
                    continue
                key = "current_command" if family == "sc" else "frequency_hz"
                if name.endswith("_ack") and req.get(key) != event.get(key):
                    issues.append({"event": name, "reason": "request_terminal_value_mismatch",
                                   "pc_monotonic_ns": event["pc_monotonic_ns"]})
                    continue
                pairs.append((family, req, event))
    for family, req in pending.items():
        issues.append({"event": family, "reason": "unterminated_request",
                       "pc_monotonic_ns": req["pc_monotonic_ns"] if req else None})
    return pairs, issues


def elapsed_ms(a, b):
    return (b-a)/1e6 if a is not None and b is not None and b >= a else None


def ns(evidence, key):
    v = evidence.get(key)
    return round(v*1e9) if isinstance(v, (int, float)) and math.isfinite(v) else None


def subscription_timings(evidence, request, terminal):
    """Measure only subscription events belonging to this transaction.

    Reused persistent sessions carry historical subscription timestamps.
    Legacy SC stores subscription/unsubscription timestamps in nested ACK.
    Pre-device-request time can include closing the SF session, but is
    not a direct measurement of that close.
    """
    nested = evidence.get("ack") or {}
    if not isinstance(nested, dict):
        nested = {}
    reused = evidence.get("persistent_session_reused")
    def stamp(key):
        value = ns(evidence, key)
        return value if value is not None else ns(nested, key)
    def duration(a, b):
        if a is None or b is None or not request <= a <= b <= terminal:
            return None
        return elapsed_ms(a, b)
    return {
        "persistent_session_reused": reused,
        "subscribe_ms": (None if reused is True else duration(
            stamp("subscribe_begin_monotonic_s"), stamp("subscribe_return_monotonic_s"))),
        "unsubscribe_ms": duration(stamp("unsubscribe_begin_monotonic_s"),
                                    stamp("unsubscribe_return_monotonic_s")),
        "before_device_request_ms": duration(request, ns(evidence, "request_monotonic_s")),
        "device_request_to_write_ms": duration(ns(evidence, "request_monotonic_s"),
                                               stamp("write_begin_monotonic_s")),
    }


def transaction_rows(events, sample_ns, targets):
    by_time = defaultdict(list)
    for i, t in enumerate(sample_ns):
        by_time[int(t)].append(i)
    pairs, issues = pair_transactions(events)
    rows = []
    for family, req, terminal in pairs:
        src = req.get("source_sample_ns")
        candidates = by_time.get(src, [])
        wanted = {int(targets[i]) for i in candidates}
        target = next(iter(wanted)) if len(wanted) == 1 else None
        request = int(req["pc_monotonic_ns"])
        done = int(terminal["pc_monotonic_ns"])
        ev = terminal.get("evidence")
        ev = ev if isinstance(ev, dict) else {}
        write = ns(ev, "write_begin_monotonic_s")
        ack = ns(ev, "ack_monotonic_s")
        ok = (terminal["event"].endswith("_ack") and
              (terminal.get("verified") is True if family == "configuration"
               else terminal.get("status") == "OK"))
        sf = req.get("frequency_hz")
        special = bool(req.get("zero_current_workaround") or
                       req.get("fast_zero_current_scheduler"))
        match = "NOT_SF"
        if family in ("sf", "configuration"):
            if type(sf) is not int or not 1 <= sf <= 400:
                match = "INVALID_SF"
            elif target is None:
                match = "SOURCE_MISSING_OR_AMBIGUOUS"
            elif special:
                match = "MUTE_SCHEDULER" if target == 0 and sf == 400 else "MUTE_SCHEDULER_MISMATCH"
            else:
                match = "MATCH" if target == sf else "MISMATCH"
        # Embedded BLE timestamps must lie within the logged transaction.
        timing_valid = (write is not None and ack is not None and
                        request <= write <= ack <= done)
        rows.append({
            "family": family, "request_ns": request, "terminal_ns": done,
            "source_sample_ns": src, "source_candidate_count": len(candidates),
            "source_target_hz": target, "requested_sf_hz": sf,
            "requested_sc": req.get("current_command"),
            "mode": ("mute_scheduler" if special else
                     "restore" if req.get("restoring_current") else "ordinary"),
            "success": ok, "source_match": match,
            "write_begin_ns": write, "ack_ns": ack,
            "embedded_timing_valid": timing_valid,
            "source_to_request_ms": elapsed_ms(src, request) if candidates else None,
            "request_to_write_ms": elapsed_ms(request, write) if timing_valid else None,
            "write_to_ack_ms": elapsed_ms(write, ack) if timing_valid and ok else None,
            "ack_to_trace_ms": elapsed_ms(ack, done) if timing_valid and ok else None,
            "request_to_terminal_ms": elapsed_ms(request, done),
            "guard_sleep_requested_ms": ev.get("guard_sleep_requested_ms"),
            **subscription_timings(ev, request, done),
        })
    return rows, issues


def sample_coverage(sample_ns, targets, transactions, events):
    """Classify target changes by trace evidence, not inferred worker decisions."""
    source_times = {e.get("source_sample_ns") for e in events
                    if e.get("event") in ("sf_requested", "sc_requested",
                                          "configuration_requested")}
    rows = []
    # A serial read batch can share a timestamp; don't invent unique sample IDs.
    grouped = defaultdict(list)
    for t, target in zip(sample_ns, targets):
        grouped[int(t)].append(int(target))
    previous = None
    first, last = min(grouped), max(grouped)
    for t, values in sorted(grouped.items()):
        distinct = set(values)
        target = values[0] if len(distinct) == 1 else None
        changed = target is not None and previous is not None and target != previous
        busy = [r for r in transactions if r["request_ns"] <= t < r["terminal_ns"]]
        status = ("AMBIGUOUS_BATCH" if target is None else
                  "INITIAL_OR_AFTER_AMBIGUITY" if previous is None else
                  "UNCHANGED_TARGET" if not changed else
                  "CHANGE_REFERENCED_BY_REQUEST" if t in source_times else
                  "CHANGE_UNREFERENCED_DURING_TRANSACTION" if busy else
                  "CHANGE_UNREFERENCED_OUTSIDE_TRANSACTION")
        rows.append({"sample_ns": t, "samples_at_timestamp": len(values),
                     "target_hz": target, "previous_target_hz": previous,
                     "status": status, "busy_families": "+".join(sorted({r["family"] for r in busy})),
                     "at_recording_boundary": t in (first, last)})
        previous = target
    return rows


def physical_disagreements(intervals, transactions):
    """Connect stable target/ACK discrepancies to actual SF trace evidence.

    A matching ACK value/time is not proof of when the device applied SF.
    SC restore can activate a previously acknowledged SF; leave it unmatched
    rather than falsely linking a later identical frequency.
    """
    rows = []
    successes = sorted((r for r in transactions if r["family"] == "sf" and r["success"]),
                       key=lambda r: r["terminal_ns"])
    for p in intervals:
        if (p["controller_target_assessment"] != "STABLE_ACROSS_TESTED_OFFSETS" or
                p["ack_assessment"] != "STABLE_ACROSS_TESTED_OFFSETS"):
            continue
        target = int(float(p["controller_target_hz_if_stable"]))
        ack = int(float(p["ack_expected_hz_if_stable"]))
        start = float(p["start_host_lower_ns"])
        prior = [r for r in successes if r["terminal_ns"] <= start]
        last = prior[-1] if prior else None
        # Only report the latest preceding SF transaction as a contextual
        # observation, not necessarily the active-state originating event.
        linked = last if last and last["requested_sf_hz"] == ack else {}
        rows.append({"record": p["record"], "start_onset_id": p["start_onset_id"],
                     "start_scope_s": p["start_scope_s"], "target_hz": target,
                     "ack_hz": ack, "physical_hz": float(p["measured_inverse_period_hz"]),
                     "target_equals_ack": target == ack,
                     "preceding_matching_sf_request_ns": linked.get("request_ns"),
                     "preceding_matching_sf_source_ns": linked.get("source_sample_ns"),
                     "preceding_matching_sf_source_target_hz": linked.get("source_target_hz"),
                     "preceding_matching_sf_terminal_ns": linked.get("terminal_ns"),
                     "sf_trace_age_lower_ms": elapsed_ms(linked.get("terminal_ns"), start),
                     "sf_trace_age_upper_ms": elapsed_ms(linked.get("terminal_ns"),
                                                        float(p["start_host_upper_ns"]))})
    return rows


def run(run_dir):
    root = raw_run_dir(run_dir)
    analysis = analysis_root(root, legacy_ok=False)
    prior = analysis/"piecewise_command_analysis_integer"
    summary_path, intervals_path = prior/"summary.json", prior/"all_intervals.csv"
    previous = json.loads(summary_path.read_text())
    if (previous["source_run"] != root.name or
            previous["schema"] != "matrix32_piecewise_command_association_integer_v2"):
        raise ValueError("Wrong integer interval analysis")
    for name, digest in previous["source_sha256"].items():
        if sha256(root/name) != digest:
            raise ValueError(f"Source changed: {name}")
    mcu_dir = root/"mcu_matrix32"
    rid = choose_recording(mcu_dir)
    mcu = load_recordings(mcu_dir, rid)
    if mcu.get("mode") != "rates":
        raise ValueError("Expected recorded MCU rate snapshots")
    source = mcu["experiment_settings"]["configuration"]["stimulation_source"]
    taxel, encoding = int(source["taxel"]), source["encoding"]
    if (taxel, encoding) != (previous["taxel"], previous["encoding"]):
        raise ValueError("Stimulation source differs from interval analysis")
    trace = mcu_dir/f"{rid}_matrix32_commands.jsonl"
    events = read_events(trace)
    if not events:
        raise ValueError("Missing command trace")
    timestamps = mcu["taxel_pc_ns"]
    targets = controller_target_rates(mcu, taxel, encoding)
    tx, issues = transaction_rows(events, timestamps, targets)
    coverage = sample_coverage(timestamps, targets, tx, events)
    with intervals_path.open(newline="") as f:
        intervals = list(csv.DictReader(f))
    if len(intervals) != previous["total_approved_intervals"]:
        raise ValueError("Interval count mismatch")
    physical = physical_disagreements(intervals, tx)
    metrics = ("source_to_request_ms", "request_to_write_ms", "write_to_ack_ms",
               "ack_to_trace_ms", "request_to_terminal_ms", "guard_sleep_requested_ms")
    groups = {}
    for family in ("sf", "sc", "configuration"):
        for mode in ("ordinary", "mute_scheduler", "restore"):
            group = [r for r in tx if r["family"] == family and r["mode"] == mode]
            if group:
                good = [r for r in group if r["success"]]
                groups[f"{family}_{mode}"] = {
                    "paired": len(group), "success": len(good),
                    "source_match_counts": dict(Counter(r["source_match"] for r in group)),
                    "successful_timing": {k: stats([r[k] for r in good]) for k in metrics},
                }
    # Report host-session coverage separately from physical acquisition coverage.
    payload = {
        "schema": "matrix32_command_chain_diagnostic_v1", "run": root.name,
        "taxel": taxel, "encoding": encoding,
        "source_sha256": {str(p.relative_to(root)): sha256(p) for p in
                          (trace, mcu_dir/f"{rid}_tactile_rates.csv",
                           mcu_dir/f"{rid}_settings.json")},
        "derived_sha256": {p.name: sha256(p) for p in (summary_path, intervals_path)},
        "trace_event_counts": dict(Counter(e["event"] for e in events)),
        "pairing_issues": issues, "transactions": groups,
        "host_session_samples": len(timestamps),
        "distinct_host_timestamps": len(coverage),
        "sample_coverage_counts": dict(Counter(r["status"] for r in coverage)),
        "physical_both_stable_intervals": len(physical),
        "physical_target_ack_disagreements": sum(not r["target_equals_ack"] for r in physical),
        "physical_application_time_verified": False,
    }
    dest = ensure_derived_destination(analysis/"command_chain_diagnostic", root)
    dest.mkdir(parents=True, exist_ok=False)
    for name, rows in (("transactions", tx), ("sample_coverage", coverage),
                       ("physical_same_intervals", physical)):
        if rows:
            _csv(dest/f"{name}.csv", rows, list(rows[0]))
    atomic_json(dest/"summary.json", payload)
    lines = ["# MCU to Matrix32 command chain (exploratory)", "",
             f"Recording: {root.name}; taxel {taxel}; encoding {encoding}.", "",
             "All latency measurements below are PC monotonic observations, not physical application latency.",
             "Host logs span the entire session, including time outside the scope acquisition.",
             "No request reference does not prove a sample was skipped; equal targets and latest-slot behavior are expected.",
             "Same-arrival timestamp batches are not unique sample IDs.", "",
             "| Command group | Successful / paired | Sample arrival to request median / p95 (ms) | Request to write median / p95 (ms) | Write to ACK median / p95 (ms) |",
             "| --- | ---: | ---: | ---: | ---: |"]
    def fmt(s):
        return "n/a" if s["n"] == 0 else f"{s['median']:.3f} / {s['p95']:.3f} (n={s['n']})"
    for name, g in groups.items():
        m = g["successful_timing"]
        lines.append(f"| {name} | {g['success']} / {g['paired']} | {fmt(m['source_to_request_ms'])} | {fmt(m['request_to_write_ms'])} | {fmt(m['write_to_ack_ms'])} |")
    lines += ["", "## Target coverage (host session)", "",
              *[f"- {k}: {v}" for k,v in payload["sample_coverage_counts"].items()],
              "", f"Pairing issues: {len(issues)}; see summary.json.", "",
              "## Physical comparison (scope coverage only)", "",
              f"Both stable: {len(physical)}; differing target and ACK: {payload['physical_target_ack_disagreements']}.",
              "The preceding matching SF transaction is contextual evidence only, not proof of device application.",
              "Mute scheduler SF400 and SC transactions are separated from ordinary positive SF commands.",
              "", "See transactions.csv, sample_coverage.csv and physical_same_intervals.csv for details."]
    (dest/"report.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    print(f"[CHAIN] {dest/'report.md'}")
    return payload


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir", required=True, type=Path)
    return run(p.parse_args(argv).run_dir)


if __name__ == "__main__":
    main()
