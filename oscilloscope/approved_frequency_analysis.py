"""Source-locked approved-negative-onset frequency analysis.

Only the existing operator-reviewed CH1 onset inventory is treated as physical
pulse evidence. Host/MCU/ACK time overlays are PROVISIONAL; no exact electrical
latency, state application time, or per-command success is inferred.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter
import csv
import json
import math
from pathlib import Path

import numpy as np

from oscilloscope.apply_pulse_review import load_approved_onsets
from oscilloscope.matrix32_dynamic_frequency import map_ack_states
from oscilloscope.record_scope import atomic_json

FIELDS = (
    "record_index", "start_onset_id", "end_onset_id",
    "start_scope_s", "end_scope_s", "mid_scope_s", "period_ms",
    "physical_inverse_period_hz", "mcu_taxel_value_nearest_provisional",
    "mcu_requested_hz_nearest_provisional", "mcu_sample_age_ms_provisional",
    "ack_expected_hz_provisional", "ack_state_age_ms_provisional",
    "ack_state_at_start", "ack_state_at_end", "ack_state_assessment",
    "abs_frequency_error_hz_provisional", "relative_error_pct_provisional",
    "clock_status", "cross_clock_alignment_verified",
    "physical_application_latency_verified",
)

# Same numeric screening tolerance as the legacy exploratory dynamic check.
# Not a device-acceptance specification; changed cycle may wait on prior period.
DIAGNOSTIC_ABS_HZ = 8.0
DIAGNOSTIC_REL_FRAC = 0.15


def _float(value):
    return float(value) if value is not None else None


def _quantiles(values):
    a = np.asarray(values, dtype=float)
    if not len(a):
        return {"min": None, "p10": None, "median": None,
                "p90": None, "max": None, "mean": None}
    return {
        "min": float(np.min(a)), "p10": float(np.percentile(a, 10)),
        "median": float(np.median(a)), "p90": float(np.percentile(a, 90)),
        "max": float(np.max(a)), "mean": float(np.mean(a))
    }


def _host_ns(scope_s, first, last, mapping):
    if mapping is None or last <= first:
        return None
    h0 = int(mapping["first_sample_pc_monotonic_ns_est"])
    h1 = int(mapping["last_sample_pc_monotonic_ns_est"])
    return int(h0 + (scope_s-first)/(last-first)*(h1-h0))


def _saved_csv(path, rows, fields):
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _acked_row(row, state, scope_age_ms, status):
    row["ack_state_assessment"] = status
    if state is not None:
        row["ack_expected_hz_provisional"] = (
            state.get("expected_hz") if state.get("output_active_assumed", True)
            else None)
        row["ack_state_age_ms_provisional"] = scope_age_ms
    return row


def compute_approved_intervals(*, index, approved_onsets, scope_start,
                               scope_end, mapping, mcu, taxel, encoding,
                               ack_states):
    """Classify every approved onset-to-onset interval; no gap interpolation.

    ACK comparison is explicitly diagnostic and only when the interval is
    wholly inside one ACK state with an additional conservative guard of
    max(5 ms, previous ACK period). Unknown prior-period state cannot be
    independently resolved; labels never claim verified physical application.
    """
    if taxel not in range(12):
        raise ValueError("Invalid selected taxel")
    if encoding not in ("sa_ii", "ra_i"):
        raise ValueError("Unknown stimulation encoding")
    times = np.asarray([float(r["time_s"]) for r in approved_onsets], dtype=float)
    if len(times) > 1 and np.any(np.diff(times) <= 0):
        raise ValueError("Approved onsets are not strictly ordered")
    mapped_states = map_ack_states(ack_states, mapping, scope_start, scope_end)
    # Include ACK history before scope start; important for first interval.
    state_times = [float(s["scope_time_s_est"]) for s in mapped_states]
    source = mcu["sa_rate_hz"] if encoding == "sa_ii" else mcu["ra_rate_hz"]
    pc = np.asarray(mcu["taxel_pc_ns"], dtype=np.int64)
    pressure = np.asarray(mcu["taxel_data"])
    rows = []
    counts = Counter()
    for left, right, a, b in zip(times[:-1], times[1:],
                                  approved_onsets[:-1], approved_onsets[1:]):
        dt = float(right-left)
        phys = 1/dt
        mid = float((left+right)/2)
        row = {
            "record_index": index, "start_onset_id": a["id"],
            "end_onset_id": b["id"],
            "start_scope_s": float(left), "end_scope_s": float(right),
            "mid_scope_s": mid, "period_ms": dt*1000,
            "physical_inverse_period_hz": phys,
            "mcu_taxel_value_nearest_provisional": None,
            "mcu_requested_hz_nearest_provisional": None,
            "mcu_sample_age_ms_provisional": None,
            "ack_expected_hz_provisional": None,
            "ack_state_age_ms_provisional": None,
            "ack_state_at_start": None, "ack_state_at_end": None,
            "ack_state_assessment": "UNCLASSIFIED",
            "abs_frequency_error_hz_provisional": None,
            "relative_error_pct_provisional": None,
            "clock_status": (mapping or {}).get("status", "UNAVAILABLE"),
            "cross_clock_alignment_verified": False,
            "physical_application_latency_verified": False,
        }
        ns = _host_ns(mid, scope_start, scope_end, mapping)
        if ns is not None and len(pc):
            i = int(np.searchsorted(pc, ns, side="right")-1)
            if i >= 0 and 0 <= ns-int(pc[i]) <= 250_000_000:
                row["mcu_taxel_value_nearest_provisional"] = float(pressure[i, taxel])
                row["mcu_requested_hz_nearest_provisional"] = float(source[i, taxel])
                row["mcu_sample_age_ms_provisional"] = (ns-int(pc[i]))/1e6
        if mapping is None or not mapped_states:
            status = "UNKNOWN_NO_CLOCK_MAP_OR_ACK"
            row["ack_state_assessment"] = status
        else:
            il = bisect_right(state_times, float(left))-1
            ir = bisect_right(state_times, float(right))-1
            row["ack_state_at_start"] = il if il >= 0 else None
            row["ack_state_at_end"] = ir if ir >= 0 else None
            if il < 0:
                row["ack_state_assessment"] = "UNKNOWN_BEFORE_FIRST_ACK"
            elif il != ir:
                row["ack_state_assessment"] = "NOT_OBSERVABLE_SPANS_ACK_CHANGE"
            else:
                state = mapped_states[il]
                hz = state.get("expected_hz")
                if not state.get("output_active_assumed", True) or hz is None or hz <= 0:
                    row["ack_state_assessment"] = "NOT_OBSERVABLE_MUTED_OR_UNKNOWN"
                else:
                    # A prior slow cycle may finish after a new ACK. Guard
                    # using the previous commanded period if it is available.
                    prior = mapped_states[il-1] if il > 0 else None
                    prev_hz = (prior.get("expected_hz") if prior
                               and prior.get("output_active_assumed", True) else None)
                    if prev_hz is None or prev_hz <= 0:
                        row["ack_state_assessment"] = "NOT_OBSERVABLE_UNKNOWN_PRIOR_CYCLE"
                    else:
                        guard = max(0.005, 1.0/float(prev_hz))
                        age_s = float(left) - state_times[il]
                        row["ack_state_age_ms_provisional"] = age_s*1000
                        row["ack_expected_hz_provisional"] = float(hz)
                        if age_s < guard:
                            row["ack_state_assessment"] = "NOT_OBSERVABLE_PRIOR_CYCLE_GUARD"
                        else:
                            error = phys - float(hz)
                            threshold = max(DIAGNOSTIC_ABS_HZ,
                                            DIAGNOSTIC_REL_FRAC*float(hz))
                            row["abs_frequency_error_hz_provisional"] = abs(error)
                            row["relative_error_pct_provisional"] = 100*error/float(hz)
                            row["ack_state_assessment"] = (
                                "COMPATIBLE_DIAGNOSTIC_ONLY" if abs(error) <= threshold
                                else "POTENTIALLY_INCONSISTENT_REVIEW")
        counts[row["ack_state_assessment"]] += 1
        rows.append(row)
    comparable = sum(counts[x] for x in (
        "COMPATIBLE_DIAGNOSTIC_ONLY", "POTENTIALLY_INCONSISTENT_REVIEW"))
    compatibles = counts["COMPATIBLE_DIAGNOSTIC_ONLY"]
    errs = [r["abs_frequency_error_hz_provisional"] for r in rows
            if r["abs_frequency_error_hz_provisional"] is not None]
    summary = {
        "schema": "matrix32_approved_physical_frequency_v1",
        "record_index": index,
        "status": "OPERATOR_SOURCE_LOCKED_APPROVED",
        "onset_count": len(times), "interval_count": len(rows),
        "record_time_start_s": scope_start, "record_time_end_s": scope_end,
        "period_ms": _quantiles([r["period_ms"] for r in rows]),
        "inverse_period_hz": _quantiles(
            [r["physical_inverse_period_hz"] for r in rows]),
        "diagnostic_ack_assessment_counts": dict(counts),
        "diagnostic_comparable_intervals": comparable,
        "diagnostic_compatible_intervals": compatibles,
        "diagnostic_compatibility_fraction": (
            compatibles/comparable if comparable else None),
        "diagnostic_median_absolute_error_hz": (
            float(np.median(errs)) if errs else None),
        "clock_status": (mapping or {}).get("status", "UNAVAILABLE"),
        "cross_clock_alignment_verified": False,
        "physical_application_latency_verified": False,
        "temperature_or_electrical_current_calibrated": False,
        "interpretation": (
            "Every physical onset and interval is operator-approved. "
            "Request/ACK overlays are provisional due to clock offset; "
            "diagnostic agreement is not validated tracking accuracy. "
            "State transitions may be deferred to a later cycle."
        ),
    }
    return rows, summary, mapped_states


def plot_record(path, rows, summary, mapped_states, mcu, taxel, encoding,
                mapping, scope_start, scope_end):
    """S01/S02-inspired aligned frequency, taxel and ACK visual presentation."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(3, 1, figsize=(16, 11),
                             sharex=True, layout="constrained")
    measured = [(r["mid_scope_s"], r["physical_inverse_period_hz"])
                for r in rows]
    if measured:
        axes[0].scatter([x[0] for x in measured], [x[1] for x in measured],
                        s=9, alpha=.8, label="Approved negative-onset inverse period")
        axes[0].legend(loc="upper right")
    else:
        axes[0].text(.5, .5, "No approved physical onsets / intervals",
                     ha="center", va="center", transform=axes[0].transAxes)
    axes[0].set_ylabel("Physical 1 / period (Hz)")
    axes[0].set_title(
        f"Record {summary['record_index']}: operator-approved physical frequency")

    ns = np.asarray(mcu["taxel_pc_ns"], dtype=np.int64)
    if mapping is not None and len(ns):
        a = int(mapping["first_sample_pc_monotonic_ns_est"])
        b = int(mapping["last_sample_pc_monotonic_ns_est"])
        t = scope_start + ((ns-a)/(b-a))*(scope_end-scope_start)
        selected = (t >= scope_start) & (t <= scope_end)
        hz = (mcu["sa_rate_hz"] if encoding == "sa_ii"
              else mcu["ra_rate_hz"])[:, taxel]
        axes[1].plot(t[selected], hz[selected], lw=1,
                     label=f"MCU taxel {taxel} {encoding} requested rate")
        axes[1].legend(loc="upper right")
    else:
        axes[1].text(.5, .5, "No provisional clock mapping",
                     ha="center", va="center", transform=axes[1].transAxes)
    axes[1].set_ylabel("MCU requested (Hz)")

    visible = [
        s for s in mapped_states
        if scope_start <= s["scope_time_s_est"] <= scope_end
    ]
    if visible:
        t = [s["scope_time_s_est"] for s in visible]
        y = [float(s["expected_hz"]) if
             s.get("output_active_assumed", True)
             and s.get("expected_hz") is not None else np.nan
             for s in visible]
        axes[2].step(t, y, where="post", lw=1,
                     label="ACK-indicated SF (muted states hidden)")
        axes[2].legend(loc="upper right")
    else:
        axes[2].text(.5, .5, "No eligible ACK events during scope window",
                     ha="center", va="center", transform=axes[2].transAxes)
    axes[2].set_ylabel("ACK-indicated SF (Hz)")
    axes[2].set_xlabel("Scope-local time (s); MCU and ACK clocks PROVISIONAL")
    for ax in axes:
        ax.set_xlim(scope_start, scope_end)
        ax.grid(alpha=.2)
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return str(path)


