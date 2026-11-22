"""Offline quality diagnostics for Nano four-timestamp clock calibration.

This module deliberately does NOT replace the operational active_clock_fit:
it compares multiple fits and checks physical feasibility of clock offsets.
Results are diagnostics, not validated absolute timing accuracy.
"""
from __future__ import annotations

import math
import statistics

from communication.clock_sync import active_clock_fit, _percentile


def transport_delay_ns(row, slope_ns_per_us):
    """Round trip minus Nano measured processing, on PC time scale."""
    return (row["t4_host_ns"] - row["t1_host_ns"]
            - slope_ns_per_us * (row["t3_nano_us"] - row["t2_nano_us"]))


def _fit_brief(fit):
    return {
        "sample_count": fit["sample_count"],
        "span_s": fit["span_s"],
        "drift_ppm_nano_to_host": fit["drift_ppm_nano_to_host"],
        "rms_residual_us": fit["rms_residual_us"],
        "max_abs_residual_us": fit["max_abs_residual_us"],
        "best_exchange_offset_halfwidth_us":
            fit["best_exchange_offset_halfwidth_us"],
    }


def _clock_intersection(exchanges, slope, anchor_nano_us, *,
                        anchor_host_ns=None):
    """Intersect nonnegative-delay intervals for host time at Nano anchor.

    Per record: t1 <= a*t2 + b, and a*t3+b <= t4.
    Use relative coordinates so 1e14 ns host values do not lose precision.
    The interval is conditional on the supplied slope being correct and on
    correctly ordered software timestamps. It is not an absolute error bound.
    """
    if anchor_host_ns is None:
        anchor_host_ns = exchanges[0]["t1_host_ns"]
    low, high = -math.inf, math.inf
    for row in exchanges:
        lower = ((row["t1_host_ns"] - anchor_host_ns)
                 - slope * (row["t2_nano_us"] - anchor_nano_us))
        upper = ((row["t4_host_ns"] - anchor_host_ns)
                 - slope * (row["t3_nano_us"] - anchor_nano_us))
        if lower > upper + 100_000:
            raise ValueError("SYNC timestamps violate nonnegative-delay ordering")
        low = max(low, lower)
        high = min(high, upper)
    feasible = high >= low
    result = {
        "feasible": feasible,
        "anchor_nano_us": anchor_nano_us,
        "anchor_host_reference_ns": anchor_host_ns,
        "intersection_lower_relative_ns": low,
        "intersection_upper_relative_ns": high,
        "intersection_width_us": ((high-low)/1000 if feasible else None),
        "infeasibility_gap_us": ((low-high)/1000 if not feasible else 0.0),
        "interpretation": (
            "Intersection is conditional on fitted slope and nonnegative "
            "software-level transport delays. It is not calibrated "
            "one-way-delay asymmetry or a guaranteed timing accuracy."
        ),
    }
    return result


def _window_fits(exchanges, window_count, min_samples, min_span_s):
    """Non-overlapping chronological segments, for stationarity diagnostics."""
    n = len(exchanges)
    windows = []
    for i in range(window_count):
        lo = i*n//window_count
        hi = (i+1)*n//window_count
        segment = exchanges[lo:hi]
        try:
            fit = active_clock_fit(segment, min_samples=min_samples,
                                   min_span_s=min_span_s)
        except ValueError as exc:
            windows.append({
                "index": i+1, "status": "not_qualified",
                "sample_count": len(segment), "reason": str(exc),
            })
        else:
            windows.append({"index": i+1, "status": "qualified",
                            **_fit_brief(fit)})
    return windows


