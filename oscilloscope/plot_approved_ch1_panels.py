"""Replot existing source-locked approved CH1 pulses as four 5-second panels.

Offline visualization only. Does not rerun detectors, modify approval,
smooth signal for analysis, or alter timestamps. Display uses raw per-bin
minimum and maximum voltage to preserve visible pulse extrema.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

import numpy as np

from oscilloscope.apply_pulse_review import load_approved_onsets
from plotting.envelopes import minmax_envelope


def load_waveform(path):
    times, volts = [], []
    with Path(path).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not {"time_s", "voltage_v"} <= set(reader.fieldnames or ()):
            raise ValueError("CH1 CSV must have time_s and voltage_v columns")
        for row in reader:
            times.append(float(row["time_s"]))
            volts.append(float(row["voltage_v"]))
    t = np.asarray(times)
    v = np.asarray(volts)
    if not len(t) or np.any(~np.isfinite(t)) or np.any(~np.isfinite(v)):
        raise ValueError("Invalid or empty CH1 waveform")
    if len(t) > 1 and np.any(np.diff(t) <= 0):
        raise ValueError("Scope timestamps are not strictly increasing")
    return t, v


def render_four_panels(t, v, onsets, path, *, record, seconds=5.):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("seconds must be finite and positive")
    start, end = float(t[0]), float(t[-1])
    n = max(1, math.ceil((end-start)/seconds))
    if n != 4:
        raise ValueError(
            f"Expected four {seconds:g}-second panels for 20-s capture; got {n}")
    fig, axes = plt.subplots(
        4, 1, figsize=(17, 15), sharey=True, layout="constrained")
    marker_times = np.array([float(x["time_s"]) for x in onsets], dtype=float)
    if len(marker_times) > 1 and np.any(np.diff(marker_times) <= 0):
        raise ValueError("Approved onset times not strictly increasing")
    if len(marker_times) and (marker_times[0] < start or marker_times[-1] > end):
        raise ValueError("Approved onset outside source CH1 capture")
    for i, ax in enumerate(axes):
        left = start+i*seconds
        right = left+seconds
        a = int(np.searchsorted(t, left, side="left"))
        b = int(np.searchsorted(t, min(right, end), side="right"))
        if b > a:
            tx, low, high = minmax_envelope(
                t[a:b].tolist(), v[a:b].tolist(), bins=1800)
            ax.fill_between(
                tx, low, high, alpha=.65,
                label="Original CH1 samples (min–max display)")
        keep = ((marker_times >= left)
                & (marker_times < right if i < 3 else marker_times <= end))
        chosen = marker_times[keep]
        if len(chosen):
            # Vertical lines remain visible even with dense pulse activity;
            # they mark approved electrical negative onset times.
            ax.vlines(chosen, ymin=float(np.min(v)), ymax=float(np.max(v)),
                      color="C3", alpha=.27, linewidth=.55,
                      label=f"Approved negative onsets: {len(chosen)}")
        ax.set(
            xlim=(left, right),
            ylabel="CH1 voltage (V)",
            title=f"Record {record} — {i*seconds:g}–{(i+1)*seconds:g} s "
                  "from record start")
        ax.grid(alpha=.15)
        ax.legend(loc="upper right", fontsize=8)
    axes[-1].set_xlabel("Scope-local time (s)")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=165)
    plt.close(fig)
    return path


def run(run_dir, *, record=None):
    root = Path(run_dir)
    metadata = json.loads((root/"metadata.json").read_text(encoding="utf-8"))
    paths = []
    for item in metadata["records"]:
        index = int(item["index"])
        if record is not None and index != record:
            continue
        ch1 = root/item["channels"]["CH1"]["path"]
        directory = root/"analysis"/f"record_{index:05d}"
        review = directory/"pulse_review"
        _, onsets = load_approved_onsets(review, ch1)
        t, v = load_waveform(ch1)
        path = render_four_panels(
            t, v, onsets,
            review/"figures"/"waveform_approved_four_5s_panels.png",
            record=index)
        # Add a figure link to the *existing* pulse-review report without
        # regenerating detectors, review decisions, or signed approval.
        report = review/"report.md"
        if not report.is_file():
            raise FileNotFoundError(f"Expected existing pulse-review report: {report}")
        section = (
            "## Approved pulse markers: four 5-second views\n\n"
            "The source-locked operator-approved negative CH1 onsets are "
            "overlaid on the unmodified CH1 waveform. Every panel covers "
            "a separate 5-second interval of this scope record; the "
            "display envelope retains source minimum and maximum values. "
            "No pulse detection, approval, or timing data is changed.\n\n"
            "![Approved CH1 negative-onset markers in four 5-second panels]"
            "(figures/waveform_approved_four_5s_panels.png)\n"
        )
        original = report.read_text(encoding="utf-8")
        heading = "## Approved pulse markers: four 5-second views"
        if heading in original:
            old_start = original.index(heading)
            next_heading = original.find("\n## ", old_start+len(heading))
            updated = (original[:old_start] + section
                       + (original[next_heading:] if next_heading >= 0 else ""))
        else:
            updated = original.rstrip()+"\n\n"+section
        if updated != original:
            report.write_text(updated, encoding="utf-8")
        print(f"[PLOT] {path}: {len(onsets)} approved onsets; linked in {report}")
        paths.append(str(path))
    if not paths:
        raise ValueError("No matching record")
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--record", type=int, default=None)
    args = parser.parse_args(argv)
    return run(args.run_dir, record=args.record)


if __name__ == "__main__":
    main()
