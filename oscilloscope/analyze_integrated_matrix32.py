"""Analyze integrated Matrix32/MCU/Nano/scope recordings using approved negative onsets.

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


def summarize(root, meta, *, taxel=None, show=False):
    """Primary integrated analysis: approved S02 negative onsets exclusively."""
    from oscilloscope.analyze_integrated_session import analyze_session
    return analyze_session(root, meta, taxel=taxel, show=show)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir", type=Path, default=None,
                   help="Explicit integrated session folder; default latest COMPLETE")
    p.add_argument("--output-root", type=Path, default=ROOT / "data/recordings")
    p.add_argument("--taxel", type=int, default=None, choices=range(12))
    p.add_argument("--show", action="store_true",
                   help="Print saved PNG filenames; no GUI is opened")
    args = p.parse_args(argv)
    root, meta = choose_run(args.output_root, args.run_dir)
    return summarize(root, meta, taxel=args.taxel, show=args.show)

if __name__ == "__main__":
    main()
