"""Compare a matched replay pair using only host-domain MCU and BLE evidence."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path
import json
import numpy as np

from analyze_mcu_recording import choose_recording, load_recordings
from communication.rate_replay import load_replay
from experiment_analysis.analyze_integrated_matrix32 import read_events
from experiment_analysis.analyze_command_chain import transaction_rows, sample_coverage, stats
from experiment_analysis.analyze_ble_session_overhead import group_transactions
from experiment_analysis.offset_robust_command_matching import controller_target_rates, _csv
from experiment_analysis.paths import analysis_root, ensure_derived_destination, raw_run_dir, sha256
from oscilloscope.record_scope import atomic_json


def unique(events, name):
    matches = [e for e in events if e["event"] == name]
    if len(matches) != 1:
        raise ValueError(f"Expected one {name}, found {len(matches)}")
    return matches[0]


def check_delivery(plan, batches, mcu, start):
    """Compare source values/order and actual host timestamps, excluding pre-roll."""
    if len(batches) != len(plan.batches):
        raise ValueError("Replay batch count differs from source")
    arrivals, expected, lateness = [], [], []
    for event, (offset, snapshots) in zip(batches, plan.batches):
        actual = int(event["arrival_ns"])
        if (event["source_offset_ns"] != offset or event["planned_ns"] != start+offset
                or event["sample_count"] != len(snapshots)):
            raise ValueError("Replay batch schedule differs from source")
        delay = actual-start-offset
        if not 0 <= delay <= 50_000_000:
            raise ValueError("Replay delivery outside lateness limit")
        lateness.append(delay/1e6)
        arrivals.extend([actual]*len(snapshots))
        expected.extend(snapshots)
    if any(b < a for a,b in zip(arrivals, arrivals[1:])):
        raise ValueError("Replay delivery timestamps out of order")
    times = np.asarray(mcu["taxel_pc_ns"])
    # The finite replay is the final rate stream after the synthetic pre-roll.
    idx = np.flatnonzero(times >= start)
    if not np.array_equal(times[idx], arrivals):
        raise ValueError("Recorded rate rows do not match replay delivery timestamps")
    for key, field in (("taxel_data", "taxels"), ("ra_rate_hz", "ra_rates_hz"),
                       ("sa_rate_hz", "sa_rates_hz"), ("taxel_raw_timestamps", "raw_timestamp_ms")):
        if not np.array_equal(mcu[key][idx], [s[field] for s in expected]):
            raise ValueError(f"Replayed values differ from source: {field}")
    return idx, stats(lateness)


def latency_groups(rows):
    groups = defaultdict(list)
    previous = None
    for row in rows:
        if row["family"] in ("sf", "sc"):
            groups[row["family"]].append(row)
            if row["family"] == "sf":
                groups["sf_after_"+(previous or "initial")].append(row)
        previous = row["family"]
    return {key: {"count": len(group), "success": sum(r["success"] for r in group),
                  "metrics_ms": {m: stats([r[m] for r in group if r["success"]])
                                 for m in ("source_to_request_ms", "request_to_write_ms",
                                           "write_to_ack_ms", "request_to_terminal_ms",
                                           "guard_sleep_requested_ms")}}
            for key, group in groups.items()}


def shared_transport(settings):
    """Read new transport identity or immutable historical A/B metadata."""
    if "live_transport" in settings:
        if settings["live_transport"] != "shared_persistent_sf_sc":
            raise ValueError("Unknown live transport")
        if "experimental_shared_sf_sc" in settings and settings["experimental_shared_sf_sc"] is not True:
            raise ValueError("Conflicting transport metadata")
        return True
    flag = settings["experimental_shared_sf_sc"]
    if type(flag) is not bool:
        raise ValueError("Invalid historical transport metadata")
    return flag


def analyze(run_dir, shared):
    root = raw_run_dir(run_dir)
    folder = root/"mcu_matrix32"
    rid = choose_recording(folder)
    mcu = load_recordings(folder, rid)
    config = mcu["experiment_settings"]["configuration"]
    control = config["stimulation_control"]
    source = control["replay_source"]
    if shared_transport(control) is not shared:
        raise ValueError("Unexpected transport mode")
    meta = json.loads((root/"metadata.json").read_text())
    if meta["plan"]["replay_source"] != source or shared_transport(meta["plan"]) is not shared:
        raise ValueError("Capture plan and controller settings disagree")
    if meta.get("status") != "complete":
        raise ValueError("Incomplete acquisition")
    trace = folder/f"{rid}_matrix32_commands.jsonl"
    events = read_events(trace)
    started = unique(events, "replay_started")
    completed = unique(events, "replay_completed")
    finished = unique(events, "replay_finished")
    if started["source"] != source or finished.get("stop_write_completed") is not True:
        raise ValueError("Missing verified replay provenance or STOP write")
    if completed["source_samples"] != source["sample_count"]:
        raise ValueError("Replay completion count mismatch")
    plan = load_replay(source["source_run"], duration_s=source["duration_s"],
                       start_s=(source["start_s"] if source["selection"].startswith("explicit") else None),
                       stale_s=control["stale_timeout_ms"]/1000)
    if plan.provenance != source:
        raise ValueError("Original replay source changed")
    start = int(started["scheduled_start_ns"])
    stop = start+round(source["duration_s"]*1e9)
    batches = [e for e in events if e["event"] == "replay_batch"]
    idx, jitter = check_delivery(plan, batches, mcu, start)
    routing = config["stimulation_source"]
    targets = controller_target_rates(mcu, int(routing["taxel"]), routing["encoding"])
    rows, issues = transaction_rows(events, mcu["taxel_pc_ns"], targets)
    dynamic = [r for r in rows if start <= r["request_ns"] < stop]
    coverage = sample_coverage(mcu["taxel_pc_ns"][idx], targets[idx], rows, events)
    counts = Counter(r["status"] for r in coverage)
    changes = sum(v for k,v in counts.items() if k.startswith("CHANGE_"))
    # Count only paired transaction subscription evidence, not every possible GATT operation.
    result = {
        "run": root.name, "shared": shared, "replay_source": source,
        "replay_initialization": control["replay_initialization"],
        "sample_count_verified": len(idx), "delivery_lateness_ms": jitter,
        "input_values_and_schedule_verified": True,
        "coverage_counts": dict(counts), "target_changes": changes,
        "unreferenced_changes": sum(v for k,v in counts.items() if k.startswith("CHANGE_UNREFERENCED")),
        "latency_groups": latency_groups(dynamic),
        "subscription_groups": group_transactions(dynamic),
        "measured_subscription_opens": sum(r["subscribe_ms"] is not None for r in dynamic if r["family"] in ("sf","sc")),
        "measured_subscription_closes": sum(r["unsubscribe_ms"] is not None for r in dynamic if r["family"] in ("sf","sc")),
        "pairing_issues": issues,
        "command_source_associations": dict(Counter(r["source_match"] for r in dynamic if r["family"] == "sf")),
        "transactions_finishing_after_replay_deadline": sum(r["terminal_ns"] >= stop for r in dynamic),
        "physical_application_verified": False,
        "source_sha256": {str(p.relative_to(root)): sha256(p) for p in
                          (root/"metadata.json", trace, folder/f"{rid}_settings.json",
                           folder/f"{rid}_tactile_rates.csv")},
    }
    return result, dynamic, coverage


def run(baseline, shared):
    a, ar, ac = analyze(baseline, False)
    b, br, bc = analyze(shared, True)
    if a["replay_source"] != b["replay_source"] or a["replay_initialization"] != b["replay_initialization"]:
        raise ValueError("A/B replay source or initialization differs")
    dest = ensure_derived_destination(
        analysis_root(shared, legacy_ok=False)/"ble_ab_comparison", baseline, shared)
    dest.mkdir(parents=True, exist_ok=False)
    atomic_json(dest/"summary.json", {"schema": "matrix32_ble_replay_ab_v1", "baseline": a, "shared": b})
    for label, rows, coverage in (("A",ar,ac),("B",br,bc)):
        if rows:
            _csv(dest/f"{label}_transactions.csv", rows, list(rows[0]))
        _csv(dest/f"{label}_sample_coverage.csv", coverage, list(coverage[0]))
    lines = ["# BLE replay A/B comparison", "", f"A: {a['run']}", f"B: {b['run']}",
             "", "Input values and planned schedule verified against original source.",
             "Pre-roll excluded from dynamic comparison. Host-domain measurements only.",
             "Missing subscription evidence is not proof of no subscription.",
             "Transactions may finish after replay deadline; failures are counted.",
             "", "| Metric | A | B |", "| --- | ---: | ---: |"]
    for key in ("sample_count_verified", "target_changes", "unreferenced_changes",
                "measured_subscription_opens", "measured_subscription_closes",
                "transactions_finishing_after_replay_deadline"):
        lines.append(f"| {key} | {a[key]} | {b[key]} |")
    lines.append(f"| Pairing issues | {len(a['pairing_issues'])} | {len(b['pairing_issues'])} |")
    def fmt(item):
        return f"{item['median']:.3f} / {item['p95']:.3f} (n={item['n']})" if item and item["n"] else "n/a"
    lines += ["", "| Command group / metric | A median / p95 (ms) | B median / p95 (ms) |",
              "| --- | ---: | ---: |"]
    lines.append(f"| Replay delivery lateness | {fmt(a['delivery_lateness_ms'])} | {fmt(b['delivery_lateness_ms'])} |")
    for group in sorted(set(a["latency_groups"]) | set(b["latency_groups"])):
        ga, gb = a["latency_groups"].get(group, {}), b["latency_groups"].get(group, {})
        lines.append(f"| {group} successful / total | {ga.get('success',0)}/{ga.get('count',0)} | {gb.get('success',0)}/{gb.get('count',0)} |")
        for metric in ("request_to_write_ms","write_to_ack_ms","request_to_terminal_ms"):
            lines.append(f"| {group}: {metric} | {fmt(ga.get('metrics_ms',{}).get(metric))} | {fmt(gb.get('metrics_ms',{}).get(metric))} |")
    (dest/"report.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    print(f"[BLE A/B] {dest/'report.md'}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--baseline", required=True, type=Path)
    p.add_argument("--shared", required=True, type=Path)
    args = p.parse_args(argv)
    run(args.baseline, args.shared)


if __name__ == "__main__":
    main()
