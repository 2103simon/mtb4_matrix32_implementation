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


def tentative_host_map(root):
    file = root / "epoch_clock_analysis.json"
    if not file.exists():
        return None
    try:
        report = json.loads(file.read_text(encoding="utf-8"))
        row = report["records"][0]
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
               taxel, zoom_start, zoom_sec):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output = root / "analysis"
    output.mkdir(exist_ok=True)
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
    if t_mcu is not None:
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
        local = mcu["taxel_corrected_timestamps"] / 1000
        axes[2].plot(local, mcu["taxel_data"][:, taxel], linewidth=0.8)
        axes[3].plot(local, hz[:, taxel], linewidth=0.8)
        axes[2].text(0.01, 0.95, "MCU time only; NOT aligned to scope",
                     transform=axes[2].transAxes, va="top")
        axes[3].text(0.01, 0.95, "MCU time only; NOT aligned to scope",
                     transform=axes[3].transAxes, va="top")
    axes[2].set_ylabel(f"Taxel {taxel} (0–255)")
    axes[3].set_ylabel(f"MCU {enc} request (Hz)")
    axes[3].set_xlabel("Scope waveform time (s)" if mapping else
                       "Separate local times (NOT aligned)")
    if mapping:
        axes[3].legend(loc="upper right", fontsize=8)
    for ax in axes:
        ax.grid(alpha=0.2)
    title = "Integrated Matrix32 / MCU: " + root.name
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


def plot_frequency_comparison(root, windows, compared, mapping):
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
    output = root / "analysis" / "matrix32_frequency_comparison.png"
    fig.savefig(output, dpi=150)
    plt.close(fig)
    return str(output)


