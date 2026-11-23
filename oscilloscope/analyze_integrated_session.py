"""Analyze distinct scope acquisitions independently, then summarize a session.

Offline-only. Scope time axes are local to EACH record. The continuous MCU
and BLE traces are never concatenated with scope samples. Host-time mapping
is accepted only for matching per-record clock fits, and remains provisional.
"""
from __future__ import annotations

from collections import Counter
import json
import math
from pathlib import Path

import numpy as np

from analyze_mcu_recording import choose_recording, load_recordings, plot_traces
from oscilloscope.record_scope import atomic_json
from oscilloscope.matrix32_dynamic_frequency import (
    analyze_dynamic_cycles, plot_dynamic_tracking,
)
from oscilloscope.analyze_integrated_matrix32 import (
    acknowledged_frequency_states, compare_physical_with_acks, load_wave,
    make_plots, plot_frequency_comparison, pulse_statistics, read_events,
    rising_pulses, sliding_physical_rates, summarize_frequency_by_ack,
    tentative_host_map, write_markdown_report,
)


def record_boundaries(records):
    """Validate distinct record identities, never infer adjacency from index."""
    if not records:
        raise ValueError("No scope records")
    ids = [int(r["index"]) for r in records]
    if len(set(ids)) != len(ids) or any(i < 1 for i in ids):
        raise ValueError("Duplicate or invalid scope record index")
    for rec in records:
        if rec.get("status") != "captured":
            raise ValueError(f"Scope record {rec['index']} is not captured")
        if not all(ch in rec.get("channels", {}) for ch in ("CH1", "CH2")):
            raise ValueError(f"Scope record {rec['index']} is missing CH1 or CH2")
    return sorted(records, key=lambda r: int(r["index"]))


def coverage_overlaps(mapping, mcu, events):
    """Count only FULLY mapped scope-window observations; never fill gaps."""
    if mapping is None:
        return {"status": "unknown_no_per_record_host_map",
                "mcu_sample_count": None, "ble_event_count": None,
                "first_sample_host_ns_est": None, "last_sample_host_ns_est": None}
    h0 = int(mapping["first_sample_pc_monotonic_ns_est"])
    h1 = int(mapping["last_sample_pc_monotonic_ns_est"])
    if h1 <= h0:
        raise ValueError("Non-positive per-record mapped host duration")
    pc = np.asarray(mcu["taxel_pc_ns"], dtype=np.int64)
    matched_events = [
        event for event in events
        if "pc_monotonic_ns" in event
        and h0 <= int(event["pc_monotonic_ns"]) <= h1
    ]
    return {
        "status": "provisional_clock_mapped_overlap",
        "first_sample_host_ns_est": h0,
        "last_sample_host_ns_est": h1,
        "mcu_sample_count": int(np.count_nonzero((pc >= h0) & (pc <= h1))),
        "ble_event_count": len(matched_events),
    }


def select_overlap_mcu(mcu, mapping):
    """Return the portion observed during ONE scope record, not the full log."""
    if mapping is None:
        mask = np.zeros(len(mcu["taxel_pc_ns"]), dtype=bool)
    else:
        pc = mcu["taxel_pc_ns"]
        mask = ((pc >= mapping["first_sample_pc_monotonic_ns_est"])
                & (pc <= mapping["last_sample_pc_monotonic_ns_est"]))
    selected = dict(mcu)
    for key in ("taxel_data", "sa_rate_hz", "ra_rate_hz", "taxel_pc_ns",
                "taxel_corrected_timestamps", "taxel_raw_timestamps"):
        if key in mcu:
            selected[key] = mcu[key][mask]
    return selected


