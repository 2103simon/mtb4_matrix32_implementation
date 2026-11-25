"""Analyze distinct scope acquisitions independently, then summarize a session.

Offline-only. Scope time axes are local to EACH record. The continuous MCU
and BLE traces are never concatenated with scope samples. Host-time mapping
is accepted only for matching per-record clock fits, and remains provisional.
"""
from __future__ import annotations
from experiment_analysis.paths import analysis_root

from collections import Counter
import json
import math
from pathlib import Path

import numpy as np

from analyze_mcu_recording import choose_recording, load_recordings, plot_traces
from oscilloscope.record_scope import atomic_json
from experiment_analysis.analyze_integrated_matrix32 import (
    acknowledged_frequency_states, load_wave, read_events, tentative_host_map,
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


def analyze_one(root, record, mcu, events, ack_states, *, taxel=None):
    """Use only source-locked approved S02 negative onsets for CH1 metrics.

    No positive-threshold event detector, no positive-edge frequency plots,
    no fallback to unreviewed data. Absence or stale approval fails closed.
    """
    from experiment_analysis.approved_frequency_analysis import analyze_approved_record
    idx = int(record["index"])
    output = analysis_root(root) / f"record_{idx:05d}"
    output.mkdir(parents=True, exist_ok=True)
    approval_path = output / "pulse_review" / "review_approval.json"
    if not approval_path.exists():
        raise ValueError(
            f"Record {idx}: source-locked negative-onset approval missing: "
            f"{approval_path}. Run review_pulse_detection.py and "
            "apply_pulse_review.py with operator review before integration."
        )
    ch1 = record["channels"]["CH1"]
    path = root / ch1["path"]
    # Read exact saved scope time boundaries, not host estimates. This is a
    # finite 5-million-sample recording; no new threshold detection is run.
    t, volts = load_wave(path)
    start_s, end_s = float(t[0]), float(t[-1])
    mapping = tentative_host_map(root, record_index=idx)
    overlap = coverage_overlaps(mapping, mcu, events)
    local_mcu = select_overlap_mcu(mcu, mapping)
    source = mcu.get("experiment_settings", {}).get("configuration", {}).get(
        "stimulation_source", {})
    selected_taxel = int(source.get("taxel", 11) if taxel is None else taxel)
    encoding = source.get("encoding", "sa_ii")
    if selected_taxel not in range(12):
        raise ValueError("Taxel must be 0..11")
    if encoding not in ("sa_ii", "ra_i"):
        raise ValueError(f"Unsupported stimulation encoding: {encoding}")
    rates = (local_mcu["sa_rate_hz"] if encoding == "sa_ii"
             else local_mcu["ra_rate_hz"])
    n = len(local_mcu["taxel_pc_ns"])
    deltas = np.diff(local_mcu["taxel_pc_ns"]) if n > 1 else np.array([])
    positive_deltas = deltas[deltas > 0]
    in_scope_events = [
        e for e in events
        if mapping is not None and "pc_monotonic_ns" in e
        and overlap["first_sample_host_ns_est"]
            <= int(e["pc_monotonic_ns"])
            <= overlap["last_sample_host_ns_est"]
    ]
    approved = analyze_approved_record(
        root, record, mcu, ack_states, mapping, selected_taxel,
        encoding, start_s, end_s,
    )
    report = {
        "schema_version": 3,
        "run_id": root.name, "record_index": idx, "recording_status": "captured",
        "physical_detection_method": "OPERATOR_APPROVED_S02_NEGATIVE_ONSETS_ONLY",
        "scope": {
            "ch1_path": ch1["path"],
            "ch2_path": record["channels"]["CH2"]["path"],
            "samples": int(len(t)),
            "duration_s": end_s-start_s,
            "scope_time_start_s": start_s, "scope_time_end_s": end_s,
            "ch1_min_v": float(np.min(volts)), "ch1_max_v": float(np.max(volts)),
            "gray_frames": record.get("decoded_gray", {}).get("decoded_frame_count"),
        },
        "mcu": {
            "recording_id": mcu["recording_id"], "samples": int(n),
            "taxel": selected_taxel, "encoding": encoding,
            "observed_update_rate_hz": (
                float(1e9/np.median(positive_deltas))
                if len(positive_deltas) else None),
            "requested_rate_min_hz": (
                float(np.min(rates[:, selected_taxel])) if len(rates) else None),
            "requested_rate_max_hz": (
                float(np.max(rates[:, selected_taxel])) if len(rates) else None),
        },
        "matrix32_commands": {
            "events": len(in_scope_events) if mapping else None,
            "counts_by_event": dict(Counter(e.get("event", "?")
                                            for e in in_scope_events)),
            "scope_overlap_is_provisional": True,
        },
        "scope_to_host_overlap": overlap,
        "clock_mapping": mapping,
        "approved_frequency": approved,
        "reviewed_negative_onsets": {
            "status": "OPERATOR_SOURCE_LOCKED_APPROVED",
            "onset_count": approved["onset_count"],
            "interval_count": approved["interval_count"],
        },
        "warnings": [
            "All physical frequency results use approved negative onsets only.",
            "No intervals are built across scope acquisition gaps.",
            "Host/ACK alignment and command application time remain unverified.",
        ],
        "record_report": str(output / "report.md"),
        "record_summary": str(output / "summary.json"),
    }
    atomic_json(output / "summary.json", report)
    (output / "report.md").write_text(
        f"# Record {idx}: operator-approved negative-onset analysis\n\n"
        f"**{approved['onset_count']} approved onsets, "
        f"{approved['interval_count']} approved within-record intervals.**\n\n"
        f"[Full approved frequency analysis and figures]"
        f"(approved_frequency_report.md)\n\n"
        f"[Human-reviewed CH1 trace and per-pulse decisions]"
        f"(pulse_review/report.md)\n\n"
        f"Source: source-locked S02 negative-event detector and "
        f"operator-reviewed pulse inventory. No positive-edge fallback.\n",
        encoding="utf-8",
    )
    print(f"[APPROVED RECORD {idx}] {len(t):,} CH1 samples / "
          f"{end_s-start_s:.3f} s; {approved['onset_count']} onsets; "
          f"{approved['interval_count']} intervals; median inverse period "
          f"{approved['inverse_period_hz']['median']} Hz (no positive-edge "
          "analysis; host/ACK attribution provisional)")
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
    """Approved-only session narrative; MCU/BLE host telemetry remains separate."""
    from experiment_analysis.approved_frequency_analysis import session_report
    rows = session["approved_frequency_records"]
    lines = [
        f"# Integrated Matrix32 session: {session['run_id']}", "",
        "**Primary physical evidence: source-locked, operator-approved "
        "negative CH1 onsets only.** No positive-edge detector is run.",
        "",
        session_report(rows, session["scope_record_count"]),
        "## Continuous MCU and BLE telemetry (host-native)", "",
        "| Metric | Value |", "| --- | ---: |",
        f"| Scope records | {session['scope_record_count']} |",
        f"| Total acquired CH1 duration | "
        f"{session['total_observed_scope_s']:.3f} s |",
        f"| Full MCU snapshots | "
        f"{session['mcu_full_stream']['rate_snapshots']} |",
        f"| Full BLE trace events | "
        f"{session['mcu_full_stream']['ble_events']} |",
        f"| SF requests | "
        f"{session['mcu_full_stream']['sf_requested_event_count']} |",
        f"| Successful SF ACKs | "
        f"{session['mcu_full_stream']['sf_ack_ok_count']} |",
        f"| Failed SF events | "
        f"{session['mcu_full_stream']['sf_failed_event_count']} |",
        f"| Non-OK SF ACKs | "
        f"{session['mcu_full_stream']['sf_ack_non_ok_count']} |",
        f"| Muted fast scheduler SF ACKs | "
        f"{session['mcu_full_stream']['sf_ack_ok_muted_fast_scheduler_count']} |",
        "",
        "BLE acknowledgments indicate protocol success but do not prove "
        "physical output or exact timing of application.",
        "", "![Continuous MCU rate data](mcu_full_stream.png)", "",
        "## Unobserved gaps between independent acquisitions", "",
        "| After record | Before record | Estimated duration (s) | Status |",
        "| ---: | ---: | ---: | --- |",
    ]
    for gap in session["inter_record_gaps"]:
        duration = (f"{gap['estimated_gap_s']:.3f}"
                    if gap['estimated_gap_s'] is not None else "unknown")
        lines.append(
            f"| {gap['after_record']} | {gap['before_record']} | "
            f"{duration} | {gap['status']} |"
        )
    lines.extend([
        "", "## Why are some clocks unqualified?", "",
        "See the [per-record clock qualification audit]"
        "(clock_qualification_report.md) for **every failed criterion**, "
        "actual Gray frame residuals, USB clock-fit diagnostics and "
        "outlier sensitivity. Diagnostic alternative fits never replace "
        "the original maps or silently qualify a record.",
        "", "## Evidence limitations", "",
        "- Each approved interval uses two consecutive negative CH1 onsets "
        "within the same acquired record.",
        "- Absence of observed pulses is a statement at the detector and "
        "scope sensitivity, not a current-off certification.",
        "- All MCU/BLE association and transition assessments depend on "
        "provisional cross-clock alignment; no physical apply latency proved.",
        "- No physical event time is interpolated across acquisition gaps.",
        "", "## Machine-readable evidence", "",
        "- [Session summary](integrated_summary.json)",
        "- [Per-record approved interval tables and plots]"
        "(record_00001/approved_frequency_report.md)",
    ])
    out = analysis_root(root) / "integrated_report.md"
    out.write_text("\n".join(lines)+"\n", encoding="utf-8")
    return str(out)


def analyze_session(root, meta, *, taxel=None, show=False):
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
    output = analysis_root(root)
    output.mkdir(exist_ok=True)
    full_plot = output / "mcu_full_stream.png"
    plot_traces(mcu, full_plot, selected_taxel=taxel, x_axis="pc")
    from experiment_analysis.approved_frequency_analysis import plot_session_summary
    reports = []
    approved_summaries = []
    for rec in records:
        report = analyze_one(root, rec, mcu, events, ack_states, taxel=taxel)
        reports.append(report)
        approved_summaries.append(report["approved_frequency"])
    plot_session_summary(output / "approved_session_overview.png",
                         approved_summaries)
    gaps = describe_gaps(reports)
    session = {
        "schema_version": 3,
        "run_id": meta["run_id"],
        "session_status": meta.get("status"),
        "scope_record_count": len(reports),
        "total_observed_scope_s": float(sum(
            r["scope"]["duration_s"] for r in reports)),
        "physical_pulse_source": "OPERATOR_APPROVED_S02_NEGATIVE_ONSETS",
        "approved_negative_onset_record_count": len(approved_summaries),
        "approved_negative_onset_count": sum(
            r["onset_count"] for r in approved_summaries),
        "approved_within_record_interval_count": sum(
            r["interval_count"] for r in approved_summaries),
        "approved_frequency_records": approved_summaries,
        "mcu_full_stream": summarize_full_stream(mcu, events),
        "records": [{
            "record_index": r["record_index"],
            "scope": r["scope"], "mcu": r["mcu"],
            "matrix32_commands": r["matrix32_commands"],
            "clock_mapping": r["clock_mapping"],
            "scope_to_host_overlap": r["scope_to_host_overlap"],
            "reviewed_negative_onsets": r["reviewed_negative_onsets"],
            "approved_frequency": r["approved_frequency"],
            "record_report": r["record_report"],
            "record_summary": r["record_summary"],
        } for r in reports],
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
    # Diagnose, but NEVER silently recalibrate, the original Gray/Nano/PC
    # synchronization. Missing epoch metadata is a documented limitation.
    from oscilloscope.clock_qualification_diagnostics import diagnose
    if (root / "epoch_clock_analysis.json").exists():
        clock_report, clock_records = diagnose(root)
        session["clock_qualification_diagnostics"] = {
            "report": str(clock_report),
            "records": [{
                "record_index": x["record_index"],
                "status": x["original_status"],
                "failed_checks": x["failed_checks"],
                "robust_candidate": x["robust_candidate"],
            } for x in clock_records],
            "original_clock_maps_untouched": True,
        }
    else:
        session["clock_qualification_diagnostics"] = {
            "status": "NO_EPOCH_CLOCK_ANALYSIS",
            "original_clock_maps_untouched": True,
        }
    atomic_json(output / "integrated_summary.json", session)
    report = session_markdown(root, session)
    print(f"[SESSION] {len(reports)} independent scope record(s); "
          f"{session['total_observed_scope_s']:.3f}s observed total; "
          f"{len(gaps)} explicit inter-record gap(s)")
    print(f"[SESSION] MCU full stream: "
          f"{session['mcu_full_stream']['rate_snapshots']} snapshots; "
          f"BLE {session['mcu_full_stream']['ble_events']} events")
    print(f"[SESSION] Report: {report}")
    return session