def write_markdown_report(root, report):
    """Publish an entirely offline human-readable report beside JSON."""
    out = root / "analysis" / "integrated_report.md"
    scope, mcu, pulses = report["scope"], report["mcu"], report["scope"]["pulse_detection"]
    frequency = report["frequency_comparison"]
    def fmt(value, suffix="", precision=2):
        return "not available" if value is None else f"{value:.{precision}f}{suffix}"
    lines = [
        f"# Integrated Matrix32 recording — {report['run_id']}", "",
        "> **Exploratory, bench-only analysis.** BLE acknowledgments confirm a protocol response, "
        "not electrical output timing. Cross-clock alignment is provisional; USB asymmetry "
        "is not calibrated. Do not use reported frequency agreement to claim precise latency.",
        "", "## Overview", "",
        "| Measurement | Value |", "| --- | ---: |",
        f"| Acquisition status | {report['recording_status']} |",
        f"| CH1 waveform duration | {fmt(scope['duration_s'], ' s', 3)} |",
        f"| CH1 samples | {scope['samples']:,} |",
        f"| CH1 voltage range | {fmt(scope['ch1_min_v'], ' V')} to {fmt(scope['ch1_max_v'], ' V')} |",
        f"| Gray frames (CH2) | {scope['gray_frames']} |",
        f"| Candidate positive CH1 events | {pulses['positive_rising_events']:,} |",
        f"| Median local CH1 rate | {fmt(pulses['median_instantaneous_rate_hz'], ' Hz')} |",
        f"| Median CH1 interval | {fmt(pulses['median_interevent_ms'], ' ms', 3)} |",
        f"| MCU rate snapshots | {mcu['samples']:,} |",
        f"| MCU arrival cadence | {fmt(mcu['observed_update_rate_hz'], ' Hz')} |",
        f"| Selected taxel / encoding | {mcu['taxel']} / {mcu['encoding']} |",
        f"| MCU requested rate | {fmt(mcu['requested_rate_min_hz'], ' Hz')} to "
        f"{fmt(mcu['requested_rate_max_hz'], ' Hz')} |",
        f"| Matrix32 BLE trace events | {report['matrix32_commands']['events']:,} |",
        "", "## Physical frequency versus command acknowledgments", "",
        f"Sliding count window **{fmt(frequency['window_s'], ' s')}** stepped by "
        f"**{fmt(frequency['step_s'], ' s')}**. An event is a rising CH1 "
        f"positive-phase threshold crossing above {fmt(pulses['positive_threshold_v'], ' V')}. "
        "The measured rate is events/window duration, quantized at 1/window seconds. "
        "This is not a guaranteed biphasic-cycle detector.",
        "",
        "Expected rates are sourced from the **ACK-verified initial configuration and START**, "
        "followed by successful `sf_ack` updates. Requested-only `sf_requested` events "
        "are not counted as accepted changes. `sc_ack` with current=0 is treated as muted. "
        "A settled window must be clear of ACK transitions by 0.5 s at each end.",
        "",
        f"| Settled windows comparable | {frequency['comparable_windows']} |",
        f"| Compatible (provisional) | {frequency['compatible_windows']} |",
        f"| Discrepant (provisional) | {frequency['discrepant_windows']} |",
        f"| Not comparable | {frequency['not_comparable_windows']} |",
        f"| Tolerance | max({fmt(frequency['tolerance_hz'], ' Hz')}, "
        f"{fmt(100*frequency['tolerance_fraction'], '%')}, count quantization) |",
        "", "![Measured and acknowledged frequency](matrix32_frequency_comparison.png)", "",
        "### Frequencies tested (grouped by acknowledged setting)", "",
        "| ACK-indicated SF (Hz) | Comparable windows | Compatible | Discrepant | Median measured CH1 (Hz) | Assessment |",
        "| ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    if not frequency["by_ack_frequency"]:
        lines.append("| — | 0 | 0 | 0 | — | No ACK-indicated frequencies |")
    for grouped in frequency["by_ack_frequency"]:
        lines.append(
            f"| {grouped['acknowledged_frequency_hz']} | "
            f"{grouped['settled_windows']} | {grouped['compatible_windows']} | "
            f"{grouped['discrepant_windows']} | "
            f"{fmt(grouped['median_measured_rate_hz'], precision=1)} | "
            f"{grouped['assessment']} |"
        )
    lines.extend([
        "", "### Time-resolved frequency check", "",
        "| Scope time (s) | CH1 measured (Hz) | ACK-indicated SF (Hz) | Difference (Hz) | Assessment |",
        "| ---: | ---: | ---: | ---: | --- |",
    ]
    # One row per second (or an anomalous window) keeps the report readable.
    selection = [w for i, w in enumerate(frequency["windows"]) if
                 i % max(1, round(1/frequency["step_s"])) == 0
                 or w["assessment"] == "discrepant_provisional"]
    for w in selection:
        lines.append(
            f"| {w['center_scope_s']:.2f} | {w['physical_rate_hz']:.1f} | "
            f"{fmt(w['ack_expected_hz'], precision=1)} | "
            f"{fmt(w['difference_hz'], precision=1)} | "
            f"{w['assessment']} |"
        )
    if not selection:
        lines.append("| — | — | — | — | No measurable windows |")
    lines.extend([
        "", "Individual windows with a near-boundary ACK transition are intentionally "
        "reported **not comparable**. Agreement is **provisional**, even when "
        "the clock fit passed the original Gray RMS criterion.",
        "", "## Commands and recorded evidence", "",
        "| BLE trace event | Count |", "| --- | ---: |",
    ])
    for event, count in sorted(report["matrix32_commands"]["counts_by_event"].items()):
        lines.append(f"| `{event}` | {count} |")
    clock = report["clock_mapping"]
    lines += [
        "", "## Timing / limitations", "",
        f"- Clock qualification: **{clock['status'] if clock else 'not available'}**.",
        f"- Scope-to-Nano RMS: **{fmt(clock.get('scope_rms_us') if clock else None, ' µs')}**.",
        f"- Nano-to-PC fit RMS: **{fmt(clock.get('nano_pc_rms_us') if clock else None, ' µs')}**.",
        "- The Nano USB one-way offset remains uncalibrated; the expected-vs-measured "
        "time overlay is therefore exploratory, not an absolute timing guarantee.",
        "- The 44.5 Hz MCU update cadence and Matrix32 physical pulse frequency "
        "are different quantities.",
        "", "## Figures", "",
        "### CH1 waveform, physical event density, MCU sensor and requested rate",
        "![Integrated overview](integrated_overview.png)", "",
        "### Detailed biphasic output voltage",
        "![CH1 pulse zoom](matrix32_pulse_zoom.png)", "",
        "## Files", "",
        "- Machine-readable results: [integrated_summary.json](integrated_summary.json)",
        f"- Waveform: [CH1](../{scope['ch1_path']}), [CH2](../{scope['ch2_path']})",
        "", "## Warnings", "",
    ]
    lines.extend(f"- {warning}" for warning in report["warnings"])
    lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    return str(out)


def summarize(root, meta, *, threshold_v=2, deadtime_ms=1, taxel=None,
              zoom_start=None, zoom_sec=0.5, show=False,
              frequency_window_s=1.0, frequency_step_s=0.25,
              frequency_tolerance_hz=3.0, frequency_tolerance_fraction=0.05):
    if len(meta.get("records", [])) != 1:
        raise ValueError("Expected exactly one integrated scope record")
    rec = meta["records"][0]
    if rec.get("status") != "captured":
        raise ValueError("Scope record incomplete")
    t, v = load_wave(root / rec["channels"]["CH1"]["path"])
    path2 = root / rec["channels"]["CH2"]["path"]
    # CH2 is decoded from metadata already; do not read 1M extra samples.
    starts = rising_pulses(t, v, threshold_v, deadtime_ms/1000)
    pulses = pulse_statistics(t, starts, threshold_v)
    candidates = list((root / "mcu_matrix32").glob("*_tactile_rates.csv"))
    if len(candidates) != 1:
        raise ValueError(f"Expected one MCU rates file; found {len(candidates)}")
    data_dir = root / "mcu_matrix32"
    rec_id = choose_recording(data_dir)
    mcu = load_recordings(data_dir, rec_id)
    if mcu["mode"] != "rates" or "taxel_pc_ns" not in mcu:
        raise ValueError("Integrated recording needs timestamped MCU rates")
    source = mcu.get("experiment_settings", {}).get("configuration", {}).get(
        "stimulation_source", {})
    default_taxel = source.get("taxel", 11)
    if taxel is None:
        taxel = int(default_taxel)
    if taxel not in range(12):
        raise ValueError("--taxel must be 0..11")
    enc = source.get("encoding", "sa_ii")
    selected_rates = mcu["sa_rate_hz"] if enc == "sa_ii" else mcu["ra_rate_hz"]
    pc_ns = mcu["taxel_pc_ns"]
    d = np.diff(pc_ns)
    d = d[d > 0]
    mapping = tentative_host_map(root)
    event_files = list(data_dir.glob("*_matrix32_commands.jsonl"))
    events = read_events(event_files[0]) if len(event_files) == 1 else []
    counts = Counter(row.get("event", "?") for row in events)
    windows = sliding_physical_rates(
        starts, float(t[0]), float(t[-1]), frequency_window_s,
        frequency_step_s,
    )
    ack_states = acknowledged_frequency_states(events)
    comparisons = compare_physical_with_acks(
        windows, ack_states, mapping, float(t[0]), float(t[-1]),
        frequency_tolerance_hz, frequency_tolerance_fraction,
    )
    assessments = Counter(w["assessment"] for w in comparisons)
    report = {
        "schema_version": 1,
        "run_id": meta["run_id"],
        "run_dir": str(root),
        "recording_status": meta["status"],
        "scope": {
            "ch1_path": str(rec["channels"]["CH1"]["path"]),
            "ch2_path": str(path2.relative_to(root)),
            "samples": int(len(t)), "duration_s": float(t[-1]-t[0]),
            "ch1_min_v": float(v.min()), "ch1_max_v": float(v.max()),
            "ch1_clipped_raw_sample_count": rec["channels"]["CH1"].get(
                "transfer", {}).get("binary_transfer", {}).get(
                "storage_rail_code_count"),
            "gray_frames": rec["decoded_gray"].get("decoded_frame_count"),
            "pulse_detection": pulses,
        },
        "mcu": {
            "recording_id": rec_id,
            "samples": int(len(pc_ns)),
            "taxel": taxel, "encoding": enc,
            "requested_rate_mean_hz": float(np.mean(selected_rates[:, taxel])),
            "requested_rate_min_hz": float(np.min(selected_rates[:, taxel])),
            "requested_rate_max_hz": float(np.max(selected_rates[:, taxel])),
            "positive_update_interval_median_ms": float(np.median(d)/1e6)
                if len(d) else None,
            "observed_update_rate_hz": float(1e9/np.median(d))
                if len(d) else None,
            "host_arrival_span_s": float((pc_ns[-1]-pc_ns[0])/1e9),
        },
        "matrix32_commands": {
            "trace_file_count": len(event_files),
            "events": len(events),
            "counts_by_event": dict(counts),
            "stop_write_reported": meta.get("matrix32_shutdown", {}).get(
                "stop_write_reported"),
            "physical_output_verified_off": False,
        },
        "clock_mapping": mapping,
        "frequency_comparison": {
            "window_s": frequency_window_s,
            "step_s": frequency_step_s,
            "tolerance_hz": frequency_tolerance_hz,
            "tolerance_fraction": frequency_tolerance_fraction,
            "acknowledged_states": ack_states,
            "windows": comparisons,
            "by_ack_frequency": summarize_frequency_by_ack(
                comparisons, ack_states
            ),
            "comparable_windows": (assessments["compatible_provisional"]
                                   + assessments["discrepant_provisional"]),
            "compatible_windows": assessments["compatible_provisional"],
            "discrepant_windows": assessments["discrepant_provisional"],
            "not_comparable_windows": assessments["not_comparable"],
            "cross_clock_agreement_verified": False,
        },
        "warnings": [
            "Positive CH1 threshold events are candidate physical pulse phases, "
            "not an independently verified complete biphasic cycle count.",
            "MCU 44.5 Hz update rate is distinct from commanded Matrix32 SF and "
            "actual measured CH1 physical pulse rate.",
            "Even a provisional scope-to-host time overlay has uncalibrated "
            "Nano USB offset asymmetry; do not claim absolute physical latency.",
            "BLE ACKs are not physical output-edge timestamps.",
            "Matrix32 shutdown STOP write does not independently confirm "
            "zero electrical output.",
        ],
    }
    report["plots"] = make_plots(
        root, t, v, starts, mcu, events, mapping, taxel, zoom_start, zoom_sec
    )
    report["plots"].append(
        plot_frequency_comparison(root, windows, comparisons, mapping)
    )
    report["markdown_report"] = str(root / "analysis" / "integrated_report.md")
    atomic_json(root / "analysis" / "integrated_summary.json", report)
    write_markdown_report(root, report)
    print(f"[INTEGRATED] {root.name}")
    print(f"[SCOPE] CH1 {len(t):,} samples, {t[-1]-t[0]:.3f}s, "
          f"range {v.min():.2f}..{v.max():.2f}V; "
          f"CH2 {rec['decoded_gray'].get('decoded_frame_count')} Gray frames")
    print(f"[PULSES] {pulses['positive_rising_events']} positive threshold events "
          f"(threshold {threshold_v:g} V); "
          f"{pulses['mean_events_per_scope_second']:.1f}/s over entire record")
    if pulses["median_interevent_ms"] is not None:
        print(f"[PULSES] median interval {pulses['median_interevent_ms']:.3f} ms; "
              f"median local rate {pulses['median_instantaneous_rate_hz']:.1f} Hz; "
              f"first event at {pulses['first_positive_event_scope_s']:.3f}s")
    print(f"[MCU] {len(pc_ns)} rate samples; selected taxel {taxel} ({enc}); "
          f"requested {report['mcu']['requested_rate_min_hz']:.1f}.."
          f"{report['mcu']['requested_rate_max_hz']:.1f} Hz"
          + (f"; arrival cadence {report['mcu']['observed_update_rate_hz']:.2f} Hz"
             if len(d) else ""))
    print(f"[BLE] {len(events)} trace events: {dict(counts)}")
    freq = report["frequency_comparison"]
    print(f"[FREQUENCY] {freq['comparable_windows']} settled windows: "
          f"{freq['compatible_windows']} compatible, "
          f"{freq['discrepant_windows']} discrepant (PROVISIONAL); "
          f"{freq['not_comparable_windows']} not comparable")
    if mapping is None:
        print("[CLOCK] No valid saved scope->host fit; MCU/scope plotted on "
              "SEPARATE axes, not synchronized")
    else:
        print(f"[CLOCK] {mapping['status']}; scope RMS "
              f"{mapping['scope_rms_us']:.1f} us; Nano->PC RMS "
              f"{mapping['nano_pc_rms_us']:.1f} us; overlay PROVISIONAL")
    for path in report["plots"]:
        print(f"[PLOT] {path}")
    print(f"[SUMMARY] {root / 'analysis' / 'integrated_summary.json'}")
    print(f"[REPORT] {root / 'analysis' / 'integrated_report.md'}")
    if show:
        # PNGs have been saved; interactive viewers can open files separately.
        print("[INFO] Use your image viewer to open the saved PNG plots.")
    return report


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