def plot_distribution(path, rows, summary):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    period = [r["period_ms"] for r in rows]
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.5), layout="constrained")
    if period:
        axes[0].hist(period, bins=min(80, max(10, int(math.sqrt(len(period))))),
                     edgecolor="none")
        axes[1].hist([r["physical_inverse_period_hz"] for r in rows],
                     bins=min(80, max(10, int(math.sqrt(len(period))))),
                     edgecolor="none")
        median = summary["period_ms"]["median"]
        axes[0].axvline(median, ls="--", label=f"Median {median:.3f} ms")
        axes[0].legend()
    else:
        for ax in axes:
            ax.text(.5, .5, "No approved intervals", ha="center",
                    va="center", transform=ax.transAxes)
    axes[0].set(xlabel="Approved onset-to-onset period (ms)", ylabel="Count")
    axes[1].set(xlabel="Inverse period (Hz)", ylabel="Count")
    for ax in axes:
        ax.grid(alpha=.2)
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return str(path)


def plot_ack_diagnostic(path, rows):
    """Scatter of interval-derived physical Hz vs ACK Hz, diagnostics only."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 7), layout="constrained")
    statuses = (
        ("COMPATIBLE_DIAGNOSTIC_ONLY", "Compatible under diagnostic tolerance"),
        ("POTENTIALLY_INCONSISTENT_REVIEW", "Potentially inconsistent; review"),
    )
    compared = [
        row for row in rows
        if row["ack_expected_hz_provisional"] is not None
        and row["ack_state_assessment"] in dict(statuses)
    ]
    for status, label in statuses:
        group = [r for r in compared if r["ack_state_assessment"] == status]
        if group:
            ax.scatter([r["ack_expected_hz_provisional"] for r in group],
                       [r["physical_inverse_period_hz"] for r in group],
                       s=11, alpha=.65, label=f"{label} (n={len(group)})")
    if compared:
        maximum = max(
            max(float(r["ack_expected_hz_provisional"]),
                float(r["physical_inverse_period_hz"]))
            for r in compared
        )
        ax.plot([0, maximum], [0, maximum], ls="--", color="black",
                lw=1, label="Identity (only a visual reference)")
        ax.legend(loc="upper left", fontsize=8)
    else:
        ax.text(.5, .5, "No diagnostically comparable intervals",
                ha="center", va="center", transform=ax.transAxes)
    ax.set(xlabel="ACK-indicated SF (Hz; provisional host-clock map)",
           ylabel="Approved CH1 inverse period (Hz)",
           title="Diagnostic ACK compatibility — NOT calibrated tracking accuracy")
    ax.grid(alpha=.2)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return str(path)


def plot_pulse_activity(path, onsets, scope_start, scope_end):
    """Per-second approved onset count; silent bins are only within capture."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    times = np.asarray([float(r["time_s"]) for r in onsets], dtype=float)
    bin_size = 1.0
    edges = np.arange(scope_start, scope_end + bin_size, bin_size)
    if edges[-1] < scope_end:
        edges = np.r_[edges, scope_end]
    counts, _ = np.histogram(times, bins=edges)
    fig, ax = plt.subplots(figsize=(15, 4), layout="constrained")
    ax.bar(edges[:-1], counts, width=np.diff(edges), align="edge",
           edgecolor="none")
    ax.set(xlim=(scope_start, scope_end),
           xlabel="Scope-local time (s)",
           ylabel="Approved onsets per 1-s bin",
           title="Observed CH1 pulse activity (only acquired windows)")
    ax.grid(axis="y", alpha=.2)
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return str(path)