def _stratified_low_latency(exchanges, base_slope, *,
                            bins=6, keep_fraction=0.25):
    """Keep the lowest transport-delay observations within EACH time bin.

    Time stratification avoids selecting only a low-latency cluster near
    one end of the recording, which can destabilize drift estimation.
    """
    selected = []
    for index in range(bins):
        section = exchanges[index*len(exchanges)//bins:
                            (index+1)*len(exchanges)//bins]
        if not section:
            continue
        count = max(2, math.ceil(len(section)*keep_fraction))
        ranked = sorted(section, key=lambda row:
                        transport_delay_ns(row, base_slope))
        selected.extend(ranked[:count])
    selected.sort(key=lambda row: row["t1_host_ns"])
    return selected


def _holdout_checks(exchanges, *, folds=3, min_samples=8, min_span_s=5.0):
    """Chronological blocked holdout predictions using disjoint test records.

    Fit on other blocks and assess signed midpoint error on the held-out
    block. Midpoint error is NOT an absolute clock-offset error: transport
    asymmetry remains and can be time-dependent.
    """
    if folds < 2:
        raise ValueError("Need at least two holdout folds")
    tests = []
    n = len(exchanges)
    for index in range(folds):
        lo, hi = index*n//folds, (index+1)*n//folds
        held_out = exchanges[lo:hi]
        training = exchanges[:lo] + exchanges[hi:]
        if not held_out:
            continue
        try:
            fit = active_clock_fit(training, min_samples=min_samples,
                                   min_span_s=min_span_s)
        except ValueError as exc:
            tests.append({"fold": index+1, "status": "not_qualified",
                          "test_count": len(held_out), "reason": str(exc)})
            continue
        anchor_nano = fit["anchor_nano_us"]
        anchor_host = fit["anchor_host_monotonic_ns"]
        slope = fit["slope_host_ns_per_nano_us"]
        offset = fit["relative_offset_ns"]
        errors_us = []
        for row in held_out:
            nano_mid = (row["t2_nano_us"] + row["t3_nano_us"])/2
            # Subtract host anchor before float conversion to avoid
            # precision loss for large monotonic timestamps.
            observed_rel_ns = ((row["t1_host_ns"]-anchor_host)
                               + (row["t4_host_ns"]-anchor_host))/2
            predicted_rel_ns = slope*(nano_mid-anchor_nano) + offset
            errors_us.append((observed_rel_ns-predicted_rel_ns)/1000)
        rms = math.sqrt(sum(v*v for v in errors_us)/len(errors_us))
        tests.append({
            "fold": index+1, "status": "qualified",
            "train_count": len(training), "test_count": len(held_out),
            "train_span_s": fit["span_s"],
            "train_drift_ppm": fit["drift_ppm_nano_to_host"],
            "test_midpoint_rms_us": rms,
            "test_midpoint_bias_us": statistics.mean(errors_us),
            "test_midpoint_max_abs_us": max(map(abs, errors_us)),
        })
    qualified = [x for x in tests if x["status"] == "qualified"]
    return {
        "folds": tests,
        "qualified_fold_count": len(qualified),
        "worst_holdout_midpoint_rms_us": (
            max(x["test_midpoint_rms_us"] for x in qualified)
            if qualified else None
        ),
        "max_abs_holdout_midpoint_bias_us": (
            max(abs(x["test_midpoint_bias_us"]) for x in qualified)
            if qualified else None
        ),
        "holdout_train_drift_range_ppm": (
            max(x["train_drift_ppm"] for x in qualified)
            - min(x["train_drift_ppm"] for x in qualified)
            if len(qualified) >= 2 else None
        ),
        "interpretation": (
            "Blocked held-out midpoint predictions test time-generalization "
            "within the recording, not physical offset accuracy; asymmetric "
            "USB delays affect both training and test observations."
        ),
    }


def diagnose_active_clock(exchanges, *,
                          min_samples=8, min_span_s=5.0,
                          bins=6, keep_fraction=0.25):
    """Compare all-data, stratified lowest-delay, and window clock fits.

    No method here changes the canonical active fit. Report slope
    sensitivity, clock feasibility and latency quantiles for review.
    """
    if not (0 < keep_fraction <= 1):
        raise ValueError("keep_fraction must lie in (0,1]")
    if bins < 2:
        raise ValueError("Need at least two chronological selection bins")
    baseline = active_clock_fit(exchanges, min_samples=min_samples,
                                min_span_s=min_span_s)
    slope = baseline["slope_host_ns_per_nano_us"]
    selected = _stratified_low_latency(
        exchanges, slope, bins=bins, keep_fraction=keep_fraction)
    robust = active_clock_fit(selected, min_samples=min_samples,
                              min_span_s=min_span_s)

    all_intersection = _clock_intersection(
        exchanges, slope, baseline["anchor_nano_us"],
        anchor_host_ns=baseline["anchor_host_monotonic_ns"])
    selected_intersection = _clock_intersection(
        selected, robust["slope_host_ns_per_nano_us"],
        robust["anchor_nano_us"],
        anchor_host_ns=robust["anchor_host_monotonic_ns"])
    windows = _window_fits(exchanges, 3, min_samples=min_samples,
                           min_span_s=min_span_s)
    holdout = _holdout_checks(exchanges, folds=3,
                              min_samples=min_samples,
                              min_span_s=min_span_s)
    good = [row["drift_ppm_nano_to_host"] for row in windows
            if row["status"] == "qualified"]
    delay = sorted(transport_delay_ns(row, slope)/1000 for row in exchanges)
    result = {
        "status": "diagnostic_only_no_production_fit_replacement",
        "baseline": _fit_brief(baseline),
        "stratified_low_latency": {
            **_fit_brief(robust),
            "selection": {
                "bins": bins,
                "fraction_per_bin": keep_fraction,
                "selected_exchanges": len(selected),
                "selected_ratio": len(selected)/len(exchanges),
            },
        },
        "difference_low_latency_minus_baseline_ppm": (
            robust["drift_ppm_nano_to_host"]
            - baseline["drift_ppm_nano_to_host"]
        ),
        "absolute_slope_sensitivity_ppm": abs(
            robust["drift_ppm_nano_to_host"]
            - baseline["drift_ppm_nano_to_host"]
        ),
        "chronological_window_fits": windows,
        "blocked_holdout_validation": holdout,
        "window_drift_range_ppm": (
            max(good)-min(good) if len(good) >= 2 else None
        ),
        "window_drift_stddev_ppm": (
            statistics.stdev(good) if len(good) >= 2 else None
        ),
        "offset_constraints": {
            "all_exchanges_conditional_on_baseline_slope": all_intersection,
            "low_latency_conditional_on_selected_slope": selected_intersection,
        },
        "transport_delay_us": {
            "p10": _percentile(delay, 10),
            "median": _percentile(delay, 50),
            "p90": _percentile(delay, 90),
        },
        "warnings": [],
        "interpretation": (
            "Low-latency selection, subwindow drift and offset-interval "
            "intersection are diagnostics only. A feasible conditional "
            "interval is not a validated hardware timestamp accuracy. "
            "Small RMS does not exclude constant asymmetric USB delays."
        ),
    }
    if not all_intersection["feasible"]:
        result["warnings"].append(
            "No common causal offset interval at the baseline slope; "
            "slope estimate, timestamps or assumed delay model may be inconsistent."
        )
    if not selected_intersection["feasible"]:
        result["warnings"].append(
            "Low-latency fitted slope yields no common causal offset interval."
        )
    if holdout["qualified_fold_count"] < 3:
        result["warnings"].append(
            "Not all blocked holdout folds qualified; prediction stability unverified."
        )
    if (holdout["max_abs_holdout_midpoint_bias_us"] is not None
            and holdout["max_abs_holdout_midpoint_bias_us"] > 1000):
        result["warnings"].append(
            "Held-out midpoint bias exceeds 1 ms; clock mapping may not generalize."
        )
    if len(good) < 2:
        result["warnings"].append(
            "Insufficient qualified chronological windows to characterize drift stability."
        )
    if result["absolute_slope_sensitivity_ppm"] > 50:
        result["warnings"].append(
            "Clock slope changes >50 ppm with low-delay selection; treat the drift fit as unstable."
        )
    if result["window_drift_range_ppm"] is not None and result["window_drift_range_ppm"] > 100:
        result["warnings"].append(
            "Chronological window drift spread >100 ppm; investigate nonstationary latency or oscillator."
        )
    return result