def summarize_full_stream(mcu, events):
    pc = np.asarray(mcu["taxel_pc_ns"], dtype=np.int64)
    diff = np.diff(pc)
    valid = diff[diff > 0]
    # All time measurements here are native PC monotonic; no scope-to-Nano
    # clock fit is needed for continuous MCU/ACK throughput statistics.
    full_span_s = float((int(pc[-1]) - int(pc[0])) / 1e9)
    ok_sf = sorted(
        [e for e in events if e.get("event") == "sf_ack"
         and e.get("status") == "OK" and "pc_monotonic_ns" in e],
        key=lambda e: int(e["pc_monotonic_ns"]),
    )
    sf_times = np.asarray([int(e["pc_monotonic_ns"]) for e in ok_sf],
                          dtype=np.int64)
    sf_deltas = np.diff(sf_times)
    positive_sf_deltas = sf_deltas[sf_deltas > 0]
    sf_requested = [e for e in events if e.get("event") == "sf_requested"]
    sf_muted = [e for e in ok_sf if e.get("zero_current_workaround")]
    sf_restore_while_muted = [
        e for e in ok_sf if e.get("restoring_current")
    ]
    sf_active = [
        e for e in ok_sf
        if not e.get("zero_current_workaround")
        and not e.get("restoring_current")
    ]
    sf_failed = [e for e in events if e.get("event") == "sf_failed"]
    acked_frequencies = sorted({
        int(e["frequency_hz"]) for e in ok_sf if e.get("frequency_hz") is not None
    })
    return {
        "recording_id": mcu["recording_id"],
        "rate_snapshots": int(len(pc)),
        "first_arrival_pc_monotonic_ns": int(pc[0]),
        "last_arrival_pc_monotonic_ns": int(pc[-1]),
        "arrival_span_s": float((int(pc[-1])-int(pc[0])) / 1e9),
        "median_positive_arrival_interval_ms": (
            float(np.median(valid)/1e6) if len(valid) else None
        ),
        "host_arrival_rate_hz": (
            float(1e9/np.median(valid)) if len(valid) else None
        ),
        "arrival_interval_p10_ms": (
            float(np.percentile(valid, 10)/1e6) if len(valid) else None
        ),
        "arrival_interval_p90_ms": (
            float(np.percentile(valid, 90)/1e6) if len(valid) else None
        ),
        "total_mcu_arrivals_per_second": (
            float((len(pc)-1)/full_span_s) if full_span_s > 0 else None
        ),
        "arrival_nonpositive_delta_count": int(len(diff)-len(valid)),
        "arrival_intervals_above_twice_median_count": (
            int(np.count_nonzero(valid > 2 * np.median(valid)))
            if len(valid) else None
        ),
        "ble_events": len(events),
        "ble_events_by_type": dict(Counter(e.get("event", "?") for e in events)),
        "sf_requested_event_count": len(sf_requested),
        "sf_ack_ok_count": len(ok_sf),
        "sf_failed_event_count": len(sf_failed),
        "sf_ack_non_ok_count": sum(
            e.get("event") == "sf_ack" and e.get("status") != "OK"
            for e in events
        ),
        "sf_ack_ok_rate_per_mcu_recording_second": (
            float(len(ok_sf)/full_span_s) if full_span_s > 0 else None
        ),
        "sf_ack_ok_rate_per_ack_span_second": (
            float((len(ok_sf)-1) / ((int(sf_times[-1])-int(sf_times[0]))/1e9))
            if len(ok_sf) > 1 and sf_times[-1] > sf_times[0] else None
        ),
        "sf_ack_ok_median_interack_ms": (
            float(np.median(positive_sf_deltas)/1e6)
            if len(positive_sf_deltas) else None
        ),
        "sf_ack_ok_p90_interack_ms": (
            float(np.percentile(positive_sf_deltas, 90)/1e6)
            if len(positive_sf_deltas) else None
        ),
        "sf_ack_ok_distinct_frequencies_hz": acked_frequencies,
        "sf_ack_ok_muted_fast_scheduler_count": len(sf_muted),
        "sf_ack_ok_restore_while_zero_current_count": len(sf_restore_while_muted),
        "sf_ack_ok_active_frequency_count": len(sf_active),
        "sf_ack_ok_active_frequencies_hz": sorted({
            int(e["frequency_hz"]) for e in sf_active
            if e.get("frequency_hz") is not None
        }),
        "sf400_during_sc_zero_is_not_physical_400hz_evidence": True,
        "sf_ack_matching_warning": (
            "Event counts are not per-command identity matches. "
            "ACKs indicate protocol success; actual electrical output "
            "must be checked on physically observed CH1 intervals."
        ),
        "is_scope_recording": False,
    }