def report_markdown(summary):
    period = summary["period_ms"]
    inv = summary["inverse_period_hz"]
    counts = summary["diagnostic_ack_assessment_counts"]
    rows = [
        f"# Record {summary['record_index']}: approved physical pulse frequency", "",
        "**Source:** source-locked, operator-approved S02 negative-onset detection. "
        "No preliminary positive-edge pulses enter any physical metric here.",
        "",
        "| Metric | Result |", "| --- | ---: |",
        f"| Approved CH1 onsets | {summary['onset_count']} |",
        f"| Approved within-record intervals | {summary['interval_count']} |",
        f"| Median period | {period['median']} ms |",
        f"| Period p10 / p90 | {period['p10']} / {period['p90']} ms |",
        f"| Minimum / maximum period | {period['min']} / {period['max']} ms |",
        f"| Median inverse-period frequency | {inv['median']} Hz |",
        f"| Inverse-period frequency p10 / p90 | {inv['p10']} / {inv['p90']} Hz |",
        f"| Diagnostic comparable ACK intervals | "
        f"{summary['diagnostic_comparable_intervals']} |",
        f"| Diagnostic compatible | "
        f"{summary['diagnostic_compatible_intervals']} |",
        f"| Diagnostic compatible fraction | "
        f"{summary['diagnostic_compatibility_fraction']} |",
        f"| Clock mapping quality | {summary['clock_status']} |",
        f"| Selected MCU taxel / encoding | "
        f"{summary.get('mcu_context', {}).get('taxel', 'n/a')} / "
        f"{summary.get('mcu_context', {}).get('encoding', 'n/a')} |",
        f"| Provisional MCU rate snapshots | "
        f"{summary.get('mcu_context', {}).get('rate_snapshot_count_provisional', 'n/a')} |",
        f"| Requested MCU Hz min / max (provisional) | "
        f"{summary.get('mcu_context', {}).get('requested_rate_hz', {}).get('min')} / "
        f"{summary.get('mcu_context', {}).get('requested_rate_hz', {}).get('max')} |",
        f"| Taxel value min / max (provisional) | "
        f"{summary.get('mcu_context', {}).get('taxel_value', {}).get('min')} / "
        f"{summary.get('mcu_context', {}).get('taxel_value', {}).get('max')} |",
        f"| MCU zero-request snapshots (provisional) | "
        f"{summary.get('mcu_context', {}).get('zero_requested_snapshots_provisional', 'n/a')} |",
        f"| SF ACK events in mapped window (provisional) | "
        f"{summary.get('mcu_context', {}).get('mapped_sf_ack_events_provisional', 'n/a')} |",
        f"| SC=0 ACK events in mapped window (provisional) | "
        f"{summary.get('mcu_context', {}).get('mapped_sc_zero_ack_events_provisional', 'n/a')} |",
        "", "![Approved physical/requested/ACK tracking]"
        "(approved_frequency_tracking.png)", "",
        "![Approved interval frequency and period distributions]"
        "(approved_frequency_distribution.png)", "",        "![Per-second approved onset activity]"
        "(approved_pulse_activity.png)", "",
        "![Diagnostic ACK compatibility scatter; uncalibrated clocks]"
        "(approved_ack_diagnostic.png)", "",
        "## Complete interval decisions (all approved periods retained)", "",
        "The machine-readable [approved_frequency_tracking.csv]"
        "(approved_frequency_tracking.csv) includes every interval, "
        "its measured frequency, requested-rate context, ACK state, "
        "comparison status and explicit clock qualification.",
        "",
        "| Diagnostic classification | Intervals |",
        "| --- | ---: |",
    ]
    rows.extend(f"| {name} | {count} |" for name, count
                in sorted(counts.items(), key=lambda x: (-x[1], x[0])))
    rows += [
        "",
        "## Interpretation and limitations", "",
        "- Each frequency is 1 / (time between two consecutive *approved* onsets).",
        "- A median inverse period describes active cycles, not the "
        "time-averaged pulse rate across silent periods.",
        "- Only intervals within this one scope record are used.",
        "- Zero approved onsets means no pulses observed at this scope sensitivity; "
        "it does not independently prove zero electrical current.",
        "- ACK overlays and matching use the provisional Nano/host clock map. "
        "The uncalibrated absolute offset prevents verified per-command "
        "physical attribution or exact latency claims.",
        "- ACK transitions can take effect after the previous Matrix32 cycle "
        "completes. A diagnostic guard is applied; no constant 5-ms "
        "physical-application-time assumption is made.",
        "- Diagnostic tolerance is max(8 Hz, 15% of ACK SF). This is "
        "exploratory screening, not a calibrated compliance threshold.",
        "- The 400-Hz fast scheduler while SC=0 is transport evidence, "
        "not measured 400-Hz physical output.",
        "- Other gaps, weak/no-signal periods and clock limitations must not "
        "be converted into zero frequency or perfect tracking.",
        "",
    ]
    return "\n".join(rows)


