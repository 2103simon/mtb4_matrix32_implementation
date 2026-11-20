"""Print and plot the latest integrated Matrix32/MCU/Nano/scope recording.

Offline only: no serial, BLE, scope, or stimulation commands.
Usage:
  python oscilloscope/analyze_integrated_matrix32.py
  python oscilloscope/analyze_integrated_matrix32.py --run-dir data/recordings/<run>
  python oscilloscope/analyze_integrated_matrix32.py --taxel 11 --zoom-start-s 5 --zoom-sec 0.5

Plot time: scope time (seconds after first saved scope sample).
MCU/BLE arrival times map approximately through the *unverified*
scope->Nano->PC affine clock estimates. Such overlays are explicitly
provisional, NEVER physical latency ground truth. Physical pulses are
detected directly from the CH1 voltage recording, independently of fit.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analyze_mcu_recording import choose_recording, load_recordings
from oscilloscope.record_scope import atomic_json
from oscilloscope.matrix32_dynamic_frequency import (
    analyze_dynamic_cycles, plot_dynamic_tracking,
)


def choose_run(output_root, run_dir=None):
    if run_dir is not None:
        root = Path(run_dir).expanduser().resolve()
    else:
        candidates = sorted(
            output_root.glob("*_integrated_matrix32_scope"),
            key=lambda p: p.name,
        )
        if not candidates:
            raise FileNotFoundError(
                f"No integrated sessions under {output_root}; pass --run-dir"
            )
        # "latest" means newest complete integrated acquisition, not latest
        # arbitrary folder or failed/incomplete run.
        complete = []
        for item in candidates:
            try:
                meta = json.loads((item / "metadata.json").read_text())
            except (FileNotFoundError, ValueError):
                continue
            if (meta.get("status") == "complete"
                    and meta.get("plan", {}).get("integrated_experiment")
                    == "MATRIX32_MCU_NANO_SCOPE_V1"):
                complete.append(item)
        if not complete:
            raise FileNotFoundError("No complete integrated Matrix32 recordings")
        root = complete[-1].resolve()
    meta = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    if meta.get("plan", {}).get("integrated_experiment") != "MATRIX32_MCU_NANO_SCOPE_V1":
        raise ValueError("Selected directory is not an integrated Matrix32 recording")
    return root, meta


def load_wave(path):
    """One-million-row CSV; retain arrays for event detection and zoom plots."""
    times, voltage = [], []
    with Path(path).open(newline="", encoding="utf-8") as file:
        for row in csv.DictReader(file):
            times.append(float(row["time_s"]))
            voltage.append(float(row["voltage_v"]))
    if len(times) < 3:
        raise ValueError(f"Missing waveform samples: {path}")
    t = np.asarray(times, dtype=float)
    v = np.asarray(voltage, dtype=float)
    if not (np.all(np.isfinite(t)) and np.all(np.isfinite(v))
            and np.all(np.diff(t) > 0)):
        raise ValueError(f"Invalid waveform values or time ordering: {path}")
    return t, v


def rising_pulses(t, v, threshold_v=2.0, min_interval_s=0.001):
    """Rising positive excursions with hysteresis and minimum deadtime.

    Count candidate *positive phases*, not biphasic phases or MCU updates.
    Threshold and deadtime must be validated against the actual CH1 waveform.
    """
    if not (math.isfinite(threshold_v) and threshold_v > 0
            and math.isfinite(min_interval_s) and min_interval_s >= 0):
        raise ValueError("Positive finite pulse threshold and deadtime required")
    low = threshold_v * 0.5
    armed = True
    last = float("-inf")
    events = []
    for x, y in zip(t, v):
        if armed and y >= threshold_v and x - last >= min_interval_s:
            events.append(float(x))
            last = float(x)
            armed = False
        elif y < low:
            armed = True
    return np.asarray(events, dtype=float)


def pulse_statistics(times, starts, threshold_v):
    spacing = np.diff(starts)
    # Distinguish total-record pulse count rate from instantaneous cadence.
    span_s = float(times[-1] - times[0])
    return {
        "positive_threshold_v": threshold_v,
        "positive_rising_events": int(len(starts)),
        "capture_span_s": span_s,
        "mean_events_per_scope_second": float(len(starts) / span_s),
        "first_positive_event_scope_s": float(starts[0]) if len(starts) else None,
        "last_positive_event_scope_s": float(starts[-1]) if len(starts) else None,
        "median_interevent_ms": float(np.median(spacing)*1000) if len(spacing) else None,
        "p10_interevent_ms": float(np.percentile(spacing, 10)*1000) if len(spacing) else None,
        "p90_interevent_ms": float(np.percentile(spacing, 90)*1000) if len(spacing) else None,
        "median_instantaneous_rate_hz": float(1/np.median(spacing)) if len(spacing)
        and np.median(spacing) > 0 else None,
        "bin_counts_per_second": [
            int(np.sum((starts >= start) & (starts < min(start + 1, times[-1]+1e-9))))
            for start in np.arange(times[0], times[-1], 1.0)
        ],
        "candidate_definition": "positive CH1 threshold crossings with hysteresis and deadtime; no proof of one event per physical stimulus cycle",
    }


def read_events(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def tentative_host_map(root, record_index=1):
    file = root / "epoch_clock_analysis.json"
    if not file.exists():
        return None
    try:
        report = json.loads(file.read_text(encoding="utf-8"))
        matches = [item for item in report["records"]
                   if int(item["record_index"]) == int(record_index)]
        if len(matches) != 1:
            return None
        row = matches[0]
        fit = row.get("fit")
        start = row.get("first_sample_host_monotonic_ns_est")
        end = row.get("last_sample_host_monotonic_ns_est")
        if fit and start is not None and end is not None and end > start:
            return {
                "first_sample_pc_monotonic_ns_est": int(start),
                "last_sample_pc_monotonic_ns_est": int(end),
                "status": row.get("status", "unqualified"),
                "scope_rms_us": row.get("scope_edge_fit_rms_us"),
                "nano_pc_rms_us": fit.get("rms_residual_us"),
                "drift_ppm": fit.get("drift_ppm_nano_to_host"),
                "host_clock_calibrated": False,
                "absolute_scope_host_alignment_verified": False,
            }
    except (KeyError, IndexError, ValueError, TypeError):
        return None
    return None


def host_to_scope(pc_ns, mapping, scope_start_s, scope_end_s):
    """Approximate PC timestamp -> waveform x coordinate, not ground truth."""
    if mapping is None:
        raise ValueError("Clock mapping missing")
    h0 = mapping["first_sample_pc_monotonic_ns_est"]
    h1 = mapping["last_sample_pc_monotonic_ns_est"]
    # Subtraction uses Python ints; avoid loss of ns precision in float64
    # by subtracting before conversion.
    fraction = (np.asarray(pc_ns, dtype=np.int64) - h0) / (h1 - h0)
    return scope_start_s + fraction * (scope_end_s - scope_start_s)


def envelope(t, v, max_bins=4000):
    """Min/max envelope retains narrow biphasic pulses when zoomed out."""
    block = max(1, int(math.ceil(len(v) / max_bins)))
    n = len(v) // block
    if not n:
        return t, v, v
    xx = t[:n*block].reshape(n, block)
    yy = v[:n*block].reshape(n, block)
    return xx[:, 0], yy.min(axis=1), yy.max(axis=1)


def make_plots(root, t, volts, starts, mcu, events, mapping,
               taxel, zoom_start, zoom_sec, record_index=1):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output = root / "analysis" / f"record_{record_index:05d}"
    output.mkdir(parents=True, exist_ok=True)
    selected = mcu.get("experiment_settings", {}).get("configuration", {}).get(
        "stimulation_source", {})
    enc = selected.get("encoding", "sa_ii")
    hz = mcu["sa_rate_hz"] if enc == "sa_ii" else mcu["ra_rate_hz"]
    t_mcu = host_to_scope(mcu["taxel_pc_ns"], mapping, t[0], t[-1]) if mapping else None
    fig, axes = plt.subplots(4, 1, figsize=(15, 12), sharex=True,
                              layout="constrained")
    x, lo, hi = envelope(t, volts)
    axes[0].fill_between(x, lo, hi, alpha=0.6, linewidth=0)
    axes[0].set_ylabel("Matrix32 CH1 (V)\nmin/max envelope")
    axes[1].hist(starts, bins=np.arange(t[0], t[-1]+1.01, 1),
                 align="left", rwidth=0.95)
    axes[1].set_ylabel("CH1 positive events\nper 1 s bin")
    if t_mcu is not None and len(t_mcu):
        axes[2].plot(t_mcu, mcu["taxel_data"][:, taxel], linewidth=0.8)
        axes[3].plot(t_mcu, hz[:, taxel], linewidth=0.8,
                     label=f"MCU {enc} requested rate")
        for name, color in (("sf_requested", "tab:orange"),
                            ("sf_ack", "tab:green"),
                            ("start_write_completed", "tab:red")):
            selected_events = [e["pc_monotonic_ns"] for e in events
                               if e.get("event") == name
                               and "pc_monotonic_ns" in e]
            if selected_events:
                xs = host_to_scope(selected_events, mapping, t[0], t[-1])
                axes[3].scatter(xs, np.interp(xs, t_mcu, hz[:, taxel]),
                                marker="|", s=35, label=name, color=color)
    else:
        for ax in axes[2:]:
            ax.text(0.02, 0.85,
                    "MCU overlap unavailable — no usable per-record clock map "
                    "or no overlapping samples. See independent full MCU plot.",
                    transform=ax.transAxes, va="top", wrap=True)
    axes[2].set_ylabel(f"Taxel {taxel} (0–255)")
    axes[3].set_ylabel(f"MCU {enc} request (Hz)")
    axes[3].set_xlabel("Time within THIS scope record (s)")
    if mapping:
        axes[3].legend(loc="upper right", fontsize=8)
    for ax in axes:
        ax.grid(alpha=0.2)
    for ax in axes:
        ax.set_xlim(float(t[0]), float(t[-1]))
    title = f"Integrated Matrix32 / MCU: {root.name}, record {record_index}"
    title += ("\nPROVISIONAL cross-clock overlay (USB offset uncalibrated)"
              if mapping else "\nNO scope-to-host map: panels 3–4 use local MCU time")
    fig.suptitle(title)
    full = output / "integrated_overview.png"
    fig.savefig(full, dpi=130)
    plt.close(fig)

    if zoom_start is None:
        # Prefer a dense pulse interval, but keep the zoom inside waveform.
        center = starts[len(starts)//2] if len(starts) else (t[0] + t[-1])/2
        zoom_start = max(float(t[0]), float(center - zoom_sec/2))
    zoom_start = max(float(t[0]), float(zoom_start))
    zoom_end = min(float(t[-1]), zoom_start + zoom_sec)
    mask = (t >= zoom_start) & (t <= zoom_end)
    if not mask.any():
        raise ValueError("Zoom contains no waveform samples")
    fig, ax = plt.subplots(figsize=(15, 5), layout="constrained")
    ax.plot(t[mask], volts[mask], linewidth=0.8)
    ax.set_xlim(zoom_start, zoom_end)
    ax.set_xlabel("Oscilloscope waveform time (s)")
    ax.set_ylabel("Matrix32 CH1 dummy-load voltage (V)")
    ax.set_title(f"Physical waveform detail: {root.name}")
    ax.grid(alpha=0.25)
    zoom = output / "matrix32_pulse_zoom.png"
    fig.savefig(zoom, dpi=150)
    plt.close(fig)
    return [str(full), str(zoom)]



def sliding_physical_rates(starts, t_start, t_end, window_s=1.0, step_s=0.25):
    """Non-overlapping count uncertainty is ~1/window Hz; return centered windows.

    Windows are never permitted to extend past the saved waveform bounds.
    """
    if not (math.isfinite(window_s) and math.isfinite(step_s)
            and window_s > 0 and step_s > 0 and t_end > t_start):
        raise ValueError("Invalid sliding-window parameters")
    if t_end - t_start < window_s:
        return []
    starts = np.sort(np.asarray(starts, dtype=float))
    first = float(t_start)
    centers = np.arange(first + window_s/2, t_end - window_s/2 + 1e-10, step_s)
    left = np.searchsorted(starts, centers - window_s/2, side="left")
    right = np.searchsorted(starts, centers + window_s/2, side="left")
    return [{"center_scope_s": float(c), "window_s": float(window_s),
             "physical_positive_events": int(n),
             "physical_rate_hz": float(n / window_s)}
            for c, n in zip(centers, right - left)]


def acknowledged_frequency_states(events):
    """Extract protocol state from acknowledgments, NOT request timestamps.

    Initial frequency is accepted on 'configuration_ack' but only becomes
    running on 'start_write_completed'. SF=2 after SC=0 is output-muted,
    never evidence of 2 Hz pulses. Unknown after STOP/lost connection.
    """
    ordered = sorted((e for e in events if "pc_monotonic_ns" in e),
                     key=lambda e: int(e["pc_monotonic_ns"]))
    states = []
    initial = None
    running = False
    muted = False
    frequency = None
    for event in ordered:
        name = event.get("event")
        timestamp = int(event["pc_monotonic_ns"])
        if name == "configuration_ack" and event.get("verified") is True:
            initial = event.get("frequency_hz")
        elif name == "start_write_completed":
            frequency = event.get("frequency_hz", initial)
            running = frequency is not None
            muted = False
            if running:
                states.append({"pc_monotonic_ns": timestamp, "expected_hz": int(frequency),
                               "source": name, "output_active_assumed": True})
        elif name == "sf_ack" and event.get("status") == "OK":
            frequency = event.get("frequency_hz")
            if running and not muted and frequency is not None:
                states.append({"pc_monotonic_ns": timestamp,
                               "expected_hz": int(frequency), "source": name,
                               "output_active_assumed": True})
        elif name == "sc_ack" and event.get("status") == "OK":
            if event.get("current_command") == 0:
                muted = True
                if running:
                    states.append({"pc_monotonic_ns": timestamp, "expected_hz": 0,
                                   "source": "sc_ack_muted", "output_active_assumed": False})
            elif running:
                muted = False
                if frequency is not None:
                    states.append({"pc_monotonic_ns": timestamp,
                                   "expected_hz": int(frequency), "source": "sc_ack_restored",
                                   "output_active_assumed": True})
        elif name in ("stop_write_completed", "shutdown_stop_write_completed"):
            running = False
            states.append({"pc_monotonic_ns": timestamp, "expected_hz": 0,
                           "source": name, "output_active_assumed": False})
        elif name in ("ble_lost", "stop_failed", "stop_unavailable", "start_failed"):
            running = False
            states.append({"pc_monotonic_ns": timestamp, "expected_hz": None,
                           "source": name, "output_active_assumed": False})
    return states


def compare_physical_with_acks(windows, states, mapping, t_start, t_end,
                               tolerance_hz=3.0, tolerance_fraction=0.05):
    """Tentative comparison only for settled ACK states over whole windows.

    Reject windows crossing ANY ACK transition; a 0.5s guard on either
    side helps avoid interpreting BLE command propagation as instant.
    A valid clock mapping is required; unqualified fits stay provisional.
    """
    result = []
    state_ns = np.asarray([s["pc_monotonic_ns"] for s in states], dtype=np.int64)
    for window in windows:
        row = dict(window)
        row.update({"ack_expected_hz": None, "difference_hz": None,
                    "assessment": "not_comparable"})
        if mapping is None or not len(state_ns):
            row["reason"] = "no scope-to-host mapping or ACK states"
            result.append(row)
            continue
        # Invert the linear affine estimate using integer anchor differences.
        mid = window["center_scope_s"]
        half = window["window_s"] / 2
        h0 = mapping["first_sample_pc_monotonic_ns_est"]
        h1 = mapping["last_sample_pc_monotonic_ns_est"]
        def host_at(scope_s):
            return int(round(h0 + (scope_s - t_start) / (t_end - t_start)
                             * (h1-h0)))
        begin = host_at(mid-half)
        end = host_at(mid+half)
        center = host_at(mid)
        preceding = int(np.searchsorted(state_ns, center, side="right")) - 1
        if preceding < 0:
            row["reason"] = "no prior START/ACK evidence"
            result.append(row)
            continue
        state = states[preceding]
        if state["expected_hz"] is None:
            row["reason"] = "unknown output state"
            result.append(row)
            continue
        # Require the same state through the full window and 0.5s before
        # and after; don't classify a transition-containing window.
        guard_ns = 500_000_000
        prior_t = int(state_ns[preceding])
        next_t = (int(state_ns[preceding+1])
                  if preceding+1 < len(state_ns) else None)
        if prior_t > begin - guard_ns or (next_t is not None
                and next_t < end + guard_ns):
            row["reason"] = "ACK transition near window; unsettled"
            result.append(row)
            continue
        expected = int(state["expected_hz"])
        difference = row["physical_rate_hz"] - expected
        tolerance = max(float(tolerance_hz), expected*tolerance_fraction,
                        1.0/window["window_s"])
        row.update({"ack_expected_hz": expected,
                    "difference_hz": float(difference),
                    "tolerance_hz": float(tolerance),
                    "ack_source": state["source"],
                    "assessment": ("compatible_provisional" if abs(difference) <= tolerance
                                   else "discrepant_provisional")})
        result.append(row)
    return result


def summarize_frequency_by_ack(comparisons, states):
    """Aggregate settled comparisons by SF without hiding untested settings."""
    grouped = {}
    for state in states:
        hz = state.get("expected_hz")
        if hz is not None:
            grouped.setdefault(int(hz), [])
    for row in comparisons:
        hz = row.get("ack_expected_hz")
        if hz is not None:
            grouped.setdefault(int(hz), []).append(row)
    result = []
    for hz, rows in sorted(grouped.items()):
        compatible = sum(x["assessment"] == "compatible_provisional" for x in rows)
        discrepant = sum(x["assessment"] == "discrepant_provisional" for x in rows)
        observed = [x["physical_rate_hz"] for x in rows]
        result.append({
            "acknowledged_frequency_hz": hz,
            "settled_windows": len(rows),
            "compatible_windows": compatible,
            "discrepant_windows": discrepant,
            "median_measured_rate_hz": float(np.median(observed)) if observed else None,
            "mean_measured_rate_hz": float(np.mean(observed)) if observed else None,
            "assessment": (
                "not_testable" if not rows else
                "discrepancy_observed_provisional" if discrepant else
                "compatible_in_observed_windows_provisional"
            ),
        })
    return result


def plot_frequency_comparison(root, windows, compared, mapping, record_index=1):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(14, 4.5), layout="constrained")
    x = [w["center_scope_s"] for w in windows]
    measured = [w["physical_rate_hz"] for w in windows]
    ax.plot(x, measured, label="CH1 measured positive-phase rate (sliding count)")
    if mapping:
        comparable = [w for w in compared if w["ack_expected_hz"] is not None]
        if comparable:
            ax.step([w["center_scope_s"] for w in comparable],
                    [w["ack_expected_hz"] for w in comparable],
                    where="mid", label="BLE ACK-indicated SF (provisional placement)")
            mismatches = [w for w in comparable
                          if w["assessment"] == "discrepant_provisional"]
            if mismatches:
                ax.scatter([w["center_scope_s"] for w in mismatches],
                           [w["physical_rate_hz"] for w in mismatches],
                           marker="x", label="Discrepant settled window")
    ax.set_xlabel("Time in scope waveform (s)")
    ax.set_ylabel("Frequency (Hz)")
    ax.set_ylim(bottom=0)
    ax.set_title("Measured CH1 vs ACK-indicated SF — provisional time mapping")
    ax.grid(alpha=0.25)
    ax.legend()
    output = (root / "analysis" / f"record_{record_index:05d}"
              / "matrix32_frequency_comparison.png")
    fig.savefig(output, dpi=150)
    plt.close(fig)
    return str(output)


def write_markdown_report(root, report, *, record_index=1, per_record=False):
    """Short, question-oriented report; full detail stays in JSON."""
    out = (root / "analysis" / f"record_{record_index:05d}" / "report.md"
           if per_record else root / "analysis" / "integrated_report.md")
    image_prefix = "" if per_record else f"record_{record_index:05d}/"
    waveform_prefix = "../../" if per_record else "../"
    json_link = "summary.json" if per_record else "integrated_summary.json"
    scope = report["scope"]
    mcu = report["mcu"]
    pulses = scope["pulse_detection"]
    dynamic = report["dynamic_tracking"]
    old = report["frequency_comparison"]
    clock = report["clock_mapping"]
    counts = report["matrix32_commands"]["counts_by_event"]
    overlap = report.get("scope_to_host_overlap")
    if overlap is None:
        coverage_note = "MCU statistics describe the original recording input."
    elif overlap["status"] == "unknown_no_per_record_host_map":
        coverage_note = (
            "MCU/Matrix32 overlap with this scope acquisition is UNKNOWN "
            "because its per-record scope-to-host mapping is missing. "
            "An apparent zero selected MCU samples is NOT evidence of missing telemetry."
        )
    else:
        coverage_note = (
            f"This record includes {overlap['mcu_sample_count']} MCU samples "
            f"and {overlap['ble_event_count']} BLE events inside its "
            "provisionally mapped host window only. "
            "The continuous logs are analyzed separately in the session report."
        )
    def fmt(value, unit="", digits=1):
        return "n/a" if value is None else f"{value:.{digits}f}{unit}"
    lines = [
        f"# Matrix32 recording: {report['run_id']}", "",
        "> Bench-only exploratory recording. Scope↔Nano↔PC offset is "
        "**not calibrated**; ACK timings indicate software protocol events, "
        "not electrically verified setting application or latency.", "",
        "## What happened?", "",
        coverage_note, "",
        f"**{scope['samples']:,} CH1 samples** over "
        f"**{scope['duration_s']:.3f} s** and "
        f"**{pulses['positive_rising_events']:,} candidate physical positive phases**. "
        f"Median physical positive-phase interval: "
        f"**{fmt(pulses['median_interevent_ms'], ' ms', 3)}** "
        f"({fmt(pulses['median_instantaneous_rate_hz'], ' Hz')}). "
        f"Voltage range: {scope['ch1_min_v']:.1f} to {scope['ch1_max_v']:.1f} V.",
        "",
        f"**{mcu['samples']:,} MCU rate snapshots** "
        + ("within this record's estimated scope interval " if per_record
           else "recorded ")
        + f"({fmt(mcu['observed_update_rate_hz'], ' Hz')} median host-arrival "
        f"cadence) for taxel {mcu['taxel']} / {mcu['encoding']}. "
        f"MCU requested output range: "
        f"{fmt(mcu['requested_rate_min_hz'], ' Hz', 0)} to "
        f"{fmt(mcu['requested_rate_max_hz'], ' Hz', 0)}. "
        "Host-arrival cadence is not necessarily the MCU's true firmware update rate.",
        "",
        f"**{counts.get('sf_ack', 0)} acknowledged SF frequency changes**; "
        f"{counts.get('sf_requested', 0)} sent SF requests. "
        f"{counts.get('sc_ack', 0)} acknowledged current changes; "
        f"{counts.get('start_write_completed', 0)} START completion(s).",
        "",
        "## Did physical pulse frequency follow the Matrix32 commands?", "",
        "### Dynamic check: one pulse interval at a time", "",
        "The physical frequency is 1 / interval between successive detected "
        "positive pulse phases. It is compared with the most recent "
        "ACK-indicated SF only when no ACK transition occurred inside that "
        "interval and the previous ACK is at least 5 ms old.",
        "",
        "| Measure | Value |", "| --- | ---: |",
        f"| Complete measured pulse intervals | {dynamic['complete_intervals']} |",
        f"| Provisionally comparable | {dynamic['classified']} |",
        f"| Compatible (max 8 Hz or 15%) | {dynamic['compatible']} |",
        f"| Discrepant | {dynamic['discrepant']} |",
        f"| Not comparable (transition / mute / unknown) | {dynamic['not_comparable']} |",
        f"| Comparable fraction within tolerance | "
        f"{fmt(100*dynamic['compatibility_fraction'] if dynamic['compatibility_fraction'] is not None else None, '%')} |",
        f"| Median absolute frequency difference | "
        f"{fmt(dynamic['median_absolute_error_hz'], ' Hz')} |",
        "",
        "![Dynamic per-pulse tracking]({image_prefix}matrix32_dynamic_tracking.png)", "",
        "### Where did it match and where did it differ?", "",
        "These are the **most frequently observed ACK frequencies among "
        "comparable pulse intervals**, not a certification of steady-state "
        "frequency accuracy. Each line can include measurements at different "
        "times. The complete per-cycle results are in JSON.",
        "",
        "| ACK SF (Hz) | Compared intervals | Within tolerance | Outside tolerance | Median physical (Hz) |",
        "| ---: | ---: | ---: | ---: | ---: |",
    ]
    if not dynamic["per_frequency"]:
        lines.append("| — | 0 | 0 | 0 | — |")
    for item in dynamic["per_frequency"][:15]:
        lines.append(
            f"| {item['ack_hz']} | {item['cycle_intervals']} | "
            f"{item['compatible']} | {item['discrepant']} | "
            f"{item['median_measured_hz']:.1f} |"
        )
    lines += [
        "", "### What the one-second analysis can (and cannot) say", "",
        f"The original 1-second sliding-window test found "
        f"**{old['comparable_windows']} settled windows** of "
        f"**{len(old['windows'])}**. With changing SF approximately every "
        "few tens of milliseconds, those windows are mainly **ineligible**, "
        "not failed. The sliding rate curve is still useful to visualize "
        "overall frequency trends.",
        "",
        "![Sliding physical rate]({image_prefix}matrix32_frequency_comparison.png)", "",
        "## Sensor and waveform context", "",
        "![Scope waveform and MCU traces]({image_prefix}integrated_overview.png)", "",
        "### Un-decimated voltage detail", "",
        "![Pulse zoom]({image_prefix}matrix32_pulse_zoom.png)", "",
        "## Clock and data quality", "",
        f"- Scope-to-Nano Gray fit RMS: "
        f"**{fmt(clock.get('scope_rms_us') if clock else None, ' µs')}**.",
        f"- Nano-to-PC fit RMS: "
        f"**{fmt(clock.get('nano_pc_rms_us') if clock else None, ' µs')}**.",
        f"- Original clock qualification: "
        f"**{clock.get('status') if clock else 'unavailable'}**.",
        f"- First physical positive-phase event: "
        f"**{fmt(pulses['first_positive_event_scope_s'], ' s', 3)}**.",
        f"- Last physical positive-phase event: "
        f"**{fmt(pulses['last_positive_event_scope_s'], ' s', 3)}**.",
        "- The 20 µs scope sampling interval limits pulse-edge resolution.",
        "- BLE ACK ordering and provisional host timestamps cannot establish "
        "true electrical frequency-update latency or exact device apply time.",
        "- Dynamic comparisons assume a frequency update can affect the "
        "next measurable pulse cycle. The 5 ms guard is a diagnostic choice.",
        "- Pulse detection depends on CH1 voltage threshold, hysteresis, "
        "and minimum event spacing; inspect the waveform if counts look wrong.",
        "", "## Raw files and next checks", "",
        "- Full statistics, individual cycle classifications and raw ACK "
        f"states: [{json_link}]({json_link}).",
        f"- Physical [CH1]({waveform_prefix}{scope['ch1_path']}) and "
        f"Gray [CH2]({waveform_prefix}{scope['ch2_path']}) CSV files.",
        "- To validate *steady-state* SF accuracy, do a separate dummy-load "
        "run with several-second plateaus at fixed frequencies; live variable "
        "SF updates are too rapid for 1-second steady-state windows.",
        "", "### Warnings", "",
    ]
    lines.extend(f"- {message}" for message in report["warnings"])
    lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    return str(out)



def summarize(root, meta, *, threshold_v=2, deadtime_ms=1, taxel=None,
              zoom_start=None, zoom_sec=0.5, show=False,
              frequency_window_s=1.0, frequency_step_s=0.25,
              frequency_tolerance_hz=3.0, frequency_tolerance_fraction=0.05):
    """One session = N independent scope records + one continuous MCU log."""
    from oscilloscope.analyze_integrated_session import analyze_session
    return analyze_session(
        root, meta, threshold_v=threshold_v, deadtime_ms=deadtime_ms,
        taxel=taxel, zoom_start=zoom_start, zoom_sec=zoom_sec, show=show,
        frequency_window_s=frequency_window_s,
        frequency_step_s=frequency_step_s,
        frequency_tolerance_hz=frequency_tolerance_hz,
        frequency_tolerance_fraction=frequency_tolerance_fraction,
    )


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir", type=Path, default=None,
                   help="Explicit integrated session folder; default latest COMPLETE")
    p.add_argument("--output-root", type=Path, default=ROOT / "data/recordings")
    p.add_argument("--taxel", type=int, default=None, choices=range(12))
    p.add_argument("--threshold-v", type=float, default=2.0,
                   help="Positive CH1 crossing threshold for exploratory pulses")
    p.add_argument("--deadtime-ms", type=float, default=1.0)
    p.add_argument("--zoom-start-s", type=float, default=None)
    p.add_argument("--zoom-sec", type=float, default=0.5)
    p.add_argument("--frequency-window-s", type=float, default=1.0,
                   help="Sliding measured pulse-rate window (default 1 s)")
    p.add_argument("--frequency-step-s", type=float, default=0.25,
                   help="Sliding rate update step (default 0.25 s)")
    p.add_argument("--frequency-tolerance-hz", type=float, default=3.0)
    p.add_argument("--frequency-tolerance-fraction", type=float, default=0.05)
    p.add_argument("--show", action="store_true",
                   help="Print saved PNG filenames; no GUI is opened")
    args = p.parse_args(argv)
    if not (math.isfinite(args.threshold_v) and args.threshold_v > 0
            and math.isfinite(args.deadtime_ms) and args.deadtime_ms >= 0
            and math.isfinite(args.zoom_sec) and args.zoom_sec > 0
            and (args.zoom_start_s is None or math.isfinite(args.zoom_start_s))):
        p.error("Threshold, deadtime and zoom range must be finite and valid")
    if not (0 < args.frequency_window_s <= 10
            and 0 < args.frequency_step_s <= args.frequency_window_s
            and math.isfinite(args.frequency_window_s)
            and math.isfinite(args.frequency_step_s)
            and math.isfinite(args.frequency_tolerance_hz)
            and args.frequency_tolerance_hz > 0
            and math.isfinite(args.frequency_tolerance_fraction)
            and 0 <= args.frequency_tolerance_fraction <= 1):
        p.error("Invalid sliding-rate window or frequency tolerance")
    root, meta = choose_run(args.output_root, args.run_dir)
    return summarize(root, meta, threshold_v=args.threshold_v,
                     deadtime_ms=args.deadtime_ms, taxel=args.taxel,
                     zoom_start=args.zoom_start_s, zoom_sec=args.zoom_sec,
                     show=args.show, frequency_window_s=args.frequency_window_s,
                     frequency_step_s=args.frequency_step_s,
                     frequency_tolerance_hz=args.frequency_tolerance_hz,
                     frequency_tolerance_fraction=args.frequency_tolerance_fraction)


if __name__ == "__main__":
    main()