def analyze_one(root, record, mcu, events, ack_states, *, threshold_v,
                deadtime_ms, taxel, zoom_start, zoom_sec, window_s, step_s,
                tolerance_hz, tolerance_fraction):
    """Compute every statistic from this record's OWN CH1 and host overlap."""
    idx = int(record["index"])
    tag = f"record_{idx:05d}"
    output = root / "analysis" / tag
    output.mkdir(parents=True, exist_ok=True)
    t, volts = load_wave(root / record["channels"]["CH1"]["path"])
    starts = rising_pulses(t, volts, threshold_v, deadtime_ms/1000)
    pulses = pulse_statistics(t, starts, threshold_v)
    mapping = tentative_host_map(root, record_index=idx)
    overlap = coverage_overlaps(mapping, mcu, events)
    local_mcu = select_overlap_mcu(mcu, mapping)
    source = mcu.get("experiment_settings", {}).get("configuration", {}).get(
        "stimulation_source", {})
    taxel = int(source.get("taxel", 11) if taxel is None else taxel)
    if taxel not in range(12):
        raise ValueError("Taxel must be 0..11")
    enc = source.get("encoding", "sa_ii")
    local_rates = (local_mcu["sa_rate_hz"] if enc == "sa_ii"
                   else local_mcu["ra_rate_hz"])
    windows = sliding_physical_rates(starts, float(t[0]), float(t[-1]),
                                    window_s, step_s)
    compared = compare_physical_with_acks(
        windows, ack_states, mapping, float(t[0]), float(t[-1]),
        tolerance_hz, tolerance_fraction
    )
    dynamic = analyze_dynamic_cycles(
        starts, ack_states, mapping, float(t[0]), float(t[-1])
    )
    counts = Counter(row["assessment"] for row in compared)
    in_scope_events = (
        [event for event in events
         if ("pc_monotonic_ns" in event
             and overlap["first_sample_host_ns_est"]
             <= int(event["pc_monotonic_ns"])
             <= overlap["last_sample_host_ns_est"])]
        if mapping is not None else []
    )
    report = {
        "schema_version": 2,
        "run_id": root.name,
        "record_index": idx,
        "recording_status": "captured",
        "scope": {
            "ch1_path": record["channels"]["CH1"]["path"],
            "ch2_path": record["channels"]["CH2"]["path"],
            "samples": int(len(t)),
            "duration_s": float(t[-1]-t[0]),
            "scope_time_start_s": float(t[0]),
            "scope_time_end_s": float(t[-1]),
            "ch1_min_v": float(np.min(volts)),
            "ch1_max_v": float(np.max(volts)),
            "gray_frames": record.get("decoded_gray", {}).get("decoded_frame_count"),
            "pulse_detection": pulses,
        },
        "mcu": {
            "recording_id": mcu["recording_id"],
            "samples": int(len(local_mcu["taxel_pc_ns"])),
            "samples_are_full_stream": False,
            "taxel": taxel,
            "encoding": enc,
            "observed_update_rate_hz": (
                float(1e9/np.median(np.diff(local_mcu["taxel_pc_ns"])[
                    np.diff(local_mcu["taxel_pc_ns"]) > 0]))
                if len(local_mcu["taxel_pc_ns"]) > 1 and np.any(
                    np.diff(local_mcu["taxel_pc_ns"]) > 0) else None
            ),
            "requested_rate_min_hz": (
                float(np.min(local_rates[:, taxel])) if len(local_rates) else None
            ),
            "requested_rate_max_hz": (
                float(np.max(local_rates[:, taxel])) if len(local_rates) else None
            ),
        },
        "matrix32_commands": {
            "events": len(in_scope_events) if mapping else None,
            "counts_by_event": dict(Counter(
                e.get("event", "?") for e in in_scope_events)),
            "sf_ack_ok_count": (
                sum(e.get("event") == "sf_ack" and e.get("status") == "OK"
                    for e in in_scope_events) if mapping else None
            ),
            "sf_ack_ok_rate_per_scope_mapped_second": (
                sum(e.get("event") == "sf_ack" and e.get("status") == "OK"
                    for e in in_scope_events)
                / ((overlap["last_sample_host_ns_est"]
                    - overlap["first_sample_host_ns_est"]) / 1e9)
                if mapping is not None
                and overlap["last_sample_host_ns_est"]
                > overlap["first_sample_host_ns_est"] else None
            ),
            "events_are_full_stream": False,
            "scope_overlap_is_provisional": True,
        },
        "scope_to_host_overlap": overlap,
        "clock_mapping": mapping,
        "frequency_comparison": {
            "window_s": window_s, "step_s": step_s,
            "tolerance_hz": tolerance_hz,
            "tolerance_fraction": tolerance_fraction,
            "windows": compared,
            "by_ack_frequency": summarize_frequency_by_ack(compared, ack_states),
            "comparable_windows": counts["compatible_provisional"]
                                  + counts["discrepant_provisional"],
            "compatible_windows": counts["compatible_provisional"],
            "discrepant_windows": counts["discrepant_provisional"],
            "not_comparable_windows": counts["not_comparable"],
        },
        "dynamic_tracking": dynamic,
        "warnings": [
            "Record time starts at its OWN first oscilloscope sample; do not "
            "concatenate record time axes or include acquisition gaps.",
            "MCU and BLE counts are restricted to this record's provisionally "
            "mapped host coverage; without an independent clock map they are unknown.",
            "USB/Nano host clock offset remains uncalibrated; ACK time is not "
            "Matrix32 physical application time.",
        ],
    }
    # Optional human-reviewed S02-style CH1 onset inventory. Never promote
    # an unreviewed negative candidate or substitute legacy positive
    # threshold crossing for a source-locked approved onset.
    from oscilloscope.apply_pulse_review import load_approved_onsets
    review_dir = root / "analysis" / tag / "pulse_review"
    record_ch1 = root / record["channels"]["CH1"]["path"]
    if (review_dir / "review_approval.json").exists():
        try:
            approval, approved_onsets = load_approved_onsets(
                review_dir, record_ch1
            )
            approved_times = np.asarray(
                [float(row["time_s"]) for row in approved_onsets],
                dtype=float,
            )
            accepted_intervals = np.diff(approved_times)
            report["reviewed_negative_onsets"] = {
                "status": "OPERATOR_SOURCE_LOCKED_APPROVED",
                "reviewer": approval["reviewer"],
                "source_ch1_sha256": approval["source_ch1_sha256"],
                "onset_count": int(len(approved_times)),
                "interval_count": int(len(accepted_intervals)),
                "median_interval_ms": (
                    float(np.median(accepted_intervals)*1000)
                    if len(accepted_intervals) else None
                ),
                "median_inverse_period_hz": (
                    float(np.median(1.0/accepted_intervals))
                    if len(accepted_intervals) else None
                ),
                "approved_onsets_csv": str(review_dir / "approved_onsets.csv"),
                "approved_intervals_csv": str(review_dir / "approved_intervals.csv"),
                "ack_attribution": "NOT_YET_VALIDATED",
                "comment": (
                    "Only operator-approved CH1 events used here. "
                    "No cross-record intervals and no ACK/application "
                    "latency asserted."
                ),
            }
        except (ValueError, KeyError, OSError) as exc:
            report["reviewed_negative_onsets"] = {
                "status": "STALE_OR_INVALID_APPROVAL",
                "reason": str(exc),
                "onset_count": None,
            }
    else:
        report["reviewed_negative_onsets"] = {
            "status": "NOT_APPROVED",
            "onset_count": None,
            "ack_attribution": "NOT_ELIGIBLE",
        }
    report["plots"] = make_plots(
        root, t, volts, starts, local_mcu, in_scope_events, mapping,
        taxel, zoom_start, zoom_sec, record_index=idx,
    )
    report["plots"].append(plot_frequency_comparison(
        root, windows, compared, mapping, record_index=idx,
    ))
    report["plots"].append(plot_dynamic_tracking(
        output / "matrix32_dynamic_tracking.png",
        dynamic, ack_states, mapping, float(t[0]), float(t[-1])
    ))
    report["record_report"] = str(output / "report.md")
    report["record_summary"] = str(output / "summary.json")
    atomic_json(output / "summary.json", report)
    write_markdown_report(root, report, record_index=idx, per_record=True)
    print(f"[RECORD {idx}] scope {len(t):,} pts / {t[-1]-t[0]:.3f}s, "
          f"{pulses['positive_rising_events']} positive pulses, "
          f"MCU overlap {overlap['mcu_sample_count']}, "
          f"BLE overlap {overlap['ble_event_count']}, "
          f"dynamic {dynamic['compatible']}/{dynamic['classified']} "
          "compatible (provisional)")
    return report


