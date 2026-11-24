"""Audit approved physical frequency agreement under timing/transition assumptions.

Run after evaluate_clock_models.py --block-s 2. Reads the ORIGINAL Gray
frames, Nano serial log, MCU rates, BLE ACK log, and source-locked CH1 onset
reviews. Never edits official clock maps or approvals. Does not estimate
Matrix32 electrical command-application latency.

Outputs per-record and session robustness sweeps, diagnostic outlier context,
period errors, and Gray frame-spacing diagnostics. All agreement claims are
conditional on the models and offset scenarios described in the report.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
import math
from pathlib import Path

import numpy as np

from analyze_mcu_recording import choose_recording, load_recordings
from oscilloscope.align_gray_records import read_nano_frames
from oscilloscope.analyze_integrated_matrix32 import (
    acknowledged_frequency_states, read_events,
)
from oscilloscope.apply_pulse_review import load_approved_onsets
from oscilloscope.clock_qualification_diagnostics import _frame_pairs
from oscilloscope.evaluate_clock_models import _xy, predict
from oscilloscope.offset_robust_command_matching import (
    _nano_to_host_float, classify_interval, offset_scenarios,
    select_clock_model,
)
from oscilloscope.record_scope import atomic_json

SCALES = (1., 2., 3.)
GUARDS_MS = (5., 20., 50.)
FIELDS = [
    "record", "start_onset_id", "end_onset_id", "start_scope_s",
    "end_scope_s", "measured_period_ms", "measured_frequency_hz",
    "mcu_requested_hz_stable", "ack_indicated_hz_stable",
    "commanded_period_ms_stable", "period_error_us",
    "frequency_error_hz", "relative_frequency_error_pct",
    "baseline_mcu_assessment", "baseline_ack_assessment",
    "baseline_ack_diagnostic", "strict_mcu_assessment",
    "strict_ack_assessment", "strict_ack_diagnostic",
    "stable_ack_across_all_sensitivity_scenarios",
    "stable_both_across_all_sensitivity_scenarios",
    "all_scenarios_same_ack_hz", "nominal_period_range_flag",
    "adjacent_outlier_pair", "clock_model", "clock_model_status",
    "physical_application_latency_verified",
]


def write_csv(path, rows, fields):
    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        w = csv.DictWriter(handle, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def summary_errors(rows):
    diffs = np.asarray(rows, dtype=float)
    if not len(diffs):
        return {"n": 0, "median_abs": None, "p90_abs": None,
                "p95_abs": None, "max_abs": None, "mean_signed": None}
    return {
        "n": int(len(diffs)),
        "median_abs": float(np.median(np.abs(diffs))),
        "p90_abs": float(np.percentile(np.abs(diffs), 90)),
        "p95_abs": float(np.percentile(np.abs(diffs), 95)),
        "max_abs": float(np.max(np.abs(diffs))),
        "mean_signed": float(np.mean(diffs)),
    }


def gray_spacing(pairs):
    """Diagnose CH2/Nano inter-frame timing without asserting GPIO truth.

    Subtract fitted median *relative rate*, not merely 100ms, so that
    drift and frame scheduling residuals remain distinguishable.
    """
    x, y = _xy(pairs)
    dx, dy = np.diff(x), np.diff(y)
    ratio = dy/dx
    median_ratio = float(np.median(ratio))
    discrepancies_us = (dy-median_ratio*dx)*1e6
    data = [
        {"scope_s": float(x[i+1]), "scope_spacing_ms": float(dx[i]*1000),
         "nano_spacing_ms": float(dy[i]*1000),
         "spacing_difference_us": float(discrepancies_us[i]),
         "instantaneous_nano_scope_rate_ppm": float(
             (ratio[i]/median_ratio-1)*1e6)}
        for i in range(len(dx))
    ]
    first = discrepancies_us[:max(1, len(discrepancies_us)//2)]
    second = discrepancies_us[len(discrepancies_us)//2:]
    return data, {
        "frame_pairs": len(pairs),
        "median_scope_spacing_ms": float(np.median(dx)*1000),
        "median_nano_spacing_ms": float(np.median(dy)*1000),
        "median_nano_scope_clock_slope": median_ratio,
        "relative_frame_spacing_error_us": summary_errors(discrepancies_us),
        "first_half_spacing_rms_us": float(np.sqrt(np.mean(first**2))),
        "second_half_spacing_rms_us": (
            float(np.sqrt(np.mean(second**2))) if len(second) else None),
        "physical_GPIO_transition_independently_timestamped": False,
    }


def draw_gray_spacing(path, rows, *, record):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(14, 4.5), layout="constrained")
    ax.plot([r["scope_s"] for r in rows],
            [r["spacing_difference_us"] for r in rows],
            lw=.75, marker=".", markersize=3)
    ax.axhline(0, color="black", ls="--", alpha=.5)
    ax.set(xlabel="Scope-local Gray frame time (s)",
           ylabel="Nano frame spacing − fitted-rate × scope spacing (µs)",
           title=f"Record {record}: physical CH2 frame spacing vs Nano EDGE_US")
    ax.grid(alpha=.2)
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _period_flag(period_s):
    if period_s > .5:
        return "LONGER_THAN_2HZ_PERIOD"
    if period_s < .0025:
        return "SHORTER_THAN_400HZ_PERIOD"
    return "WITHIN_NOMINAL_ACTIVE_PERIOD_RANGE"


def _quantities(physical_hz, ack_hz):
    if ack_hz is None or ack_hz <= 0:
        return None, None, None, None
    commanded_ms = 1000/ack_hz
    period_error_us = (1000/physical_hz-commanded_ms)*1000
    frequency_error = physical_hz-ack_hz
    return (commanded_ms, period_error_us, frequency_error,
            100*frequency_error/ack_hz)


def evaluate_scenarios(times, source_rows, host_ns, valid, *,
                       record, model, model_status, epoch, cv_model,
                       mcu_pc, rates, ack_pc, ack_states):
    """Analyze every approved period at all 3 offset scales x 3 guards."""
    p95_values = [
        (cv_model.get(k) or {}).get("p95_abs_us")
        for k in ("holdout", "holdout_2x")
    ]
    p95_values = [float(v) for v in p95_values if v is not None]
    if not p95_values:
        raise ValueError(f"Record {record}: missing withheld-frame p95 scale")
    p95 = max(p95_values)
    scenario_cache = {}
    for scale in SCALES:
        offsets, issue = offset_scenarios(epoch, p95, scale=scale)
        if issue:
            raise ValueError(f"Record {record}: {issue}")
        scenario_cache[scale] = offsets

    output = []
    counts = {}
    for scale in SCALES:
        for guard_ms in GUARDS_MS:
            counts[(scale, guard_ms)] = {
                "approved_intervals": max(0, len(times)-1),
                "within_gray_support": 0,
                "stable_mcu": 0, "stable_ack": 0, "both_stable": 0,
                "ack_within_tolerance": 0,
                "ack_outside_tolerance": 0,
                "period_errors_us": [], "frequency_errors_hz": [],
            }

    for i in range(max(0, len(times)-1)):
        dt = float(times[i+1]-times[i])
        frequency = 1/dt
        baseline = None
        strict = None
        scenarios = {}
        stable_freqs = []
        for scale, offset in scenario_cache.items():
            shifts = np.asarray(offset["deltas_ns"], dtype=float)
            for guard_ms in GUARDS_MS:
                if not valid[i] or not valid[i+1]:
                    result = {
                        "mcu_assessment": "OUTSIDE_GRAY_FRAME_SUPPORT",
                        "ack_assessment": "OUTSIDE_GRAY_FRAME_SUPPORT",
                        "ack_matching_diagnostic": "NOT_COMPARABLE",
                        "all_scenarios_agree": False,
                        "ack_expected_hz_if_stable": None,
                        "mcu_requested_hz_if_stable": None,
                    }
                else:
                    result = classify_interval(
                        host_ns[i]+shifts, host_ns[i+1]+shifts,
                        mcu_pc=mcu_pc, mcu_rates=rates,
                        ack_pc=ack_pc, ack_states=ack_states,
                        measured_hz=frequency, hold_s=guard_ms/1000,
                    )
                scenarios[(scale, guard_ms)] = result
                count = counts[(scale, guard_ms)]
                if valid[i] and valid[i+1]:
                    count["within_gray_support"] += 1
                if result["mcu_assessment"] == "STABLE_ACROSS_TESTED_OFFSETS":
                    count["stable_mcu"] += 1
                if result["ack_assessment"] == "STABLE_ACROSS_TESTED_OFFSETS":
                    count["stable_ack"] += 1
                    ack_hz = result["ack_expected_hz_if_stable"]
                    commanded_ms, period_error_us, frequency_error, _ = (
                        _quantities(frequency, ack_hz))
                    count["period_errors_us"].append(period_error_us)
                    count["frequency_errors_hz"].append(frequency_error)
                    if result["ack_matching_diagnostic"] == (
                            "WITHIN_EXPLORATORY_TOLERANCE"):
                        count["ack_within_tolerance"] += 1
                    else:
                        count["ack_outside_tolerance"] += 1
                if result["all_scenarios_agree"]:
                    count["both_stable"] += 1
                if (scale, guard_ms) == (1., 5.):
                    baseline = result
                if (scale, guard_ms) == (3., 50.):
                    strict = result
        stable_ack_everywhere = all(
            r["ack_assessment"] == "STABLE_ACROSS_TESTED_OFFSETS"
            for r in scenarios.values())
        stable_both_everywhere = all(
            r["all_scenarios_agree"] for r in scenarios.values())
        for r in scenarios.values():
            if r["ack_assessment"] == "STABLE_ACROSS_TESTED_OFFSETS":
                stable_freqs.append(r["ack_expected_hz_if_stable"])
        unique_ack = len(set(stable_freqs)) == 1 if stable_freqs else False
        ack_hz = (baseline["ack_expected_hz_if_stable"] if
                  baseline["ack_assessment"] == "STABLE_ACROSS_TESTED_OFFSETS"
                  else None)
        command_period, error_us, error_hz, error_pct = (
            _quantities(frequency, ack_hz))
        output.append({
            "record": record, "start_onset_id": source_rows[i]["id"],
            "end_onset_id": source_rows[i+1]["id"],
            "start_scope_s": float(times[i]),
            "end_scope_s": float(times[i+1]),
            "measured_period_ms": 1000*dt,
            "measured_frequency_hz": frequency,
            "mcu_requested_hz_stable":
                baseline["mcu_requested_hz_if_stable"],
            "ack_indicated_hz_stable": ack_hz,
            "commanded_period_ms_stable": command_period,
            "period_error_us": error_us,
            "frequency_error_hz": error_hz,
            "relative_frequency_error_pct": error_pct,
            "baseline_mcu_assessment": baseline["mcu_assessment"],
            "baseline_ack_assessment": baseline["ack_assessment"],
            "baseline_ack_diagnostic": baseline["ack_matching_diagnostic"],
            "strict_mcu_assessment": strict["mcu_assessment"],
            "strict_ack_assessment": strict["ack_assessment"],
            "strict_ack_diagnostic": strict["ack_matching_diagnostic"],
            "stable_ack_across_all_sensitivity_scenarios":
                stable_ack_everywhere and unique_ack,
            "stable_both_across_all_sensitivity_scenarios":
                stable_both_everywhere and unique_ack,
            "all_scenarios_same_ack_hz": unique_ack,
            "nominal_period_range_flag": _period_flag(dt),
            "adjacent_outlier_pair": False,
            "clock_model": model,
            "clock_model_status": model_status,
            "physical_application_latency_verified": False,
        })
    for i in range(len(output)):
        if output[i]["baseline_ack_diagnostic"] != (
                "OUTSIDE_EXPLORATORY_TOLERANCE"):
            continue
        neighbors = output[max(0, i-1):min(len(output), i+2)]
        # Two successive bad periods around a similar ACK frequency can
        # reflect a transition/quiet boundary or a missing onset.
        output[i]["adjacent_outlier_pair"] = any(
            a is not output[i] and
            a["baseline_ack_diagnostic"] == "OUTSIDE_EXPLORATORY_TOLERANCE"
            and a["ack_indicated_hz_stable"] ==
                output[i]["ack_indicated_hz_stable"]
            for a in neighbors)
    sweep = []
    for (scale, guard), c in sorted(counts.items()):
        row = {key: value for key, value in c.items()
               if key not in ("period_errors_us", "frequency_errors_hz")}
        row.update({
            "record": record, "clock_model": model,
            "clock_model_status": model_status,
            "offset_scale": scale, "transition_guard_ms": guard,
            "ack_period_error_us": summary_errors(c["period_errors_us"]),
            "ack_frequency_error_hz": summary_errors(c["frequency_errors_hz"]),
            "clock_offset_interval_width_ms":
                scenario_cache[scale]["causal_interval_width_ms"],
            "gray_holdout_p95_us": p95,
            "absolute_clock_error_guaranteed": False,
        })
        sweep.append(row)
    return output, sweep, scenario_cache


def plot_sweep(path, sweep, *, record):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.7),
                             layout="constrained")
    for guard in GUARDS_MS:
        selected = [s for s in sweep
                    if s["transition_guard_ms"] == guard]
        axes[0].plot([s["offset_scale"] for s in selected],
                     [s["stable_ack"] for s in selected],
                     marker="o", label=f"Prior-cycle guard ≥{guard:g} ms")
        axes[1].plot([s["offset_scale"] for s in selected],
                     [s["ack_frequency_error_hz"]["median_abs"]
                      for s in selected],
                     marker="o", label=f"Guard ≥{guard:g} ms")
    axes[0].set(xlabel="Gray p95 margin multiplier",
                ylabel="Intervals with stable ACK association",
                title="How many intervals remain comparable?")
    axes[1].set(xlabel="Gray p95 margin multiplier",
                ylabel="Median absolute measured−ACK difference (Hz)",
                title="Conditional error within remaining stable subset")
    for ax in axes:
        ax.grid(alpha=.2)
        if ax.get_legend_handles_labels()[0]:
            ax.legend(fontsize=8)
    fig.suptitle(f"Record {record}: sensitivity to exploratory assumptions")
    fig.savefig(path, dpi=145)
    plt.close(fig)


def plot_period_error(path, rows, *, record):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    matched = [r for r in rows if r["period_error_us"] is not None]
    fig, ax = plt.subplots(figsize=(12, 5), layout="constrained")
    if matched:
        ax.scatter([r["ack_indicated_hz_stable"] for r in matched],
                   [r["period_error_us"] for r in matched],
                   s=12, alpha=.55)
        ax.axhline(0, ls="--", color="black", alpha=.6)
    else:
        ax.text(.5, .5, "No intervals with stable ACK state",
                ha="center", va="center", transform=ax.transAxes)
    ax.set(xlabel="ACK-indicated SF (Hz)",
           ylabel="Measured minus commanded period (µs)",
           title=f"Record {record}: approved onset period error (diagnostic)")
    ax.grid(alpha=.2)
    fig.savefig(path, dpi=145)
    plt.close(fig)


def plot_outlier_windows(folder, rows, *, record):
    """Individual frequency outliers with neighboring approved intervals."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    flagged = [i for i, row in enumerate(rows)
               if row["baseline_ack_diagnostic"] ==
               "OUTSIDE_EXPLORATORY_TOLERANCE"]
    index_rows = []
    for n, i in enumerate(flagged, 1):
        r = rows[i]
        t0 = r["start_scope_s"]
        window = rows[max(0, i-7):min(len(rows), i+8)]
        center = .5*(r["start_scope_s"]+r["end_scope_s"])
        t = [(.5*(x["start_scope_s"]+x["end_scope_s"])-center)*1000
             for x in window]
        measured = [x["measured_frequency_hz"] for x in window]
        ack = [(x["ack_indicated_hz_stable"]
                if x["ack_indicated_hz_stable"] is not None else np.nan)
               for x in window]
        requested = [(x["mcu_requested_hz_stable"]
                      if x["mcu_requested_hz_stable"] is not None else np.nan)
                     for x in window]
        fig, ax = plt.subplots(figsize=(9, 4.5),
                               layout="constrained")
        ax.plot(t, measured, marker="o", markersize=4,
                label="Approved physical inverse periods")
        ax.plot(t, ack, marker=".", ls="--",
                label="Stable ACK frequency (where available)")
        ax.plot(t, requested, marker=".", ls=":",
                label="Stable MCU requested frequency (where available)")
        ax.axvline(0, color="tab:red", alpha=.7,
                   label="Flagged interval midpoint")
        ax.set(xlabel="Time relative to outlier midpoint (ms)",
               ylabel="Frequency (Hz)",
               title=(f"Record {record}, t={t0:.3f}s: "
                      "nearby physical intervals / MCU / ACK"))
        ax.grid(alpha=.2)
        ax.legend(fontsize=8)
        filename = f"outlier_{n:03d}.png"
        fig.savefig(folder/filename, dpi=130)
        plt.close(fig)
        index_rows.append({
            "record": record, "outlier_index": n,
            "start_scope_s": r["start_scope_s"],
            "end_scope_s": r["end_scope_s"],
            "frequency_hz": r["measured_frequency_hz"],
            "ack_hz": r["ack_indicated_hz_stable"],
            "frequency_error_hz": r["frequency_error_hz"],
            "period_error_us": r["period_error_us"],
            "adjacent_outlier_pair": r["adjacent_outlier_pair"],
            "strict_ack_assessment": r["strict_ack_assessment"],
            "plot": filename,
        })
    return index_rows