def analyze_approved_record(root, record, mcu, ack_states, mapping, taxel,
                            encoding, scope_start, scope_end):
    root = Path(root)
    index = int(record["index"])
    output = root / "analysis" / f"record_{index:05d}"
    approval_dir = output / "pulse_review"
    source_ch1 = root / record["channels"]["CH1"]["path"]
    # Fail closed on present but stale, changed, or incomplete approval.
    approval, onsets = load_approved_onsets(approval_dir, source_ch1)
    rows, summary, states = compute_approved_intervals(
        index=index, approved_onsets=onsets, scope_start=scope_start,
        scope_end=scope_end, mapping=mapping, mcu=mcu, taxel=taxel,
        encoding=encoding, ack_states=ack_states,
    )
    summary["reviewer"] = approval["reviewer"]
    summary["review_source_ch1_sha256"] = approval["source_ch1_sha256"]
    pc = np.asarray(mcu["taxel_pc_ns"], dtype=np.int64)
    selected_hz = (mcu["sa_rate_hz"] if encoding == "sa_ii"
                   else mcu["ra_rate_hz"])[:, taxel]
    selected_taxel = mcu["taxel_data"][:, taxel]
    if mapping is not None and len(pc):
        h0 = mapping["first_sample_pc_monotonic_ns_est"]
        h1 = mapping["last_sample_pc_monotonic_ns_est"]
        mask = (pc >= h0) & (pc <= h1)
    else:
        mask = np.zeros(len(pc), dtype=bool)
    summary["mcu_context"] = {
        "taxel": taxel, "encoding": encoding,
        "rate_snapshot_count_provisional": int(np.count_nonzero(mask)),
        "taxel_value": _quantiles(selected_taxel[mask]),
        "requested_rate_hz": _quantiles(selected_hz[mask]),
        "zero_requested_snapshots_provisional": int(
            np.count_nonzero(selected_hz[mask] == 0)),
        "mapped_ack_state_events_provisional": int(sum(
            scope_start <= x["scope_time_s_est"] <= scope_end
            for x in states)),
        "mapped_sf_ack_events_provisional": int(sum(
            scope_start <= x["scope_time_s_est"] <= scope_end
            and x.get("source") == "sf_ack" for x in states)),
        "mapped_sc_zero_ack_events_provisional": int(sum(
            scope_start <= x["scope_time_s_est"] <= scope_end
            and x.get("source") == "sc_ack_muted" for x in states)),
        "clock_alignment_verified": False,
    }
    summary["approved_intervals_csv"] = str(
        approval_dir / "approved_intervals.csv")
    summary["source_locked_approval"] = True
    _saved_csv(output / "approved_frequency_tracking.csv", rows, FIELDS)
    plot_record(output / "approved_frequency_tracking.png", rows, summary,
                states, mcu, taxel, encoding, mapping, scope_start, scope_end)
    plot_distribution(output / "approved_frequency_distribution.png",
                      rows, summary)
    plot_pulse_activity(output / "approved_pulse_activity.png",
                        onsets, scope_start, scope_end)
    plot_ack_diagnostic(output / "approved_ack_diagnostic.png", rows)
    atomic_json(output / "approved_frequency_summary.json", summary)
    (output / "approved_frequency_report.md").write_text(
        report_markdown(summary), encoding="utf-8")
    return summary