def describe_gaps(reports):
    """Report unknown gaps explicitly; never connect two scope acquisitions."""
    gaps = []
    for left, right in zip(reports, reports[1:]):
        lm, rm = left["clock_mapping"], right["clock_mapping"]
        if lm is not None and rm is not None:
            gap = ((rm["first_sample_pc_monotonic_ns_est"]
                    - lm["last_sample_pc_monotonic_ns_est"])/1e9)
            if gap < 0:
                raise ValueError("Records overlap/reverse on mapped host axis")
            status = "provisional_mapped_gap"
        else:
            gap, status = None, "unknown_no_host_map"
        gaps.append({"after_record": left["record_index"],
                     "before_record": right["record_index"],
                     "estimated_gap_s": gap,
                     "status": status,
                     "scope_samples_present": False})
    return gaps


def session_markdown(root, session):
    lines = [
        f"# Integrated Matrix32 session: {session['run_id']}", "",
        "> Each scope acquisition was analyzed on its own local time axis. "
        "MCU/BLE logs are continuous streams. Cross-device and cross-record "
        "timing is provisional; no gaps have been interpolated.",
        "", "## At a glance", "",
        "| Metric | Value |", "| --- | ---: |",
        f"| Scope acquisitions | {session['scope_record_count']} |",
        f"| Sum of observed scope durations (excludes gaps) | "
        f"{session['total_observed_scope_s']:.3f} s |",
        f"| Sum of physical CH1 pulse candidates within records | "
        f"{session['total_scope_positive_events']} |",
        f"| Full MCU snapshots (counted once) | "
        f"{session['mcu_full_stream']['rate_snapshots']} |",
        f"| Full BLE trace events (counted once) | "
        f"{session['mcu_full_stream']['ble_events']} |",
        "", "## Each independent scope record", "",
        "| Record | Observed duration | Positive CH1 events | "
        "MCU samples in scope | BLE events in scope | Clock fit | Report |",
        "| ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for item in session["records"]:
        scope = item["scope"]
        overlap = item["scope_to_host_overlap"]
        index = item["record_index"]
        clock = item["clock_mapping"]
        status = clock["status"] if clock else "no host mapping"
        mcu_count = overlap["mcu_sample_count"]
        ble_count = overlap["ble_event_count"]
        lines.append(
            f"| {index} | {scope['duration_s']:.3f} s | "
            f"{scope['pulse_detection']['positive_rising_events']} | "
            f"{mcu_count if mcu_count is not None else 'unknown'} | "
            f"{ble_count if ble_count is not None else 'unknown'} | "
            f"{status} | [Record {index}](record_{index:05d}/report.md) |"
        )
    lines.extend([
        "", "## Operator-approved negative pulse detection", "",
        f"Approved records: "
        f"{session.get('human_approved_scope_records', 0)} of "
        f"{session['scope_record_count']}. "
        f"Total onsets from these approved records ONLY: "
        f"{session.get('human_approved_onsets_only', 0)}. "
        "Unreviewed records are excluded, NOT treated as zero pulses.",
        "No approved onset interval spans two scope recordings. "
        "Review approval is not clock or BLE-application-time qualification.",
        "", "## Gaps between acquisitions", "",
        "| After | Before | Estimated unobserved time | Status |",
        "| ---: | ---: | ---: | --- |",
    ])
    if not session["inter_record_gaps"]:
        lines.append("| — | — | None: one scope acquisition | — |")
    for gap in session["inter_record_gaps"]:
        value = (f"{gap['estimated_gap_s']:.3f} s"
                 if gap["estimated_gap_s"] is not None else "unknown")
        lines.append(
            f"| {gap['after_record']} | {gap['before_record']} | "
            f"{value} | {gap['status']} |"
        )
    lines += [
        "", "## Continuous MCU and Matrix32 command logs", "",
        f"The MCU recorded **{session['mcu_full_stream']['rate_snapshots']} "
        f"rate snapshots** over "
        f"**{session['mcu_full_stream']['arrival_span_s']:.3f} s** of "
        "PC arrival time, which is **not** the same as total scope coverage.",
        f"The Matrix32 BLE trace contains **"
        f"{session['mcu_full_stream']['ble_events']} events** across the "
        "whole controller lifetime. These are counted only once here.",
        "", "### PC-native acquisition and BLE command statistics", "",
        f"- Median MCU arrival interval: "
        f"{session['mcu_full_stream']['median_positive_arrival_interval_ms']} ms.",
        f"- MCU arrival interval p10 / p90: "
        f"{session['mcu_full_stream'].get('arrival_interval_p10_ms')} / "
        f"{session['mcu_full_stream'].get('arrival_interval_p90_ms')} ms.",
        f"- MCU intervals >2× median: "
        f"{session['mcu_full_stream'].get('arrival_intervals_above_twice_median_count')}.",
        f"- SF requested events: "
        f"{session['mcu_full_stream'].get('sf_requested_event_count')}.",
        f"- SF successful ACKs (status OK): "
        f"{session['mcu_full_stream'].get('sf_ack_ok_count')}.",
        f"- SF failed events: "
        f"{session['mcu_full_stream'].get('sf_failed_event_count')}.",
        f"- Successful SF ACKs per second of MCU recording: "
        f"{session['mcu_full_stream'].get('sf_ack_ok_rate_per_mcu_recording_second')}.",
        f"- Median inter-ACK interval: "
        f"{session['mcu_full_stream'].get('sf_ack_ok_median_interack_ms')} ms.",
        f"- Distinct successful SF ACK frequencies (includes muted transport): "
        f"{session['mcu_full_stream'].get('sf_ack_ok_distinct_frequencies_hz')}.",
        f"- Active-request SF ACK frequencies (exclude SC0 mute/restore): "
        f"{session['mcu_full_stream'].get('sf_ack_ok_active_frequencies_hz')}.",
        f"- Muted fast-scheduler SF ACKs (SC=0, SF=400): "
        f"{session['mcu_full_stream'].get('sf_ack_ok_muted_fast_scheduler_count')}.",
        "These counts are host-native telemetry, not a confirmation that "
        "each acknowledged state appeared on CH1. Scope-window overlap "
        "rates require the provisional time mapping.",
        "", "![Independent full MCU stream](mcu_full_stream.png)", "",
        "## Command versus physical measurement", "",
        "Each record report compares its OWN CH1 pulse events with the "
        "BLE ACK states projected onto that record using its OWN provisional "
        "Nano-to-PC clock fit. No scope pulses or sample times are joined "
        "across record boundaries. No combined single-stream latency "
        "or instantaneous-frequency fit is reported.",
        "", "## Interpretive limitations", "",
        "- Per-record MCU and BLE overlap counts depend on provisional "
        "scope-to-host mapping, which has uncalibrated USB asymmetry.",
        "- The total scope duration is the sum of acquired durations, "
        "never the wall-clock span including idle pauses.",
        "- Total pulse event count is an integer sum of independent records, "
        "not proof of continuous physical output between them.",
        "- For pulse frequency accuracy, inspect individual record reports "
        "or acquire fixed-frequency plateaus.",
        "", "## Files", "",
        "- [Session summary JSON](integrated_summary.json)",
    ]
    for item in session["records"]:
        n = item["record_index"]
        lines.append(f"- [Record {n} summary JSON](record_{n:05d}/summary.json)")
    output = root / "analysis" / "integrated_report.md"
    output.write_text("\n".join(lines)+"\n", encoding="utf-8")
    return output


def analyze_session(root, meta, *, threshold_v=2, deadtime_ms=1,
                    taxel=None, zoom_start=None, zoom_sec=0.5, show=False,
                    frequency_window_s=1.0, frequency_step_s=0.25,
                    frequency_tolerance_hz=3.0,
                    frequency_tolerance_fraction=0.05):
    root = Path(root)
    records = record_boundaries(meta.get("records", []))
    mcu_dir = root / "mcu_matrix32"
    mcu_files = sorted(mcu_dir.glob("*_tactile_rates.csv"))
    if len(mcu_files) != 1:
        raise ValueError(
            f"Expected exactly one continuous MCU rate recording per integrated "
            f"session, found {len(mcu_files)}. Refusing to silently select a "
            f"latest file or concatenate distinct MCU recordings."
        )
    rid = choose_recording(mcu_dir)
    mcu = load_recordings(mcu_dir, rid)
    if mcu["mode"] != "rates" or "taxel_pc_ns" not in mcu:
        raise ValueError("Full MCU stream requires PC-timestamped rate snapshots")
    trace_files = list(mcu_dir.glob("*_matrix32_commands.jsonl"))
    if len(trace_files) > 1:
        raise ValueError("Ambiguous multiple MCU Matrix32 command traces")
    events = read_events(trace_files[0]) if trace_files else []
    ack_states = acknowledged_frequency_states(events)
    output = root / "analysis"
    output.mkdir(exist_ok=True)
    full_plot = output / "mcu_full_stream.png"
    plot_traces(mcu, full_plot, selected_taxel=taxel, x_axis="pc")
    from oscilloscope.approved_frequency_analysis import (
        analyze_approved_record, plot_session_summary, session_report,
    )
    reports = []
    approved_summaries = []
    for rec in records:
        report = analyze_one(
            root, rec, mcu, events, ack_states,
            threshold_v=threshold_v, deadtime_ms=deadtime_ms, taxel=taxel,
            zoom_start=zoom_start, zoom_sec=zoom_sec,
            window_s=frequency_window_s, step_s=frequency_step_s,
            tolerance_hz=frequency_tolerance_hz,
            tolerance_fraction=frequency_tolerance_fraction,
        )
        # Source-locked operator-approved negative onsets are the PRIMARY
        # physical frequency evidence. Keep positive-edge estimates only as
        # a separately labeled historical diagnostic for comparison.
        index = int(rec["index"])
        approval_path = (root / "analysis" / f"record_{index:05d}"
                         / "pulse_review" / "review_approval.json")
        if approval_path.exists():
            selected_taxel = int(report["mcu"]["taxel"])
            encoding = report["mcu"]["encoding"]
            summary = analyze_approved_record(
                root, rec, mcu, ack_states, report["clock_mapping"],
                selected_taxel, encoding,
                report["scope"]["scope_time_start_s"],
                report["scope"]["scope_time_end_s"],
            )
            report["approved_frequency"] = summary
            approved_summaries.append(summary)
            atomic_json(root / "analysis" / f"record_{index:05d}"
                        / "summary.json", report)
            file = root / "analysis" / f"record_{index:05d}" / "report.md"
            legacy = file.read_text(encoding="utf-8")
            file.write_text(
                f"# Record {index}: physical frequency from approved negative onsets\n\n"
                f"**Primary approved analysis:** "
                f"[Full metrics, provisional MCU/ACK plots and intervals]"
                f"(approved_frequency_report.md). "
                f"**{summary['onset_count']} approved onsets; "
                f"{summary['interval_count']} approved intervals.**\n\n"
                f"Below is the *legacy exploratory positive-edge analysis*, "
                f"not a substitute for approved negative onset evidence.\n\n"
                + legacy, encoding="utf-8")
            print(f"[APPROVED {index}] {summary['onset_count']} verified "
                  f"negative onsets / {summary['interval_count']} intervals; "
                  f"median inverse period "
                  f"{summary['inverse_period_hz']['median']} Hz "
                  "(MCU/ACK comparison provisional)")
        reports.append(report)
    if approved_summaries:
        plot_session_summary(output / "approved_session_overview.png",
                             approved_summaries)
    gaps = describe_gaps(reports)
    session = {
        "schema_version": 2,
        "run_id": meta["run_id"],
        "session_status": meta.get("status"),
        "scope_record_count": len(reports),
        "total_observed_scope_s": float(sum(
            r["scope"]["duration_s"] for r in reports)),
        "total_scope_positive_events": sum(
            r["scope"]["pulse_detection"]["positive_rising_events"]
            for r in reports),
        "physical_pulse_source": (
            "OPERATOR_APPROVED_S02_NEGATIVE_ONSETS" if approved_summaries
            else "NO_APPROVED_NEGATIVE_ONSETS"),
        "approved_negative_onset_record_count": len(approved_summaries),
        "approved_negative_onset_count": sum(
            r["onset_count"] for r in approved_summaries),
        "approved_within_record_interval_count": sum(
            r["interval_count"] for r in approved_summaries),
        "approved_frequency_records": approved_summaries,
        "mcu_full_stream": summarize_full_stream(mcu, events),
        "records": [
            {"record_index": r["record_index"], "scope": r["scope"],
             "mcu": r["mcu"], "matrix32_commands": r["matrix32_commands"],
             "clock_mapping": r["clock_mapping"],
             "scope_to_host_overlap": r["scope_to_host_overlap"],
             "reviewed_negative_onsets": r.get("reviewed_negative_onsets", {
                 "status": "NOT_APPROVED", "onset_count": None,
             }),
             "record_report": r["record_report"],
             "record_summary": r["record_summary"],
             "dynamic_summary": {
                 "classified": r["dynamic_tracking"]["classified"],
                 "compatible": r["dynamic_tracking"]["compatible"],
                 "discrepant": r["dynamic_tracking"]["discrepant"],
                 "not_comparable": r["dynamic_tracking"]["not_comparable"],
             }}
            for r in reports
        ],
        "human_approved_scope_records": sum(
            r.get("reviewed_negative_onsets", {}).get("status")
            == "OPERATOR_SOURCE_LOCKED_APPROVED" for r in reports
        ),
        "human_approved_onsets_only": sum(
            int(r["reviewed_negative_onsets"]["onset_count"])
            for r in reports
            if r.get("reviewed_negative_onsets", {}).get("status")
            == "OPERATOR_SOURCE_LOCKED_APPROVED"
        ),
        "human_approved_onsets_coverage": (
            "Partial unless every independent record was human reviewed. "
            "Never infer pulse rates across gaps."
        ),
        "inter_record_gaps": gaps,
        "continuous_time_series_were_concatenated": False,
        "physical_samples_interpolated_across_gaps": False,
        "cross_clock_alignment_verified": False,
        "warnings": [
            "No across-gap instantaneous frequency or pulse interval "
            "was computed; full MCU/ACK logs are counted once.",
            "Record clock maps are provisional and cannot establish "
            "sub-millisecond absolute timing across devices.",
        ],
    }
    atomic_json(output / "integrated_summary.json", session)
    report = session_markdown(root, session)
    if approved_summaries:
        original = Path(report).read_text(encoding="utf-8")
        Path(report).write_text(
            f"# Integrated Matrix32 session: {session['run_id']}\n\n"
            + session_report(approved_summaries, len(records))
            + "\n---\n\n## Supporting MCU/BLE telemetry and legacy positive-edge diagnostics\n\n"
            + original, encoding="utf-8")
    print(f"[SESSION] {len(reports)} independent scope record(s); "
          f"{session['total_observed_scope_s']:.3f}s observed total; "
          f"{len(gaps)} explicit inter-record gap(s)")
    print(f"[SESSION] MCU full stream: "
          f"{session['mcu_full_stream']['rate_snapshots']} snapshots; "
          f"BLE {session['mcu_full_stream']['ble_events']} events")
    print(f"[SESSION] Report: {report}")
    return session