def run(run_dir, *, taxel=None):
    root = Path(run_dir).expanduser().resolve()
    meta = json.loads((root/"metadata.json").read_text())
    original = json.loads((root/"epoch_clock_analysis.json").read_text())
    cv = json.loads((root/"analysis"/"clock_model_cv_summary.json").read_text())
    if float(cv["block_s"]) != 2.:
        raise ValueError("First run evaluate_clock_models --block-s 2")
    epoch = {int(x["record_index"]): x for x in original["records"]}
    models = {int(x["record_index"]): x for x in cv["records"]}
    nano = read_nano_frames(root/"nano_serial.jsonl")

    mcu_dir = root/"mcu_matrix32"
    rid = choose_recording(mcu_dir)
    mcu = load_recordings(mcu_dir, rid)
    source = mcu.get("experiment_settings", {}).get("configuration", {}).get(
        "stimulation_source", {})
    taxel = int(source.get("taxel", 11) if taxel is None else taxel)
    encoding = source.get("encoding", "sa_ii")
    if taxel not in range(12) or encoding not in ("sa_ii", "ra_i"):
        raise ValueError("Invalid selected taxel/encoding")
    mcu_pc = np.asarray(mcu["taxel_pc_ns"], dtype=np.int64)
    hz = (mcu["sa_rate_hz"] if encoding == "sa_ii"
          else mcu["ra_rate_hz"])[:, taxel]
    ordering = np.argsort(mcu_pc, kind="stable")
    mcu_pc, hz = mcu_pc[ordering], hz[ordering]
    traces = list(mcu_dir.glob("*_matrix32_commands.jsonl"))
    if len(traces) != 1:
        raise ValueError("Expected one Matrix32 BLE command trace")
    ack = acknowledged_frequency_states(read_events(traces[0]))
    ack = sorted(ack, key=lambda x: int(x["pc_monotonic_ns"]))
    ack_pc = np.asarray([int(a["pc_monotonic_ns"]) for a in ack],
                        dtype=np.int64)

    analysis = root/"analysis"
    analysis.mkdir(exist_ok=True)
    all_intervals, all_sweeps, all_outliers, gray_stats = [], [], [], []
    by_record = []
    for record in sorted(meta["records"], key=lambda r: int(r["index"])):
        idx = int(record["index"])
        folder = analysis/f"record_{idx:05d}"
        folder.mkdir(parents=True, exist_ok=True)
        ch1 = root/record["channels"]["CH1"]["path"]
        _, onsets = load_approved_onsets(folder/"pulse_review", ch1)
        pairs, problem = _frame_pairs(root, record, nano)
        if problem:
            raise ValueError(f"Record {idx}: {problem}")
        x, y = _xy(pairs)
        spacing, spacing_summary = gray_spacing(pairs)
        spacing_summary["record"] = idx
        gray_stats.append(spacing_summary)
        write_csv(folder/"gray_frame_spacing.csv", spacing, [
            "scope_s", "scope_spacing_ms", "nano_spacing_ms",
            "spacing_difference_us", "instantaneous_nano_scope_rate_ppm",
        ])
        draw_gray_spacing(folder/"gray_frame_spacing.png", spacing,
                          record=idx)

        model, model_status = select_clock_model(models[idx])
        if model is None:
            raise ValueError(f"Record {idx}: no usable candidate mapping")
        times = np.asarray([float(r["time_s"]) for r in onsets], dtype=float)
        valid = (times >= x[0]) & (times <= x[-1])
        host = _nano_to_host_float(predict(model, x, y, times), epoch[idx]["fit"])
        rows, sweeps, offsets = evaluate_scenarios(
            times, onsets, host, valid, record=idx,
            model=model, model_status=model_status, epoch=epoch[idx],
            cv_model=models[idx]["models"][model],
            mcu_pc=mcu_pc, rates=hz, ack_pc=ack_pc, ack_states=ack,
        )
        write_csv(folder/"frequency_robustness_intervals.csv", rows, FIELDS)
        plot_sweep(folder/"frequency_robustness_sweep.png", sweeps, record=idx)
        plot_period_error(folder/"frequency_period_error.png", rows, record=idx)
        outdir = folder/"frequency_outlier_windows"
        outdir.mkdir(exist_ok=True)
        outliers = plot_outlier_windows(outdir, rows, record=idx)
        all_outliers.extend(outliers)
        all_intervals.extend(rows)
        all_sweeps.extend(sweeps)
        base = next(r for r in sweeps if r["offset_scale"] == 1.
                    and r["transition_guard_ms"] == 5.)
        strict = next(r for r in sweeps if r["offset_scale"] == 3.
                      and r["transition_guard_ms"] == 50.)
        summary = {
            "record": idx, "clock_model": model,
            "clock_model_status": model_status,
            "approved_intervals": len(rows),
            "baseline_ack_stable": base["stable_ack"],
            "strict_ack_stable": strict["stable_ack"],
            "all_sensitivity_scenarios_ack_stable": sum(
                bool(r["stable_ack_across_all_sensitivity_scenarios"])
                for r in rows),
            "baseline_ack_outside_tolerance": base["ack_outside_tolerance"],
            "strict_ack_outside_tolerance": strict["ack_outside_tolerance"],
            "outlier_pair_count": sum(
                bool(r["adjacent_outlier_pair"]) for r in rows),
            "baseline_frequency_error_hz":
                base["ack_frequency_error_hz"],
            "strict_frequency_error_hz":
                strict["ack_frequency_error_hz"],
            "baseline_period_error_us": base["ack_period_error_us"],
            "strict_period_error_us": strict["ack_period_error_us"],
            "gray_spacing": spacing_summary,
            "uncertainty_is_a_sensitivity_test_not_a_bound": True,
        }
        by_record.append(summary)
        print(f"[FREQ AUDIT {idx}] n={len(rows)}, ACK stable "
              f"{base['stable_ack']} -> {strict['stable_ack']}, "
              f"outliers {base['ack_outside_tolerance']} -> "
              f"{strict['ack_outside_tolerance']}")
    write_csv(analysis/"frequency_robustness_sweep.csv", [
        {key: value for key, value in r.items()
         if key not in ("ack_period_error_us", "ack_frequency_error_hz")}
        | {
            "median_abs_period_error_us":
                r["ack_period_error_us"]["median_abs"],
            "p90_abs_period_error_us":
                r["ack_period_error_us"]["p90_abs"],
            "median_abs_frequency_error_hz":
                r["ack_frequency_error_hz"]["median_abs"],
        } for r in all_sweeps
    ], [
        "record", "clock_model", "clock_model_status", "offset_scale",
        "transition_guard_ms", "approved_intervals", "within_gray_support",
        "stable_mcu", "stable_ack", "both_stable",
        "ack_within_tolerance", "ack_outside_tolerance",
        "clock_offset_interval_width_ms", "gray_holdout_p95_us",
        "absolute_clock_error_guaranteed", "median_abs_period_error_us",
        "p90_abs_period_error_us", "median_abs_frequency_error_hz",
    ])
    write_csv(analysis/"frequency_robustness_all_intervals.csv",
              all_intervals, FIELDS)
    write_csv(analysis/"frequency_discrepancies.csv", all_outliers, [
        "record", "outlier_index", "start_scope_s", "end_scope_s",
        "frequency_hz", "ack_hz", "frequency_error_hz",
        "period_error_us", "adjacent_outlier_pair",
        "strict_ack_assessment", "plot",
    ])
    payload = {
        "schema": "matrix32_frequency_robustness_v1",
        "record_summaries": by_record,
        "total_approved_intervals": len(all_intervals),
        "total_baseline_outliers": sum(
            r["baseline_ack_outside_tolerance"] for r in by_record),
        "all_sensitivity_scenarios": {
            "extra_gray_holdout_p95_multiplier": SCALES,
            "minimum_prior_cycle_guard_ms": GUARDS_MS,
        },
        "causal_offset_intervals_conditional_on_fitted_nano_pc_slope": True,
        "gray_holdout_p95_not_guaranteed_physical_gpio_error": True,
        "no_physical_command_application_time_measurement": True,
        "same_cross_validated_model_used_as_prior_analysis": True,
        "original_clock_maps_unchanged": True,
        "source_locked_pulse_approvals_unchanged": True,
    }
    atomic_json(analysis/"frequency_robustness_summary.json", payload)
    md = [
        "# Frequency tracking audit: uncertainty, discrepancies and Gray timing", "",
        "**Source:** source-locked, operator-approved negative CH1 onsets. "
        "No positive-edge detector, threshold tuning, or gap interpolation.",
        "",
        "A comparison survives the tested sensitivity assumptions if the "
        "MCU/ACK state is stable throughout the conditional causal-offset "
        "interval widened by a specified held-out Gray timing margin. "
        "These scenarios are NOT calibrated absolute-error bounds.",
        "",
        "| Record | Approved intervals | Baseline ACK stable | "
        "Strict ACK stable | Stable in every scenario | "
        "Baseline outliers | Strict outliers | Baseline median abs "
        "period error (µs) |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in by_record:
        md.append(
            f"| {r['record']} | {r['approved_intervals']} | "
            f"{r['baseline_ack_stable']} | {r['strict_ack_stable']} | "
            f"{r['all_sensitivity_scenarios_ack_stable']} | "
            f"{r['baseline_ack_outside_tolerance']} | "
            f"{r['strict_ack_outside_tolerance']} | "
            f"{r['baseline_period_error_us']['median_abs']} |"
        )
    md.extend([
        "", "The *strict* scenario uses 3× the Gray held-out p95 margin "
        "and a minimum 50-ms prior-cycle guard, versus 1× and 5 ms "
        "for the baseline. The actual guard is the larger of that "
        "minimum and the preceding ACK period. The measured physical "
        "period remains the approved onset-to-onset period for both.",
        "",
        "## Out-of-tolerance interval investigation", "",
        f"Baseline discrepancies: **{len(all_outliers)}**. "
        "See [every interval](frequency_discrepancies.csv). "
        "Paired long/short periods and changes around output transitions "
        "are hypotheses to inspect, NOT device failures.",
        "",
        "Per-outlier windows are saved under each "
        "\`record_0000N/frequency_outlier_windows/\` directory. The "
        "plots show neighboring approved intervals and stable ACK "
        "frequencies where association exists; they do **not** prove "
        "device transition/application latency.",
        "",
        "## Period error and comparison robustness", "",
        "For each stable, active ACK association, "
        "\`period_error_us = 1e6 × (T_measured_s − 1/f_ACK)\`. "
        "A zero error means matching periods; accuracy percentages "
        "use only the eligible subset, not all commands or pulses.",
        "",
    ])
    for r in by_record:
        i = r["record"]
        md.extend([
            f"### Record {i}", "",
            f"Clock model: **{r['clock_model']}** "
            f"({r['clock_model_status']}).",
            f"Gray frame spacing residual p95 absolute error: "
            f"{r['gray_spacing']['relative_frame_spacing_error_us']['p95_abs']} µs.",
            "",
            f"![Record {i} stability across clock and guard assumptions]"
            f"(record_{i:05d}/frequency_robustness_sweep.png)", "",
            f"![Record {i} measured-minus-commanded period errors]"
            f"(record_{i:05d}/frequency_period_error.png)", "",
            f"![Record {i} physical CH2 Gray frame spacing diagnostics]"
            f"(record_{i:05d}/gray_frame_spacing.png)", "",
        ])
    md.extend([
        "## Limitations and follow-up", "",
        "- A candidate Gray interpolation model may predict Nano "
        "timestamps well without proving those timestamps coincide "
        "with the actual GPIO transitions.",
        "- Passing USB midpoint and causal offset checks does not "
        "remove unknown one-way transport asymmetry or Matrix32 "
        "command processing latency.",
        "- Changing guard assumptions changes which samples are "
        "considered comparable; small errors for a highly selected "
        "subset cannot be generalized to all commands.",
        "- Inspect individual outlier windows against raw CH1/CH2 "
        "traces before any reclassification of physical pulse events.",
        "- For definitive command-to-pulse latency, acquire a shared "
        "electrical command marker or independently validate timing.",
        "",
        "[All approved intervals](frequency_robustness_all_intervals.csv) · "
        "[9-scenario sweep](frequency_robustness_sweep.csv) · "
        "[Machine-readable summary](frequency_robustness_summary.json).",
    ])
    report = analysis/"frequency_robustness_report.md"
    report.write_text("\n".join(md)+"\n", encoding="utf-8")
    print(f"[FREQ AUDIT] Wrote {report}")
    return payload


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--taxel", type=int, choices=range(12), default=None)
    args = p.parse_args(argv)
    return run(args.run_dir, taxel=args.taxel)


if __name__ == "__main__":
    main()
