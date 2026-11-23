"""Sensitivity of approved Matrix32 pulses to clock model and USB offset.

No hardware. Never edits source recordings, pulse approvals, original clock
analyses or existing frequency reports. Output is exploratory, not a
calibrated latency estimate or an independent physical SF-application proof.

Uses per-record cross-validated clock-model candidates plus the causally
feasible USB offset interval (conditional on Nano/PC fitted slope). Checks
both endpoints and midpoint of that interval, widened by a reported
held-out prediction-error scale. That widening is NOT a guaranteed bound.
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
from oscilloscope.align_gray_records import read_nano_frames
from oscilloscope.clock_qualification_diagnostics import _frame_pairs
from oscilloscope.evaluate_clock_models import (
    _xy, MODEL_NAMES, predict, blocked_holdout, _metrics,
)
from oscilloscope.analyze_integrated_matrix32 import (
    acknowledged_frequency_states, read_events,
)
from oscilloscope.apply_pulse_review import load_approved_onsets
from oscilloscope.record_scope import atomic_json


INTERVAL_COLUMNS = [
    "record", "start_onset_id", "end_onset_id", "start_scope_s",
    "end_scope_s", "period_ms", "measured_inverse_period_hz",
    "clock_model", "clock_candidate_status", "clock_holdout_p95_us",
    "causal_offset_width_ms", "tested_extra_clock_error_ms",
    "mcu_assessment", "mcu_requested_hz_if_stable",
    "mcu_error_hz_if_stable", "ack_assessment",
    "ack_expected_hz_if_stable", "ack_error_hz_if_stable",
    "ack_matching_diagnostic", "all_scenarios_agree",
    "physical_apply_timestamp_validated", "absolute_alignment_calibrated",
]


def _csv(path, rows, headers):
    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def select_clock_model(cv_record):
    """Use a passed diagnostic candidate; otherwise show a flagged fallback."""
    models = cv_record.get("models") or {}
    if cv_record.get("affine_predictive_model_adequate"):
        return "affine", "AFFINE_HOLDOUT_SCREEN_PASS"
    selected = cv_record.get("candidate_for_independent_validation")
    if selected in models:
        return selected, "CV_CANDIDATE_REQUIRES_INDEPENDENT_VALIDATION"
    scored = []
    for name, item in models.items():
        one = (item.get("holdout") or {}).get("rms_us")
        two = (item.get("holdout_2x") or {}).get("rms_us")
        stable = (item.get("clock_rate_stability") or {}).get("monotonic")
        if name in MODEL_NAMES and one is not None and two is not None and stable:
            scored.append((max(one, two), name))
    if not scored:
        return None, "NO_USABLE_CLOCK_MODEL"
    return min(scored)[1], "EXPLORATORY_BEST_NO_MODEL_PASSED"


def _nano_to_host_float(nano_seconds, fit, offset_delta_ns=0.):
    """Perform centered float arithmetic to preserve host-ns precision."""
    anchor = float(fit["anchor_nano_us"])
    slope = float(fit["slope_host_ns_per_nano_us"])
    relative = float(fit["relative_offset_ns"])
    host_anchor = int(fit["anchor_host_monotonic_ns"])
    nano_us = np.asarray(nano_seconds, dtype=float)*1e6
    return host_anchor + (relative + offset_delta_ns
                          + slope*(nano_us-anchor))


def offset_scenarios(epoch_row, holdout_p95_us, *, scale=1.):
    """USB causal-offset endpoints relative to *fitted midpoint* offset.

    The modeled offset interval is conditional on the fitted Nano/PC slope.
    Holdout p95 is an exploratory sensitivity width, NOT a confidence bound.
    """
    fit = epoch_row.get("fit") or {}
    offset = epoch_row.get("offset_constraint") or {}
    required = ("intersection_lower_relative_ns", "intersection_upper_relative_ns")
    if not fit.get("host_clock_mapping_estimated"):
        return None, "NO_NANO_PC_HOST_FIT"
    if offset.get("feasible") is not True:
        return None, "NO_FEASIBLE_CAUSAL_OFFSET_INTERVAL"
    if any(offset.get(key) is None for key in required):
        return None, "NO_EXPLICIT_CAUSAL_OFFSET_ENDPOINTS"
    # The intersection is relative to offset_constraint.anchor_host_reference_ns;
    # fit.relative_offset_ns is relative to fit.anchor_host_monotonic_ns.
    # The implementation currently passes the same reference (fit host anchor),
    # but assert that instead of making an unjustified offset translation.
    if int(offset.get("anchor_host_reference_ns", -1)) != int(
        fit["anchor_host_monotonic_ns"]
    ):
        return None, "OFFSET_AND_FIT_HOST_ANCHORS_DIFFER"
    if float(offset.get("anchor_nano_us", float("nan"))) != float(
        fit["anchor_nano_us"]
    ):
        return None, "OFFSET_AND_FIT_NANO_ANCHORS_DIFFER"
    shift = float(fit["relative_offset_ns"])
    lo = float(offset[required[0]])-shift
    hi = float(offset[required[1]])-shift
    if hi < lo:
        return None, "REVERSED_OFFSET_INTERVAL"
    extra = (float(holdout_p95_us)*1000*scale if
             holdout_p95_us is not None else 0.)
    if not np.isfinite(extra) or extra < 0:
        raise ValueError("Invalid uncertainty sensitivity margin")
    scenarios = sorted(set([lo-extra, (lo+hi)/2, hi+extra]))
    return {
        "deltas_ns": scenarios,
        "original_delta_low_ns": lo,
        "original_delta_high_ns": hi,
        "causal_interval_width_ms": (hi-lo)/1e6,
        "extra_holdout_p95_sensitivity_ms": extra/1e6,
        "scenario_count": len(scenarios),
        "not_guaranteed_absolute_error_bound": True,
    }, None


def _same_effective_value(states, indices):
    """Agreement is by expected output state, not by ACK event identity."""
    chosen = []
    for idx in indices:
        if idx < 0:
            return None
        state = states[int(idx)]
        value = state.get("expected_hz")
        active = bool(state.get("output_active_assumed", True))
        if value is None:
            return None
        chosen.append((active, float(value) if active else 0.))
    return chosen[0] if len(set(chosen)) == 1 else None


def classify_interval(start_host_ns, end_host_ns, *, mcu_pc, mcu_rates,
                      ack_pc, ack_states, hold_s=0.005,
                      mcu_max_age_s=0.25, tolerance_abs_hz=8.,
                      tolerance_fraction=0.15, measured_hz):
    """Evaluate whole pulse interval against all timing scenarios.

    start/end are vectors (one point per scenario), in host-monotonic ns.
    A robust assignment requires identical effective MCU or ACK states
    throughout the interval across all scenarios. For ACKs use a settle
    guard extended to prior cycle duration. Device application remains
    unverified even if the classification is stable.
    """
    start = np.asarray(start_host_ns, dtype=float)
    end = np.asarray(end_host_ns, dtype=float)
    if start.shape != end.shape or np.any(end <= start):
        raise ValueError("Invalid mapped interval")
    r = {
        "mcu_assessment": "UNKNOWN_NO_MCU_COVERAGE",
        "mcu_requested_hz_if_stable": None,
        "mcu_error_hz_if_stable": None,
        "ack_assessment": "UNKNOWN_NO_ACK_COVERAGE",
        "ack_expected_hz_if_stable": None,
        "ack_error_hz_if_stable": None,
        "ack_matching_diagnostic": "NOT_COMPARABLE",
        "all_scenarios_agree": False,
    }
    if len(mcu_pc):
        ms = np.searchsorted(mcu_pc, start, side="right")-1
        me = np.searchsorted(mcu_pc, end, side="right")-1
        if np.any(ms < 0) or np.any(me < 0):
            r["mcu_assessment"] = "BEFORE_MCU_DATA"
        else:
            start_val = mcu_rates[ms]
            end_val = mcu_rates[me]
            age_s = (end-mcu_pc[me])/1e9
            if np.any(age_s > mcu_max_age_s) or np.any(age_s < 0):
                r["mcu_assessment"] = "MCU_SNAPSHOT_TOO_OLD"
            elif not np.all(mcu_rates[
                    max(0, int(np.searchsorted(
                        mcu_pc, np.min(start), side="right")-1)):
                    int(np.searchsorted(mcu_pc, np.max(end),
                                        side="right"))] == start_val[0]):
                # Check the entire uncertainty envelope, not merely a
                # few sampled endpoints: brief MCU commands may be skipped.
                r["mcu_assessment"] = "MCU_FREQUENCY_CHANGES_WITHIN_OFFSET_RANGE"
            else:
                hz = float(start_val[0])
                r["mcu_requested_hz_if_stable"] = hz
                r["mcu_error_hz_if_stable"] = float(measured_hz-hz)
                r["mcu_assessment"] = "STABLE_ACROSS_TESTED_OFFSETS"
    if len(ack_pc):
        a0 = np.searchsorted(ack_pc, start, side="right")-1
        a1 = np.searchsorted(ack_pc, end, side="right")-1
        if np.any(a0 < 0) or np.any(a1 < 0):
            r["ack_assessment"] = "BEFORE_FIRST_ACK"
        elif np.any(a0 != a1) or (int(np.searchsorted(
                ack_pc, np.min(start), side="right")-1) !=
                int(np.searchsorted(ack_pc, np.max(end),
                                    side="right")-1)):
            # Require NO ACK event anywhere in the continuous uncertain
            # interval, even if two endpoint scenarios happen to land in
            # different states with identical effective frequency.
            r["ack_assessment"] = "TRANSITION_WITHIN_OFFSET_ENVELOPE"
        else:
            # An extra command can change the frequency to the same number;
            # treat a transition as non-observable even in that case.
            effective = _same_effective_value(ack_states, a0)
            if effective is None:
                r["ack_assessment"] = "ACK_DIFFERS_ACROSS_TESTED_OFFSETS"
            elif not effective[0] or effective[1] <= 0:
                r["ack_assessment"] = "ACK_MUTED_OR_UNKNOWN"
            else:
                # Exclude initial cycle after ACK using the prior SF period.
                guards = []
                for idx in a0:
                    idx = int(idx)
                    if idx == 0:
                        guards.append(float("inf"))
                    else:
                        prev = ack_states[idx-1]
                        prev_hz = prev.get("expected_hz")
                        if (not prev.get("output_active_assumed", True)
                                or prev_hz is None or prev_hz <= 0):
                            guards.append(float("inf"))
                        else:
                            guards.append(max(hold_s, 1/float(prev_hz)))
                if np.any(start < ack_pc[a0]+np.asarray(guards)*1e9):
                    r["ack_assessment"] = "ACK_PRIOR_CYCLE_OR_UNKNOWN_GUARD"
                else:
                    hz = float(effective[1])
                    err = float(measured_hz-hz)
                    good = abs(err) <= max(tolerance_abs_hz,
                                          tolerance_fraction*hz)
                    r["ack_expected_hz_if_stable"] = hz
                    r["ack_error_hz_if_stable"] = err
                    r["ack_assessment"] = "STABLE_ACROSS_TESTED_OFFSETS"
                    r["ack_matching_diagnostic"] = (
                        "WITHIN_EXPLORATORY_TOLERANCE" if good
                        else "OUTSIDE_EXPLORATORY_TOLERANCE"
                    )
    r["all_scenarios_agree"] = (
        r["mcu_assessment"] == "STABLE_ACROSS_TESTED_OFFSETS"
        and r["ack_assessment"] == "STABLE_ACROSS_TESTED_OFFSETS"
    )
    return r


def analyze_record(*, root, record, epoch, cv, mcu, states,
                   taxel, encoding, scale):
    idx = int(record["index"])
    report = {
        "record": idx, "original_clock_status": epoch.get("status"),
        "approved_source": "source_locked_S02_negative_onsets",
        "candidate_only": True, "absolute_alignment_calibrated": False,
        "physical_apply_timestamp_validated": False,
    }
    model, model_status = select_clock_model(cv)
    report["clock_model"] = model
    report["clock_candidate_status"] = model_status
    folder = root/"analysis"/f"record_{idx:05d}"
    folder.mkdir(parents=True, exist_ok=True)
    ch1 = root/record["channels"]["CH1"]["path"]
    _, onsets = load_approved_onsets(folder/"pulse_review", ch1)
    times = np.array([float(row["time_s"]) for row in onsets], dtype=float)
    report["approved_onsets"] = len(onsets)
    report["approved_intervals"] = max(0, len(onsets)-1)
    result_rows = []
    if model is None:
        report["reason"] = "No cross-validated model estimates available"
        return report, result_rows, []
    pairs, issue = _frame_pairs(root, record, read_nano_frames(root/"nano_serial.jsonl"))
    if issue:
        report["reason"] = issue
        return report, result_rows, []
    x, y = _xy(pairs)
    holdouts = cv.get("models", {}).get(model, {})
    p95s = [(holdouts.get(key) or {}).get("p95_abs_us")
            for key in ("holdout", "holdout_2x")]
    p95s = [float(value) for value in p95s if value is not None]
    if not p95s:
        report["reason"] = "No withheld timing uncertainty scale"
        return report, result_rows, []
    p95_us = max(p95s)
    scenario, blocker = offset_scenarios(epoch, p95_us, scale=scale)
    report["clock_holdout_p95_us"] = p95_us
    report["offset_scenarios"] = scenario
    if blocker:
        report["reason"] = blocker
        return report, result_rows, []
    nano_sec = predict(model, x, y, times) if len(times) else np.array([])
    # Never extrapolate a clock map outside the observed Gray-frame range.
    valid = (times >= x[0]) & (times <= x[-1])
    report["onsets_outside_gray_frame_support"] = int(np.sum(~valid))
    report["onsets_inside_gray_frame_support"] = int(np.sum(valid))
    host_float = (_nano_to_host_float(nano_sec, epoch["fit"])
                  if len(times) else np.array([]))
    deltas = np.asarray(scenario["deltas_ns"], dtype=float)
    mcu_pc = np.asarray(mcu["taxel_pc_ns"], dtype=np.int64)
    rates = (mcu["sa_rate_hz"] if encoding == "sa_ii"
             else mcu["ra_rate_hz"])[:, taxel]
    order = np.argsort(mcu_pc, kind="stable")
    mcu_pc, rates = mcu_pc[order], rates[order]
    ack_sorted = sorted(states, key=lambda s: int(s["pc_monotonic_ns"]))
    ack_pc = np.asarray([int(a["pc_monotonic_ns"]) for a in ack_sorted],
                        dtype=np.int64)
    for i in range(max(0, len(onsets)-1)):
        dt = float(times[i+1]-times[i])
        row = {
            "record": idx, "start_onset_id": onsets[i]["id"],
            "end_onset_id": onsets[i+1]["id"],
            "start_scope_s": float(times[i]), "end_scope_s": float(times[i+1]),
            "period_ms": 1000*dt, "measured_inverse_period_hz": 1/dt,
            "clock_model": model, "clock_candidate_status": model_status,
            "clock_holdout_p95_us": p95_us,
            "causal_offset_width_ms": scenario["causal_interval_width_ms"],
            "tested_extra_clock_error_ms":
                scenario["extra_holdout_p95_sensitivity_ms"],
            "physical_apply_timestamp_validated": False,
            "absolute_alignment_calibrated": False,
        }
        if not (valid[i] and valid[i+1]):
            status = {
                "mcu_assessment": "OUTSIDE_GRAY_FRAME_SUPPORT",
                "ack_assessment": "OUTSIDE_GRAY_FRAME_SUPPORT",
                "all_scenarios_agree": False,
            }
        else:
            start = host_float[i]+deltas
            end = host_float[i+1]+deltas
            status = classify_interval(
                start, end, mcu_pc=mcu_pc, mcu_rates=rates,
                ack_pc=ack_pc, ack_states=ack_sorted,
                measured_hz=1/dt,
            )
        row.update(status)
        result_rows.append(row)
    counts = Counter(row.get("ack_assessment") for row in result_rows)
    mcounts = Counter(row.get("mcu_assessment") for row in result_rows)
    agreed = [r for r in result_rows if r.get("all_scenarios_agree")]
    report["ack_assessment_counts"] = dict(counts)
    report["mcu_assessment_counts"] = dict(mcounts)
    report["both_stable_across_tested_offsets"] = len(agreed)
    report["both_stable_fraction_of_all_approved_intervals"] = (
        len(agreed)/len(result_rows) if result_rows else None)
    report["ack_diagnostic_tolerance_counts"] = dict(
        Counter(r.get("ack_matching_diagnostic") for r in result_rows))
    return report, result_rows, [(float(t), float(h))
                                 for t, h in zip(times, host_float)]


def plot_tracking(path, rows, *, index):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 1, figsize=(15, 8),
                             sharex=True, layout="constrained")
    labels = [
        ("STABLE_ACROSS_TESTED_OFFSETS", "Stable across tested offsets"),
        ("ACK_DIFFERS_ACROSS_TESTED_OFFSETS", "ACK ambiguous"),
        ("TRANSITION_DURING_INTERVAL", "Transition / guard ambiguous"),
    ]
    for status, label in labels:
        subset = [r for r in rows if r.get("ack_assessment") == status]
        if subset:
            axes[0].scatter(
                [(r["start_scope_s"]+r["end_scope_s"])/2 for r in subset],
                [r["measured_inverse_period_hz"] for r in subset],
                s=9, alpha=.65, label=label)
    axes[0].set(ylabel="Physical inverse period (Hz)",
                title=f"Record {index}: approved physical intervals by "
                      "ACK offset-sensitivity class")
    # An honest coverage plot: only agreements are shown, not a falsely
    # continuous interpolation of fast-changing ACKs.
    stable = [r for r in rows
              if r.get("ack_assessment") == "STABLE_ACROSS_TESTED_OFFSETS"]
    if stable:
        axes[1].scatter(
            [(r["start_scope_s"]+r["end_scope_s"])/2 for r in stable],
            [r["measured_inverse_period_hz"]-
             r["ack_expected_hz_if_stable"] for r in stable],
            s=9, alpha=.65, label="Measured − stable ACK state (diagnostic)")
    else:
        axes[1].text(.5, .5, "No intervals stable under tested offsets",
                     ha="center", va="center",
                     transform=axes[1].transAxes)
    axes[1].set(xlabel="Scope-local time (s)",
                ylabel="Physical − ACK SF (Hz)")
    for ax in axes:
        ax.grid(alpha=.2)
        if ax.get_legend_handles_labels()[0]:
            ax.legend(fontsize=8)
    fig.savefig(path, dpi=145)
    plt.close(fig)


def record4_diagnostics(root, metadata, serial, *, block_s=2.):
    """Localize record 4's failure by *withheld* time block."""
    recs = {int(r["index"]): r for r in metadata["records"]}
    if 4 not in recs:
        return None
    pairs, issue = _frame_pairs(root, recs[4], serial)
    if issue:
        return {"error": issue}
    x, y = _xy(pairs)
    models = ("affine", "local_linear_0p75s", "local_linear_1p5s",
              "local_linear_3s")
    block_rows = []
    for width in (block_s, 2*block_s):
        _, data, _ = blocked_holdout(x, y, block_s=width, models=models)
        bins = defaultdict(list)
        for row in data:
            bins[(row["block"], row["model"])].append(row)
        for (block, model), values in sorted(bins.items()):
            error = [v["heldout_error_us"] for v in values]
            block_rows.append({
                "block_width_s": width, "block_id": block,
                "start_scope_s": min(v["scope_s"] for v in values),
                "end_scope_s": max(v["scope_s"] for v in values),
                "model": model, **_metrics(error),
            })
    folder = root/"analysis"/"record_00004"
    folder.mkdir(parents=True, exist_ok=True)
    _csv(folder/"record4_clock_block_errors.csv", block_rows, [
        "block_width_s", "block_id", "start_scope_s", "end_scope_s",
        "model", "n", "rms_us", "median_abs_us", "p95_abs_us",
        "max_abs_us", "bias_us",
    ])
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(13, 5), layout="constrained")
    for model in models:
        v = [r for r in block_rows if r["model"] == model
             and r["block_width_s"] == 2*block_s]
        if v:
            ax.plot([.5*(r["start_scope_s"]+r["end_scope_s"]) for r in v],
                    [r["rms_us"] for r in v], marker="o",
                    label=f"{model}, {2*block_s:g}-s holdout")
    ax.axhline(100, color="tab:red", ls="--", label="100 µs diagnostic gate")
    ax.set(xlabel="Withheld Gray block center, scope-local seconds",
           ylabel="Held-out frame prediction RMS (µs)",
           title="Record 4: where does long-block clock recovery fail?")
    ax.grid(alpha=.2)
    if ax.get_legend_handles_labels()[0]:
        ax.legend(fontsize=9)
    fig.savefig(folder/"record4_clock_local_holdouts.png", dpi=145)
    plt.close(fig)
    worst = sorted([r for r in block_rows
                    if r["model"] == "local_linear_0p75s"
                    and r["block_width_s"] == 2*block_s],
                   key=lambda r: -r["rms_us"])
    return {"long_block_s": 2*block_s,
            "worst_local_0p75s_long_blocks": worst[:4],
            "all_block_count": len(block_rows),
            "figure": "record_00004/record4_clock_local_holdouts.png",
            "csv": "record_00004/record4_clock_block_errors.csv"}


