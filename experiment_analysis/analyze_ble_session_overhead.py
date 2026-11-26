"""Quantify BLE subscription overhead using existing host logs only.

No hardware, waveform reads, copies, or edits to existing analysis.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path

from analyze_mcu_recording import choose_recording, load_recordings
from experiment_analysis.analyze_integrated_matrix32 import read_events
from experiment_analysis.analyze_command_chain import transaction_rows, stats
from experiment_analysis.offset_robust_command_matching import controller_target_rates, _csv
from experiment_analysis.paths import (
    analysis_root, ensure_derived_destination, raw_run_dir, sha256,
)
from oscilloscope.record_scope import atomic_json


METRICS = ("subscribe_ms", "unsubscribe_ms", "before_device_request_ms",
           "device_request_to_write_ms", "request_to_write_ms",
           "write_to_ack_ms", "ack_to_trace_ms", "request_to_terminal_ms",
           "guard_sleep_requested_ms")


def group_transactions(rows):
    grouped = defaultdict(list)
    for row in rows:
        if not row["success"] or row["family"] not in ("sf", "sc"):
            continue
        reused = row["persistent_session_reused"]
        lifecycle = ("reused" if reused is True else
                     "opened" if reused is False else "legacy_or_unrecorded")
        label = f"{row['family']}_{row['mode']}_{lifecycle}"
        grouped[label].append(row)
    return {name: {"count": len(items),
                   "metrics_ms": {key: stats([r[key] for r in items])
                                  for key in METRICS}}
            for name, items in sorted(grouped.items())}


def run(run_dir):
    root = raw_run_dir(run_dir)
    mcu_dir = root/"mcu_matrix32"
    rid = choose_recording(mcu_dir)
    mcu = load_recordings(mcu_dir, rid)
    if mcu.get("mode") != "rates":
        raise ValueError("Expected rate snapshots")
    source = mcu["experiment_settings"]["configuration"]["stimulation_source"]
    taxel, encoding = int(source["taxel"]), source["encoding"]
    trace = mcu_dir/f"{rid}_matrix32_commands.jsonl"
    events = read_events(trace)
    if not events:
        raise ValueError("Missing command trace")
    rows, issues = transaction_rows(
        events, mcu["taxel_pc_ns"], controller_target_rates(mcu, taxel, encoding))
    groups = group_transactions(rows)
    payload = {
        "schema": "matrix32_ble_subscription_overhead_v1",
        "run": root.name,
        "source_sha256": {str(p.relative_to(root)): sha256(p) for p in
                          (trace, mcu_dir/f"{rid}_tactile_rates.csv",
                           mcu_dir/f"{rid}_settings.json")},
        "groups": groups, "pairing_issues": issues,
        "transaction_counts": dict(Counter(r["family"] for r in rows)),
        "physical_application_time_verified": False,
    }
    dest = ensure_derived_destination(
        analysis_root(root, legacy_ok=False)/"ble_session_overhead", root)
    dest.mkdir(parents=True, exist_ok=False)
    if rows:
        _csv(dest/"transactions.csv", rows, list(rows[0]))
    atomic_json(dest/"summary.json", payload)
    lines = [
        "# BLE indication session overhead", "",
        f"Recording: {root.name}", "",
        "Host-domain measurements only, not physical command application.",
        "SF reused subscriptions are historical and not counted as new setup.",
        "SC pre-device-request time includes possible SF teardown plus other host work;",
        "it does not directly measure the old subscription's close.",
        "Missing timestamps remain unavailable, not zero. Configuration omitted from grouped metrics.", "",
        "| Group | n | Subscribe | Unsubscribe | Before device request | Request to write | Write to ACK | Total |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    def fmt(item):
        if not item["n"]:
            return "n/a"
        return f"{item['median']:.3f} / {item['p95']:.3f} (n={item['n']})"
    for name, group in groups.items():
        m = group["metrics_ms"]
        values = [fmt(m[key]) for key in (
            "subscribe_ms", "unsubscribe_ms", "before_device_request_ms",
            "request_to_write_ms", "write_to_ack_ms", "request_to_terminal_ms")]
        lines.append(f"| {name} | {group['count']} | " + " | ".join(values) + " |")
    lines += ["", "Cells show median / p95 in ms with valid sample count.",
              f"Pairing issues: {len(issues)}.",
              "No transport behavior was changed by this analysis."]
    (dest/"report.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    print(f"[BLE SESSION] {dest/'report.md'}")
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    return run(parser.parse_args(argv).run_dir)


if __name__ == "__main__":
    main()