def plot_session_summary(path, rows):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), layout="constrained")
    labels = [str(x["record_index"]) for x in rows]
    x = np.arange(len(rows))
    axes[0].bar(x, [r["onset_count"] for r in rows])
    axes[0].set(ylabel="Approved CH1 onsets",
                title="Operator-approved negative-onset pulse inventory")
    axes[1].bar(x, [
        r["inverse_period_hz"]["median"]
        if r["inverse_period_hz"]["median"] is not None else 0
        for r in rows])
    axes[1].set(ylabel="Median inverse period (Hz)",
                xlabel="Independent oscilloscope record")
    for ax in axes:
        ax.set_xticks(x, labels)
        ax.grid(axis="y", alpha=.2)
    axes[1].text(.98, .95, "Zero bar for record 5 means no approved intervals,\n"
                 "not a measured median of 0 Hz.",
                 ha="right", va="top", transform=axes[1].transAxes,
                 fontsize=9)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return str(path)


def session_report(summaries, total_record_count):
    total_onsets = sum(r["onset_count"] for r in summaries)
    total_intervals = sum(r["interval_count"] for r in summaries)
    lines = [
        "## Primary result: operator-approved S02 negative onsets", "",
        "This section is based only on source-locked reviewer-approved CH1 "
        "onsets. Preliminary positive-edge counts are retained solely "
        "as a separate legacy detector comparison.",
        "",
        f"**Approved records:** {len(summaries)} / {total_record_count}. "
        f"**Approved onsets:** {total_onsets}. "
        f"**Approved within-record intervals:** {total_intervals}.",
        "",
        "![Approved onsets and median active-cycle frequency]"
        "(approved_session_overview.png)", "",
        "| Record | Approved onsets | Approved intervals | "
        "Median period (ms) | Median inverse period (Hz) | "
        "Requested MCU range (Hz)* | Clock status | Full analysis |",
        "| ---: | ---: | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for r in summaries:
        i = r["record_index"]
        lines.append(
            f"| {i} | {r['onset_count']} | {r['interval_count']} | "
            f"{r['period_ms']['median'] if r['interval_count'] else 'n/a'} | "
            f"{r['inverse_period_hz']['median'] if r['interval_count'] else 'n/a'} | "
            f"{r.get('mcu_context', {}).get('requested_rate_hz', {}).get('min')}–"
            f"{r.get('mcu_context', {}).get('requested_rate_hz', {}).get('max')} | "
            f"{r['clock_status']} | "
            f"[Record {i} approved analysis]"
            f"(record_{i:05d}/approved_frequency_report.md) |"
        )
    lines += [
        "", "*Requested MCU ranges are restricted to provisionally mapped "
        "host samples. Ranges are unavailable without a clock map and are "
        "not independent physical measurements.",
        "", "### Why this does not yet prove command-by-command tracking", "",
        "All raw pulse-frequency calculations above are physical CH1 "
        "measurements from approved negative onsets. The overlaid MCU "
        "requests and BLE acknowledgments have only a provisional "
        "cross-clock alignment and represent host observations, not electrical "
        "apply timestamps. Diagnostic comparisons are explicitly labeled "
        "and do not constitute calibrated tracking accuracy or latency.",
        "",
        "Record 5's reviewer-confirmed empty inventory establishes no observed "
        "CH1 onsets above the detector/scope sensitivity; it does not "
        "independently certify zero current.",
        "",
    ]
    return "\n".join(lines)