def run(root, *, extra_scale=1., block_s=2., taxel=None):
    root = Path(root).expanduser().resolve()
    meta = json.loads((root/"metadata.json").read_text())
    epoch = json.loads((root/"epoch_clock_analysis.json").read_text())
    cv = json.loads((root/"analysis"/"clock_model_cv_summary.json").read_text())
    if abs(float(cv["block_s"])-block_s) > 1e-9:
        raise ValueError(
            f"Clock CV result uses {cv['block_s']}s blocks; "
            f"rerun evaluate_clock_models --block-s {block_s:g} or "
            "select the matching --block-s here.")
    rec_cv = {int(r["record_index"]): r for r in cv["records"]}
    rec_epoch = {int(r["record_index"]): r for r in epoch["records"]}
    serial = read_nano_frames(root/"nano_serial.jsonl")
    mcu_dir = root/"mcu_matrix32"
    rid = choose_recording(mcu_dir)
    mcu = load_recordings(mcu_dir, rid)
    events = list(mcu_dir.glob("*_matrix32_commands.jsonl"))
    if len(events) != 1:
        raise ValueError("Exactly one continuous Matrix32 command trace required")
    ack = acknowledged_frequency_states(read_events(events[0]))
    source = mcu.get("experiment_settings", {}).get("configuration", {}).get(
        "stimulation_source", {})
    taxel = int(source.get("taxel", 11) if taxel is None else taxel)
    encoding = source.get("encoding", "sa_ii")
    if taxel not in range(12) or encoding not in ("sa_ii", "ra_i"):
        raise ValueError("Invalid selected taxel/encoding")
    summaries = []
    all_rows = []
    for record in meta["records"]:
        idx = int(record["index"])
        if idx not in rec_cv or idx not in rec_epoch:
            raise ValueError(f"Record {idx}: CV/epoch result missing")
        summary, rows, _ = analyze_record(
            root=root, record=record, epoch=rec_epoch[idx],
            cv=rec_cv[idx], mcu=mcu, states=ack, taxel=taxel,
            encoding=encoding, scale=extra_scale,
        )
        summaries.append(summary)
        all_rows.extend(rows)
        folder = root/"analysis"/f"record_{idx:05d}"
        _csv(folder/"offset_robust_intervals.csv", rows, INTERVAL_COLUMNS)
        plot_tracking(folder/"offset_robust_tracking.png", rows, index=idx)
        print(f"[OFFSET {idx}] model={summary['clock_model']} "
              f"status={summary['clock_candidate_status']}; "
              f"stable both={summary.get('both_stable_across_tested_offsets', 0)} "
              f"/{summary['approved_intervals']}")
    output = root/"analysis"
    _csv(output/"offset_robust_all_intervals.csv", all_rows, INTERVAL_COLUMNS)
    detail4 = record4_diagnostics(root, meta, serial, block_s=block_s)
    report = {
        "schema": "matrix32_offset_sensitivity_v1",
        "source_run": root.name,
        "records": summaries,
        "total_approved_onsets": sum(r["approved_onsets"] for r in summaries),
        "total_approved_intervals": sum(r["approved_intervals"] for r in summaries),
        "stable_mcu_and_ack_intervals": sum(
            r.get("both_stable_across_tested_offsets", 0) for r in summaries),
        "record4_clock_diagnostics": detail4,
        "exploratory_not_calibrated": True,
        "original_clock_maps_unchanged": True,
        "original_waveforms_and_approvals_unchanged": True,
        "causal_offset_interval_is_conditional_on_fitted_slope": True,
        "heldout_p95_is_not_a_guaranteed_timing_bound": True,
        "command_apply_latency_unknown": True,
    }
    atomic_json(output/"offset_robust_summary.json", report)
    md = [
        "# Matrix32 approved-pulse command association: offset sensitivity", "",
        "**Exploratory, not calibrated.** Uses operator-approved CH1 negative "
        "onsets only. A stable result means the association did not change "
        "across the *tested* USB-offset/Gray-model uncertainty scenarios. "
        "It is NOT proof that Matrix32 physically applied that ACK.",
        "",
        "Clock model is selected from the pre-existing cross-validation "
        "comparison. If none passed its longer holdout, we use a best-"
        "scoring model only as an explicitly flagged sensitivity probe. "
        "The source-locked approvals and clock maps are never changed.",
        "",
        "| Record | Approved intervals | Clock candidate | Status | "
        "USB causal interval (ms) | MCU stable | ACK stable | Both stable |",
        "| ---: | ---: | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for r in summaries:
        a = r.get("ack_assessment_counts", {})
        m = r.get("mcu_assessment_counts", {})
        md.append(
            f"| {r['record']} | {r['approved_intervals']} | "
            f"{r['clock_model']} | {r['clock_candidate_status']} | "
            f"{(r.get('offset_scenarios') or {}).get('causal_interval_width_ms')} | "
            f"{m.get('STABLE_ACROSS_TESTED_OFFSETS', 0)} | "
            f"{a.get('STABLE_ACROSS_TESTED_OFFSETS', 0)} | "
            f"{r.get('both_stable_across_tested_offsets', 0)} |"
        )
    md += [
        "", "## Record 4: location of long-block fit errors", "",
        "The [per-block CSV](record_00004/record4_clock_block_errors.csv) "
        "exposes every withheld region's RMS, p95 absolute error and bias.",
        "",
        "![Record 4 long-block Gray prediction errors]"
        "(record_00004/record4_clock_local_holdouts.png)", "",
        "## Physical tracking under tested offsets", "",
    ]
    for r in summaries:
        idx = r["record"]
        md += [
            f"### Record {idx}", "",
            f"Candidate: **{r['clock_model']}** "
            f"({r['clock_candidate_status']}); "
            f"Gray holdout p95 envelope: {r.get('clock_holdout_p95_us')} µs "
            "(heuristic, NOT absolute bound).",
            "",
            f"MCU statuses: {r.get('mcu_assessment_counts', {})}.",
            f"ACK statuses: {r.get('ack_assessment_counts', {})}.", "",
            f"[Every approved interval and attribution]"
            f"(record_{idx:05d}/offset_robust_intervals.csv)", "",
            f"![Record {idx} physically measured and offset-stable ACK intervals]"
            f"(record_{idx:05d}/offset_robust_tracking.png)", "",
        ]
    md += [
        "## Interpretation", "",
        "- **Stable across scenarios** means the mapped interval remains "
        "inside one effective MCU rate and one settled ACK state for the "
        "specific offsets tested. Clock *model* and causal-offset assumption "
        "are not independently validated.",
        "- The feasible USB interval is conditional on the fitted drift "
        "slope and timestamp ordering; it does not bound BLE delivery or "
        "Matrix32 physical application latency.",
        "- The additional ±p95 heldout-frame scale is a sensitivity "
        "parameter, not a statistical confidence interval or guaranteed bound.",
        "- Any approved intervals outside the matched CH2 Gray-frame span "
        "are left unclassified, not extrapolated.",
        "- Intervals spanning ACK updates or during the prior-cycle guard "
        "are excluded from a physical command-level verdict.",
        "- **No stable assignment** is itself informative: rapid command "
        "updates may be unobservable under the current timing uncertainty.",
        "- To validate physical command-application latency, acquire a "
        "common electrical marker or establish an independent offset/error "
        "bound with suitable timing resolution.",
        "", f"All intervals: [CSV](offset_robust_all_intervals.csv).",
        "Structured summary: [JSON](offset_robust_summary.json).",
    ]
    (output/"offset_robust_report.md").write_text("\n".join(md)+"\n")
    print(f"[OFFSET] Report: {output/'offset_robust_report.md'}")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--block-s", type=float, default=2.)
    parser.add_argument("--extra-clock-error-scale", type=float, default=1.)
    parser.add_argument("--taxel", type=int, default=None, choices=range(12))
    args = parser.parse_args(argv)
    if not (math.isfinite(args.extra_clock_error_scale)
            and args.extra_clock_error_scale >= 0
            and args.extra_clock_error_scale <= 10
            and args.block_s >= .25):
        parser.error("Invalid clock sensitivity scale or block duration")
    return run(args.run_dir, extra_scale=args.extra_clock_error_scale,
               block_s=args.block_s, taxel=args.taxel)


if __name__ == "__main__":
    main()
