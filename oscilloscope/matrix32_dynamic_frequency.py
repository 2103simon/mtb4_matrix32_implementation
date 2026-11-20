"""Dynamic, per-cycle Matrix32 frequency checks against BLE ACK observations.

No stimulation or hardware access. Results are diagnostic, not a calibrated
latency measurement. An ACK timestamp is NOT the device application time.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import math
import numpy as np


def map_ack_states(states, mapping, t_start, t_end):
    """Convert BLE host-monotonic ACK observations to provisional scope time."""
    if mapping is None:
        return []
    a = int(mapping["first_sample_pc_monotonic_ns_est"])
    b = int(mapping["last_sample_pc_monotonic_ns_est"])
    if b <= a or t_end <= t_start:
        raise ValueError("Invalid scope/host mapping")
    return [
        {**state, "scope_time_s_est": float(
            t_start + ((int(state["pc_monotonic_ns"])-a)/(b-a))*(t_end-t_start)
        )}
        for state in sorted(states, key=lambda s: int(s["pc_monotonic_ns"]))
    ]


def analyze_dynamic_cycles(pulse_times, ack_states, mapping, t_start, t_end,
                           settle_s=0.005, tolerance_hz=8.0,
                           tolerance_fraction=0.15):
    """Compare each complete detected positive-phase interval with last ACK.

    Count separately unclassified intervals spanning ACK transitions and those
    immediately after updates. The comparison assumes the state represented
    by an ACK is applied by that observation time. It deliberately does NOT
    estimate or correct a post-hoc lag to maximize fit.
    """
    if not (math.isfinite(settle_s) and settle_s >= 0
            and math.isfinite(tolerance_hz) and tolerance_hz > 0
            and math.isfinite(tolerance_fraction) and 0 <= tolerance_fraction <= 1):
        raise ValueError("Invalid dynamic tracking configuration")
    pulses = np.sort(np.asarray(pulse_times, dtype=float))
    states = map_ack_states(ack_states, mapping, t_start, t_end)
    result = {
        "method": "single positive-phase pulse interval vs most recent ACK-indicated SF",
        "time_map": "provisional scope-to-Nano-to-PC affine mapping (offset not independently calibrated)",
        "settle_guard_s": float(settle_s),
        "tolerance_hz": float(tolerance_hz),
        "tolerance_fraction": float(tolerance_fraction),
        "physical_latency_measured": False,
        "complete_intervals": max(0, len(pulses)-1),
        "classified": 0,
        "compatible": 0,
        "discrepant": 0,
        "not_comparable": 0,
        "not_comparable_reasons": {},
        "median_absolute_error_hz": None,
        "median_absolute_error_pct": None,
        "compatibility_fraction": None,
        "per_frequency": [],
        "intervals": [],
        "ack_state_count": len(states),
    }
    if len(pulses) < 2:
        return result
    if not states:
        result["not_comparable"] = len(pulses)-1
        result["not_comparable_reasons"] = {"missing_scope_host_mapping_or_ack": len(pulses)-1}
        return result
    x = np.asarray([s["scope_time_s_est"] for s in states], dtype=float)
    first = pulses[:-1]
    second = pulses[1:]
    periods = second-first
    ix1 = np.searchsorted(x, first, side="right")-1
    ix2 = np.searchsorted(x, second, side="right")-1
    rows = []
    for left, right, dt, i, j in zip(first, second, periods, ix1, ix2):
        row = {"middle_scope_s": float((left+right)/2),
               "period_ms": float(dt*1000),
               "measured_hz": float(1/dt) if dt > 0 else None,
               "ack_expected_hz": None, "error_hz": None,
               "error_pct": None, "assessment": "not_comparable"}
        if dt <= 0:
            row["reason"] = "invalid_period"
        elif int(i) < 0:
            row["reason"] = "before_first_ack"
        elif int(i) != int(j):
            row["reason"] = "spans_ack_transition"
        else:
            state = states[int(i)]
            hz = state.get("expected_hz")
            if hz is None or hz <= 0 or not state.get("output_active_assumed", True):
                row["reason"] = "output_muted_or_unknown"
            elif left - x[int(i)] < settle_s:
                row["reason"] = "first_interval_after_ack"
            else:
                hz = float(hz)
                error = row["measured_hz"]-hz
                tolerance = max(tolerance_hz, tolerance_fraction*hz)
                row.update({
                    "ack_expected_hz": hz, "error_hz": float(error),
                    "error_pct": float(100*error/hz),
                    "assessment": ("compatible_provisional" if abs(error) <= tolerance
                                   else "discrepant_provisional"),
                })
        rows.append(row)
    result["intervals"] = rows
    kinds = Counter(r["assessment"] for r in rows)
    result["compatible"] = kinds["compatible_provisional"]
    result["discrepant"] = kinds["discrepant_provisional"]
    result["classified"] = result["compatible"]+result["discrepant"]
    result["not_comparable"] = kinds["not_comparable"]
    result["not_comparable_reasons"] = dict(
        Counter(r.get("reason", "unknown") for r in rows
                if r["assessment"] == "not_comparable")
    )
    good = [r for r in rows if r["ack_expected_hz"] is not None]
    if good:
        result["median_absolute_error_hz"] = float(
            np.median([abs(r["error_hz"]) for r in good]))
        result["median_absolute_error_pct"] = float(
            np.median([abs(r["error_pct"]) for r in good]))
        result["compatibility_fraction"] = result["compatible"]/len(good)
    buckets = defaultdict(list)
    for row in good:
        buckets[int(row["ack_expected_hz"])].append(row)
    for hz, members in sorted(buckets.items(), key=lambda item: -len(item[1])):
        result["per_frequency"].append({
            "ack_hz": hz,
            "cycle_intervals": len(members),
            "compatible": sum(r["assessment"] == "compatible_provisional"
                              for r in members),
            "discrepant": sum(r["assessment"] == "discrepant_provisional"
                              for r in members),
            "median_measured_hz": float(np.median(
                [r["measured_hz"] for r in members])),
            "median_abs_error_hz": float(np.median(
                [abs(r["error_hz"]) for r in members])),
        })
    return result


def plot_dynamic_tracking(path, comparison, ack_states, mapping,
                          scope_start_s, scope_end_s):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    states = map_ack_states(ack_states, mapping, scope_start_s, scope_end_s)
    intervals = comparison["intervals"]
    fig, ax = plt.subplots(figsize=(15, 5), layout="constrained")
    if intervals:
        for assessment, label, color in [
            ("compatible_provisional", "Within tolerance (provisional)", "tab:green"),
            ("discrepant_provisional", "Outside tolerance (provisional)", "tab:red"),
            ("not_comparable", "Transition / unclassified pulse interval", "tab:gray"),
        ]:
            rows = [r for r in intervals if r["assessment"] == assessment
                    and r["measured_hz"] is not None]
            if rows:
                ax.scatter([r["middle_scope_s"] for r in rows],
                           [min(r["measured_hz"], 400) for r in rows],
                           s=7, alpha=0.55, color=color, label=label)
    if states:
        tx, sf = [], []
        for state in states:
            t = float(state["scope_time_s_est"])
            if t < scope_start_s or t > scope_end_s:
                continue
            sf_value = state.get("expected_hz")
            tx.append(t)
            sf.append(np.nan if sf_value is None else float(sf_value))
        if tx:
            ax.step(tx, sf, where="post", color="black", linewidth=1.2,
                    alpha=0.9, label="ACK-indicated SF (provisional)")
    ax.set(xlim=(scope_start_s, scope_end_s),
           xlabel="Time relative to scope waveform (s)",
           ylabel="Positive-pulse instantaneous / ACK frequency (Hz)",
           title="Dynamic Matrix32 tracking: each physical pulse interval vs latest ACK")
    ax.grid(alpha=0.2)
    ax.legend(loc="upper right", fontsize=8, ncol=2)
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return str(path)
