"""S01/S02-style pulse reports and source-locked human review workflow.

Offline only. Operator edits review_decisions.csv and (optionally)
manual_additions.csv; no generated candidate CSV is changed by approval.
The review is locked to the exact original CH1 waveform and detector tables.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np

from plotting.envelopes import minmax_envelope
from plotting.waveforms import plot_traces
from oscilloscope.record_scope import atomic_json

CANDIDATE_FIELDS = ("id", "kind", "sample_index", "time_s",
                    "detector_status", "decision", "note")
ADDITION_FIELDS = ("id", "time_s", "decision", "note")
SOURCE_TABLES = ("accepted_onsets.csv", "rejected_onsets.csv",
                 "suppressed_onsets.csv", "periods_unverified.csv",
                 "threshold_sensitive_onsets.csv")


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def csv_rows(path):
    with Path(path).open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def write_csv(path, rows, fields):
    with Path(path).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def review_candidates(result):
    """Stable event IDs: P accepted, R rejected, S suppressed."""
    items = []
    for kind, prefix, key in (
        ("accepted", "P", "accepted_pulses"),
        ("rejected", "R", "rejected"),
        ("suppressed", "S", "suppressed"),
    ):
        for index, event in enumerate(result[key], 1):
            items.append({
                "id": f"{prefix}{index:05d}", "kind": kind,
                "sample_index": int(event["sample_index"]),
                "time_s": float(event["time_s"]),
                "detector_status": event["status"],
            })
    return items


def make_scaffold(output, source_ch1, result):
    """Create editable decisions once; never destroy existing operator edits."""
    output, source_ch1 = Path(output), Path(source_ch1)
    candidates = review_candidates(result)
    manifest = {
        "schema": "matrix32_pulse_review_v1",
        "source_ch1_sha256": sha256(source_ch1),
        "source_ch1_relative_path": result["source_ch1"],
        "detector": result["detector"],
        "record_index": result["record_index"],
        "source_tables_sha256": {
            name: sha256(output / name) for name in SOURCE_TABLES
        },
        "candidates": candidates,
        "decision_semantics": (
            "INCLUDE = observed genuine pulse; EXCLUDE = not an independent "
            "pulse; blank = unresolved unless accepted candidates are "
            "explicitly signed off using --confirm-accepted. Operator "
            "must classify every rejected/suppressed crossing."
        ),
    }
    manifest_path = output / "review_manifest.json"
    decisions = output / "review_decisions.csv"
    additions = output / "manual_additions.csv"
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous != manifest:
            if decisions.exists() or additions.exists():
                raise RuntimeError(
                    "Source waveform or generated detector tables changed; "
                    "existing human review is now stale. Preserve the old "
                    "review, then generate a fresh report directory."
                )
        else:
            if not decisions.exists() or not additions.exists():
                raise RuntimeError("Existing review manifest has missing review files")
    else:
        if decisions.exists() or additions.exists():
            raise RuntimeError("Review tables exist without source-lock manifest")
        atomic_json(manifest_path, manifest)
        write_csv(decisions, (
            {**candidate, "decision": "", "note": ""}
            for candidate in candidates
        ), CANDIDATE_FIELDS)
        write_csv(additions, [], ADDITION_FIELDS)
    return manifest


def _diagnostic_rejections(t, v, result):
    """Build S02 figure arguments without assuming a fixed nominal period."""
    import bisect
    threshold = result.get("threshold", {})
    baseline = float(threshold.get("baseline_v", 0))
    threshold_v = float(threshold.get("threshold_v", -1))
    drop = baseline - threshold_v
    indexed = (
        [(row, f"R{i:05d}") for i, row in
         enumerate(result.get("rejected", []), 1)]
        + [(row, f"S{i:05d}") for i, row in
           enumerate(result.get("suppressed", []), 1)]
    )
    indexed.sort(key=lambda pair: (pair[0]["time_s"], pair[0]["sample_index"]))
    accepted = sorted(row["time_s"] for row in result["accepted_pulses"])
    rows = []
    for row, event_id in indexed:
        center, index = float(row["time_s"]), int(row["sample_index"])
        end = min(len(t), bisect.bisect_left(t, center + .00035))
        window = v[index:end] if end > index else v[index:index+1]
        trough = float(min(window))
        peak = float(max(window))
        a = bisect.bisect_left(accepted, center)
        neighbors = [accepted[i+1]-accepted[i] for i in
                     range(max(0, a-4), min(len(accepted)-1, a+3))]
        typical = float(np.median(neighbors)) if neighbors else .01
        quiet_band = 1.5 * max(
            1., 6.*float(threshold.get("noise_sigma_v", 0.)),
            .25*float(threshold.get("threshold_drop_v", 0.)),
        )
        j, preceding_quiet = index - 1, 0
        while j >= 0 and abs(v[j] - baseline) <= quiet_band:
            preceding_quiet += 1
            j -= 1
        sample_dt_us = float(np.median(np.diff(t[:min(1001, len(t))])) * 1e6)
        rows.append({
            "id": event_id,
            "sample_index": index, "time_s": center,
            "voltage_v": float(row["voltage_v"]),
            "original_status": row["status"],
            "diagnostic_tag": ("POSSIBLE_BIPHASIC_PULSE_REVIEW"
                               if trough < baseline - max(12., 1.5*drop)
                               and peak > baseline + max(8., drop)
                               else "SECONDARY_OR_WEAK_CROSSING_REVIEW"),
            "quiet_before_us": preceding_quiet * sample_dt_us, "negative_min_v": trough,
            "positive_max_v": peak, "expected_slot_s": None,
            "delta_to_expected_us": None,
            "figure": None,
            "local_neighbor_period_s": typical,
        })
    for row in rows:
        row["figure"] = (
            f"figures/rejected/{row['id']}_t"
            f"{row['time_s'] * 1e3:010.4f}ms.png"
        )
    return rows


def plot_report(output, t, volts, result):
    """Original S02 min/max overview, S01 raw trace helper, S02 detail plots."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from analysis_primitives.s02_rejection_review import save_rejection_figures
    output = Path(output)
    figures = output / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    times = t.tolist()
    values = volts.tolist()
    shown = []
    xs, lo, hi = minmax_envelope(times, values)
    fig, ax = plt.subplots(figsize=(14, 5))
    ax.fill_between(xs, lo, hi, alpha=.65,
                    label="CH1 original sample min/max envelope")
    for key, label, marker in (
        ("accepted_pulses", "accepted candidate", "x"),
        ("rejected", "rejected transition", "o"),
        ("suppressed", "suppressed crossing", "+"),
    ):
        group = result[key]
        if group:
            ax.scatter([row["time_s"] for row in group],
                       [row["voltage_v"] for row in group],
                       s=22 if key != "accepted_pulses" else 8,
                       marker=marker, label=label)
    threshold = result.get("threshold", {})
    if "threshold_v" in threshold:
        ax.axhline(threshold["threshold_v"], ls="--", lw=.8,
                   label="negative detection threshold")
    ax.set(xlabel="Scope-local time (s)", ylabel="CH1 voltage (V)",
           title=f"Record {result['record_index']}: full pulse inventory (unreviewed)")
    ax.grid(alpha=.2)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(figures / "waveform_full.png", dpi=160)
    plt.close(fig)
    shown.append("waveform_full.png")

    # Identical source evidence and candidate markers, but present the
    # nominal 20-s acquisition as four independent 5-s views. Display bins
    # retain min/max sample extrema; this does NOT downsample analysis,
    # move event timestamps, alter decisions, or cross scope-record gaps.
    panel_seconds = 5.0
    window_start = float(t[0])
    window_end = float(t[-1])
    n_panels = max(1, int(np.ceil((window_end-window_start)/panel_seconds)))
    fig, axes = plt.subplots(
        n_panels, 1, figsize=(16, max(4, 3.7*n_panels)),
        sharey=True, layout="constrained")
    axes = np.atleast_1d(axes)
    groups = (
        ("accepted_pulses", "accepted candidate", "x", 10, "C1"),
        ("rejected", "rejected transition", "o", 23, "C3"),
        ("suppressed", "suppressed crossing", "+", 26, "C4"),
    )
    for panel, ax in enumerate(axes):
        left = window_start + panel*panel_seconds
        right = min(left+panel_seconds, window_end)
        lo_idx = int(np.searchsorted(t, left, side="left"))
        hi_idx = int(np.searchsorted(t, right, side="right"))
        if hi_idx > lo_idx:
            px, low, high = minmax_envelope(
                t[lo_idx:hi_idx].tolist(), volts[lo_idx:hi_idx].tolist(),
                bins=1700)
            ax.fill_between(px, low, high, alpha=.6,
                            label="CH1 original min/max envelope")
        for key, label, marker, size, color in groups:
            points = [event for event in result[key]
                      if left <= float(event["time_s"]) <
                      (right if panel < n_panels-1 else right+1e-9)]
            if points:
                ax.scatter(
                    [event["time_s"] for event in points],
                    [event["voltage_v"] for event in points],
                    marker=marker, s=size, color=color,
                    label=f"{label} (n={len(points)})", zorder=3)
        if "threshold_v" in threshold:
            ax.axhline(
                threshold["threshold_v"], ls="--", lw=.8,
                label="negative detection threshold")
        ax.set_xlim(left, left+panel_seconds)
        ax.set_ylabel("CH1 voltage (V)")
        ax.set_title(
            f"Record {result['record_index']}: "
            f"{left-window_start:.0f}–{left-window_start+panel_seconds:.0f} s "
            "relative to record start")
        ax.grid(alpha=.2)
        ax.legend(fontsize=8, loc="best")
    axes[-1].set_xlabel("Scope-local time (s)")
    fig.savefig(figures / "waveform_four_5s_panels.png", dpi=170)
    plt.close(fig)
    shown.append("waveform_four_5s_panels.png")

    # S01 original raw-sample helper: first candidate onset in detail.
    if result["accepted_pulses"]:
        center = result["accepted_pulses"][0]["time_s"]
        a = int(np.searchsorted(t, center - .00025))
        b = int(np.searchsorted(t, center + .0006))
        if b - a >= 3:
            fig, ax = plt.subplots(figsize=(11, 4))
            plot_traces(ax, [{
                "time_us": (t[a:b] * 1e3).tolist(),
                "values": volts[a:b].tolist(),
                "color": "C0", "label": "CH1 original samples",
            }], xlim=(float(t[a])*1e3, float(t[b-1])*1e3),
                xlabel="Scope-local time (ms)", ylabel="CH1 voltage (V)",
                title="First negative onset candidate; raw samples")
            ax.axvline(center*1e3, ls="--", label="candidate onset")
            ax.legend()
            fig.tight_layout()
            fig.savefig(figures / "waveform_event_detail.png", dpi=160)
            plt.close(fig)
            shown.append("waveform_event_detail.png")

    # S01-style unmodified raw waveforms for EVERY relaxed-quiet accepted
    # onset, not only for rejected/suppressed crosses.
    for pulse in result["accepted_pulses"]:
        if pulse["status"] != "RELAXED_QUIET_REVIEW":
            continue
        center = float(pulse["time_s"])
        a = int(np.searchsorted(t, center-.00045))
        b = int(np.searchsorted(t, center+.00065))
        if b-a < 3:
            continue
        accepted_dir = figures / "flagged_accepted"
        accepted_dir.mkdir(parents=True, exist_ok=True)
        fig, ax = plt.subplots(figsize=(11, 4))
        plot_traces(ax, [{
            "time_us": (t[a:b]*1e3).tolist(),
            "values": volts[a:b].tolist(),
            "color": "C0", "label": "original CH1 samples",
        }], xlim=(float(t[a])*1e3, float(t[b-1])*1e3),
            xlabel="Scope-local time (ms)", ylabel="CH1 voltage (V)",
            title=f"{pulse['id']}: relaxed-quiet candidate requires review")
        ax.axvline(center*1e3, ls="--", label="candidate onset")
        ax.legend()
        fig.tight_layout()
        name = f"flagged_accepted/{pulse['id']}_raw.png"
        fig.savefig(figures / name, dpi=160)
        plt.close(fig)
        shown.append(name)

    # Flagged onsets found under alternate 80%/120% thresholds only,
    # shown as raw-sample plots. They are NOT silently promoted to pulses.
    threshold_only = result.get("threshold_sensitivity_unmatched_events", [])
    for candidate in threshold_only:
        center = float(candidate["time_s"])
        a = int(np.searchsorted(t, center-.00045))
        b = int(np.searchsorted(t, center+.00065))
        if b-a < 3:
            continue
        folder = figures / "threshold_sensitive"
        folder.mkdir(parents=True, exist_ok=True)
        fig, ax = plt.subplots(figsize=(11, 4))
        plot_traces(ax, [{
            "time_us": (t[a:b]*1e3).tolist(),
            "values": volts[a:b].tolist(),
            "color": "C0", "label": "original CH1 samples",
        }], xlim=(float(t[a])*1e3, float(t[b-1])*1e3),
            xlabel="Scope-local time (ms)", ylabel="CH1 voltage (V)",
            title=f"{candidate['id']}: threshold sensitivity "
                  f"{candidate['threshold_fraction']} (not a verified onset)")
        ax.axvline(center*1e3, ls="--", label="threshold-sensitive onset")
        ax.legend()
        fig.tight_layout()
        name = f"threshold_sensitive/{candidate['id']}_raw.png"
        fig.savefig(figures / name, dpi=160)
        plt.close(fig)
        shown.append(name)

    rejections = _diagnostic_rejections(times, values, result)
    if rejections:
        # Verbatim S02 rejection plotting implementation. This uses a broad
        # context and a 1.1-ms full-resolution raw zoom for every flagged row.
        save_rejection_figures(
            output, times, values, result["accepted_pulses"], rejections,
            threshold_v=float(threshold.get("threshold_v", -1.0)),
            baseline_v=float(threshold.get("baseline_v", 0.0)),
            quiet_band_v=1.5*max(
                1., 6*float(threshold.get("noise_sigma_v", 0.)),
                .25*float(threshold.get("threshold_drop_v", 0.))),
            measured_period_s=.01,
        )
        shown.extend(row["figure"] for row in rejections)
    write_csv(output / "rejection_reviews.csv", rejections,
              ("id", "sample_index", "time_s", "original_status",
               "diagnostic_tag", "negative_min_v", "positive_max_v",
               "local_neighbor_period_s", "figure"))
    return shown


