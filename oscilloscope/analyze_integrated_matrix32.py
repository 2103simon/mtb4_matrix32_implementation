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


def summarize(root, meta, *, threshold_v=2, deadtime_ms=1, taxel=None,
              zoom_start=None, zoom_sec=0.5, show=False):
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
    atomic_json(root / "analysis" / "integrated_summary.json", report)
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
    p.add_argument("--show", action="store_true",
                   help="Print saved PNG filenames; no GUI is opened")
    args = p.parse_args(argv)
    if not (math.isfinite(args.threshold_v) and args.threshold_v > 0
            and math.isfinite(args.deadtime_ms) and args.deadtime_ms >= 0
            and math.isfinite(args.zoom_sec) and args.zoom_sec > 0
            and (args.zoom_start_s is None or math.isfinite(args.zoom_start_s))):
        p.error("Threshold, deadtime and zoom range must be finite and valid")
    root, meta = choose_run(args.output_root, args.run_dir)
    return summarize(root, meta, threshold_v=args.threshold_v,
                     deadtime_ms=args.deadtime_ms, taxel=args.taxel,
                     zoom_start=args.zoom_start_s, zoom_sec=args.zoom_sec,
                     show=args.show)


if __name__ == "__main__":
    main()