def report_markdown(result, output, manifest, figures, *, root=None, record=None):
    """Human-readable S02-style record report with live review links."""
    output = Path(output)
    threshold = result.get("threshold", {})
    intervals_s = np.asarray(
        [row["period_s"] for row in result["periods"]], dtype=float
    )
    candidate_rates = 1.0 / intervals_s if len(intervals_s) else np.array([])
    audit = {
        "schema": "matrix32_pulse_metrics_v1",
        "record_index": result["record_index"],
        "detector": result["detector"],
        "source_ch1_sha256": manifest["source_ch1_sha256"],
        "status": result.get("status", "AUTOMATED_CANDIDATES_UNREVIEWED"),
        "sampling_interval_us": result["sampling_interval_us"],
        "capture_span_s": result["capture_end_s"]-result["capture_start_s"],
        "accepted_candidate_count": len(result["accepted_pulses"]),
        "unverified_interval_count": len(intervals_s),
        "unverified_mean_period_ms": (
            float(np.mean(intervals_s)*1000) if len(intervals_s) else None
        ),
        "unverified_median_period_ms": (
            float(np.median(intervals_s)*1000) if len(intervals_s) else None
        ),
        "unverified_period_p10_ms": (
            float(np.percentile(intervals_s, 10)*1000)
            if len(intervals_s) else None
        ),
        "unverified_period_p90_ms": (
            float(np.percentile(intervals_s, 90)*1000)
            if len(intervals_s) else None
        ),
        "unverified_median_inverse_period_hz": (
            float(np.median(candidate_rates))
            if len(candidate_rates) else None
        ),
        "unverified_interval_sd_ms": (
            float(np.std(intervals_s, ddof=1)*1000)
            if len(intervals_s) > 1 else None
        ),
        "rejected_candidate_count": len(result["rejected"]),
        "threshold_specific_review_count": len(
            result.get("threshold_sensitivity_unmatched_events", [])),

        "suppressed_candidate_count": len(result["suppressed"]),
        "threshold_sensitivity_event_counts": (
            result.get("threshold_sensitivity_event_counts")),
        "flags": result["flags"],
        "threshold": threshold,
        "near_storage_rail_count": result.get("near_storage_rail_count"),
        "operator_approval": "NOT_SUPPLIED",
    }
    approval_file = output / "review_approval.json"
    if approval_file.exists():
        # Verify the editable decisions, source-locked CSV tables and approved
        # timeline hashes, not just the raw waveform source identity.
        from oscilloscope.apply_pulse_review import verified_approval
        approved = verified_approval(output)
        if approved is not None:
            audit["operator_approval"] = "SOURCE_LOCKED_REVIEW_CONFIRMED"
            audit["operator_reviewed_onsets"] = approved["included_onsets"]
            audit["operator_reviewed_intervals"] = approved["included_intervals"]
        else:
            audit["operator_approval"] = "STALE_REVIEW_DO_NOT_USE"
    atomic_json(output / "metrics.json", audit)
    lines = [
        f"# Matrix32 CH1 negative-onset review — record {result['record_index']}",
        "",
        f"**Detector:** `{result['detector']}`  ",
        f"**Status:** {audit['status']}; review: {audit['operator_approval']}. ",
        "Automated candidates do not establish stimulation accuracy.",
        "", "## 1. Acquisition and provenance", "",
        "| Property | Value |", "| --- | --- |",
        f"| CH1 waveform | `{result['source_ch1']}` |",
        f"| CH1 SHA-256 | `{manifest['source_ch1_sha256']}` |",
        f"| Samples | {result['sampling_points']:,} |",
        f"| Span | {audit['capture_span_s']:.6f} s |",
        f"| Sample spacing | {result['sampling_interval_us']:.3f} us |",
        "", "## 2. Detection and diagnostic flags", "",
        "| Metric | Value |", "| --- | ---: |",
        f"| Accepted onset candidates | {len(result['accepted_pulses'])} |",
        f"| Rejected crossings | {len(result['rejected'])} |",
        f"| Threshold-specific onset locations | "
        f"{len(result.get('threshold_sensitivity_unmatched_events', []))} |",
        f"| Suppressed close crossings | {len(result['suppressed'])} |",
        f"| Record-boundary rejects | {result.get('record_start_boundary_rejected_count', 0)} |",
        f"| Interior rejects | {result.get('interior_rejected_count', 0)} |",
        f"| Near storage rail counts | {result.get('near_storage_rail_count')} |",
        f"| Sensitivity 80%/100%/120% counts | `{result.get('threshold_sensitivity_event_counts')}` |",
        f"| Baseline (V) | {threshold.get('baseline_v')} |",
        f"| Noise sigma (V) | {threshold.get('noise_sigma_v')} |",
        f"| Threshold (V) | {threshold.get('threshold_v')} |",
        "", "Flags: " + (", ".join(result["flags"]) if result["flags"] else "none"),
        "", "## 3. Period inventory", "",
        f"{len(result['periods'])} consecutive **unverified** intervals. "
        "A missed physical pulse can invalidate neighboring intervals. "
        "No cycles are concatenated across separate scope records.",
        "", "| Candidate interval measurement | Unverified result |",
        "| --- | ---: |",
        f"| Mean period | {audit['unverified_mean_period_ms']} ms |",
        f"| Median period | {audit['unverified_median_period_ms']} ms |",
        f"| Period p10 / p90 | {audit['unverified_period_p10_ms']} / "
        f"{audit['unverified_period_p90_ms']} ms |",
        f"| Period sample SD | {audit['unverified_interval_sd_ms']} ms |",
        f"| Median inverse interval | "
        f"{audit['unverified_median_inverse_period_hz']} Hz |",
        "These are descriptive statistics of *automated candidates* only; "
        "they are not validated Matrix32 frequency or jitter measurements.",
        "", "## 4. Diagnostic figures", "",
    ]
    for picture in figures:
        if picture in ("waveform_full.png", "waveform_four_5s_panels.png",
                       "waveform_event_detail.png"):
            lines += [f"![{picture}](figures/{picture})", ""]
    flagged = [p for p in figures if p.startswith("figures/rejected/")]
    relaxed_figures = [p for p in figures if p.startswith("flagged_accepted/")]
    if flagged:
        lines += [
            f"All {len(flagged)} rejected/suppressed crossings have individual "
            "two-panel S02-style context and raw-sample figures:",
            "",
        ]
        lines.extend(f"- [{p.split('/')[-1]}]({p})" for p in flagged)
    if relaxed_figures:
        lines += ["", "### Relaxed-quiet accepted-onset review figures", ""]
        lines.extend(f"- [{p}](figures/{p})" for p in relaxed_figures)
    sensitive_figures = [p for p in figures if p.startswith("threshold_sensitive/")]
    if sensitive_figures:
        lines += ["", "### Threshold-sensitive onset review figures", ""]
        lines.extend(f"- [{p}](figures/{p})" for p in sensitive_figures)
        lines.append(
            "These are alternative-threshold observations, not automatically "
            "accepted pulses. If genuine and missing in P/R/S, enter an "
            "M... manual addition with raw-image evidence."
        )
    lines += [
        "", "## 5. Operator review and downstream pulse timeline", "",
        "Review every candidate and **especially** every rejected/suppressed "
        "crossing using the linked raw plots. Edit the generated "
        "[review_decisions.csv](review_decisions.csv), setting INCLUDE or "
        "EXCLUDE as warranted; blank flagged decisions block approval. "
        "Optionally enter genuinely missed pulses in "
        "[manual_additions.csv](manual_additions.csv). "
        "Do not modify generated event tables.",
        "", "After complete review, run `apply_pulse_review.py` with "
        "an explicit named reviewer, --confirm-accepted and "
        "--confirm-complete. This produces a separate approved onset "
        "timeline and intervals, never modifies the original detector "
        "outputs, and is source-locked to this CH1 CSV.",
        "", "## Files", "",
        "- [Machine-readable metrics](metrics.json)",
        "- [Accepted candidates](accepted_onsets.csv)",
        "- [All unverified periods](periods_unverified.csv)",
        "- [Rejected candidates](rejected_onsets.csv)",
        "- [Suppressed candidates](suppressed_onsets.csv)",
        "- [Flagged event figure index](rejection_reviews.csv)",
        "- [Threshold-sensitive onset table](threshold_sensitive_onsets.csv)",
        "- [Review source-lock manifest](review_manifest.json)",
        "- [Editable review decisions](review_decisions.csv)",
        "- [Optional manually discovered pulses](manual_additions.csv)",
    ]
    if root is not None and record is not None:
        from oscilloscope.event_review_report import generate
        lines += ["", *generate(root, record, result, output, manifest)]
    (output / "report.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    return audit
