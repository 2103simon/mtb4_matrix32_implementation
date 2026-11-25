"""Rebuild the legacy integrated_overview.png referenced by pulse review.

Offline only; requires original CH1, signed approved onsets, MCU samples,
Matrix32 command trace and saved scope-to-host clock estimates. The image is
new derived output. No source measurements, approvals, or reports are edited.

Usage (from repository root):
    python -m oscilloscope.plot_integrated_overview --run-dir data/recordings/RUN
"""
from __future__ import annotations
from experiment_analysis.paths import analysis_root

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

import numpy as np

SOURCE_TABLES = (
    "accepted_onsets.csv", "rejected_onsets.csv", "suppressed_onsets.csv",
    "periods_unverified.csv", "threshold_sensitive_onsets.csv",
)
SIGNED_FILES = {
    "review_manifest.json": "review_manifest_sha256",
    "review_decisions.csv": "review_decisions_sha256",
    "manual_additions.csv": "manual_additions_sha256",
    "approved_onsets.csv": "approved_onsets_sha256",
    "approved_intervals.csv": "approved_intervals_sha256",
}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verified_onsets(review_dir):
    """Verify operator signoff and all source-locked tables before plotting."""
    review_dir = Path(review_dir)
    approval = json.loads((review_dir / "review_approval.json").read_text())
    manifest = json.loads((review_dir / "review_manifest.json").read_text())
    if approval.get("human_signed_off") is not True:
        raise ValueError("Source-locked operator approval is absent")
    if manifest.get("source_ch1_sha256") != approval.get("source_ch1_sha256"):
        raise ValueError("Approval and review manifest disagree on CH1 source")
    expected = approval.get("source_tables_sha256", {})
    if manifest.get("source_tables_sha256") != expected:
        raise ValueError("Approval and manifest disagree on detector tables")
    for name in SOURCE_TABLES:
        if sha256(review_dir / name) != expected.get(name):
            raise ValueError(f"Source-locked detector table changed: {name}")
    for name, field in SIGNED_FILES.items():
        if sha256(review_dir / name) != approval.get(field):
            raise ValueError(f"Signed review evidence changed: {name}")
    with (review_dir / "approved_onsets.csv").open(newline="") as stream:
        onsets = list(csv.DictReader(stream))
    if len(onsets) != int(approval["included_onsets"]):
        raise ValueError("Approved onset count differs from signed approval")
    if any(row.get("review_decision") != "INCLUDE" for row in onsets):
        raise ValueError("Approved onset timeline contains non-INCLUDE decisions")
    times = np.asarray([float(row["time_s"]) for row in onsets], dtype=float)
    if (not np.all(np.isfinite(times)) or
            (len(times) > 1 and not np.all(np.diff(times) > 0))):
        raise ValueError("Approved onset times are not strictly increasing")
    return approval, times


def waveform_envelope(path, waveform_meta, expected_sha256, bins=2400):
    """Stream every original CH1 sample; preserve both extrema in each bin.

    At 10M samples, memory usage is O(bins), not O(waveform samples). The
    byte-level SHA-256 is computed during this one pass to source-lock CH1.
    """
    path = Path(path)
    n = int(waveform_meta["samples"])
    if n < 3 or bins < 10:
        raise ValueError("Waveform needs >=3 samples and >=10 display bins")
    bins = min(bins, n)
    lo = np.full(bins, np.inf)
    hi = np.full(bins, -np.inf)
    first = last = None
    hasher = hashlib.sha256()
    with path.open("rb") as file:
        header = file.readline()
        hasher.update(header)
        if header.strip().split(b",")[:2] != [b"time_s", b"voltage_v"]:
            raise ValueError("Unexpected CH1 CSV column layout")
        samples = 0
        for line in file:
            hasher.update(line)
            if not line.strip():
                continue
            if samples >= n:
                raise ValueError("CH1 contains more samples than metadata")
            columns = line.split(b",", 2)
            if len(columns) < 2:
                raise ValueError(f"Malformed CH1 row {samples + 1}")
            v = float(columns[1])
            if not math.isfinite(v):
                raise ValueError("Nonfinite CH1 sample")
            if first is None:
                first = float(columns[0])
            last = float(columns[0])
            j = samples * bins // n
            if v < lo[j]:
                lo[j] = v
            if v > hi[j]:
                hi[j] = v
            samples += 1
    if samples != n or not np.all(np.isfinite(lo)) or not np.all(np.isfinite(hi)):
        raise ValueError(f"Invalid CH1 envelope: {samples} samples, expected {n}")
    if hasher.hexdigest() != expected_sha256:
        raise ValueError("Original CH1 waveform differs from signed approval")
    expected_first = float(waveform_meta["first_time_s"])
    expected_last = float(waveform_meta["last_time_s"])
    dt = float(waveform_meta["sample_interval_s"])
    if (abs(first - expected_first) > dt or
            abs(last - expected_last) > dt or last <= first):
        raise ValueError("Original CH1 time bounds disagree with metadata")
    x = first + (np.arange(bins, dtype=float) + .5) * (last-first) / bins
    return x, lo, hi, first, last


def host_to_scope(ns, mapping, first_s, last_s):
    """Provisional host-to-scope display mapping, never physical ground truth."""
    h0, h1 = mapping
    if h1 <= h0:
        raise ValueError("Nonpositive mapped scope duration")
    arr = np.asarray(ns, dtype=np.int64)
    # Subtract integer epochs BEFORE float conversion for nanosecond fidelity.
    return first_s + ((arr - h0).astype(float) / float(h1-h0)) * (last_s-first_s)


def mcu_and_command_data(root, taxel=None):
    """Load the native recorded taxel/SA-II/RA-I series and BLE command log."""
    mcu_dir = root / "mcu_matrix32"
    rates = sorted(mcu_dir.glob("*_tactile_rates.csv"))
    commands = sorted(mcu_dir.glob("*_matrix32_commands.jsonl"))
    if len(rates) != 1 or len(commands) != 1:
        raise ValueError("Expected one MCU rates CSV and one BLE command log")
    rid = rates[0].name.removesuffix("_tactile_rates.csv")
    settings = mcu_dir / f"{rid}_settings.json"
    config = (json.loads(settings.read_text()).get("configuration", {})
              if settings.exists() else {})
    source = config.get("stimulation_source", {})
    selected = int(source.get("taxel", 11) if taxel is None else taxel)
    encoding = source.get("encoding", "sa_ii")
    if selected not in range(12) or encoding not in ("sa_ii", "ra_i"):
        raise ValueError("Unsupported selected taxel or stimulation encoding")
    pc, pressure, requested = [], [], []
    with rates[0].open(newline="") as file:
        for row in csv.DictReader(file):
            pc.append(int(row["pc_arrival_monotonic_ns"]))
            pressure.append(float(row[f"taxel_{selected}"]))
            requested.append(float(row[f"{'sa' if encoding == 'sa_ii' else 'ra'}_rate_hz_{selected}"]))
    if not pc:
        raise ValueError("MCU rate stream is empty")
    events = []
    with commands[0].open() as file:
        for line in file:
            if line.strip():
                events.append(json.loads(line))
    return (selected, encoding, np.asarray(pc, dtype=np.int64),
            np.asarray(pressure), np.asarray(requested), events)


def plot_overview(output, *, x, lo, hi, first_s, last_s, onsets,
                  scope_x, pressure, requested, sf_x, sf_hz,
                  physical_mid, physical_hz, taxel, encoding,
                  clock_status, record_index, muted_sf_count=0):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axs = plt.subplots(4, 1, figsize=(16, 12), sharex=True,
                            layout="constrained")
    axs[0].fill_between(x, lo, hi, alpha=.75,
                        label="CH1 original samples: min/max envelope")
    if len(onsets):
        y0 = float(np.min(lo))
        ylim = float(np.max(hi)-y0)
        axs[0].scatter(onsets, np.full(len(onsets), y0 - .03*ylim), s=7,
                       marker="|", color="C3",
                       label=f"{len(onsets)} approved negative pulse onsets")
    axs[0].set_ylabel("Physical CH1 (V)")
    axs[0].legend(loc="upper right", fontsize=9)

    if len(scope_x):
        axs[1].plot(scope_x, pressure, lw=.85, label=f"MCU taxel {taxel}")
        axs[2].plot(scope_x, requested, lw=.85,
                    label=f"MCU {encoding} requested Hz")
    axs[1].set_ylabel(f"Taxel {taxel} pressure")
    axs[1].set_ylim(0, 255)
    axs[2].set_ylabel("MCU request (Hz)")
    axs[2].set_ylim(bottom=0)
    for ax in axs[1:3]:
        ax.legend(loc="upper right", fontsize=9)

    if len(physical_mid):
        axs[3].scatter(physical_mid, physical_hz, s=12, alpha=.75,
                       label="Approved physical 1 / period (Hz)")
    if len(sf_x):
        axs[3].scatter(sf_x, sf_hz, s=15, alpha=.4, marker="x",
                       label="SF ACK value (provisional position)")
    axs[3].set_ylabel("Frequency (Hz)")
    axs[3].set_ylim(bottom=0)
    axs[3].legend(loc="upper right", fontsize=9)
    axs[3].set_xlabel("Oscilloscope time (s); host-aligned MCU/SF times PROVISIONAL")

    for ax in axs:
        ax.grid(alpha=.2)
        ax.set_xlim(first_s, last_s)
    fig.suptitle(
        f"Record {record_index}: CH1, tactile input, requested and measured frequency\n"
        f"Scope-native CH1/onsets; MCU and SF ACK positions provisional "
        f"(clock {clock_status}). "
        f"{muted_sf_count} muted SF400 ACKs excluded from frequency display.",
        fontsize=11)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=140)
    plt.close(fig)


def generate(root, *, record_index=1, taxel=None, bins=2400):
    root = Path(root).expanduser().resolve()
    metadata = json.loads((root / "metadata.json").read_text())
    records = [rec for rec in metadata.get("records", [])
               if int(rec["index"]) == int(record_index)]
    if len(records) != 1 or records[0].get("status") != "captured":
        raise ValueError("Expected one fully captured scope record")
    record = records[0]
    review_dir = analysis_root(root) / f"record_{record_index:05d}" / "pulse_review"
    approval, onsets = verified_onsets(review_dir)
    ch1 = record["channels"]["CH1"]
    wave = root / ch1["path"]
    x, lo, hi, first_s, last_s = waveform_envelope(
        wave, ch1["waveform"], approval["source_ch1_sha256"], bins=bins)
    if len(onsets) and (onsets[0] < first_s or onsets[-1] > last_s):
        raise ValueError("Signed onsets outside CH1 acquisition")
    epoch = json.loads((root / "epoch_clock_analysis.json").read_text())
    matches = [row for row in epoch["records"]
               if int(row["record_index"]) == int(record_index)]
    if len(matches) != 1:
        raise ValueError("No unique saved per-record scope-to-host clock map")
    cm = matches[0]
    h0 = cm.get("first_sample_host_monotonic_ns_est")
    h1 = cm.get("last_sample_host_monotonic_ns_est")
    if h0 is None or h1 is None or int(h1) <= int(h0):
        raise ValueError("Scope-to-host estimate unavailable; cannot align MCU")
    mapping = int(h0), int(h1)
    taxel, encoding, pc, pressure, requested, events = mcu_and_command_data(
        root, taxel=taxel)
    t = host_to_scope(pc, mapping, first_s, last_s)
    inside = (t >= first_s) & (t <= last_s)
    ack_rows = [e for e in events if e.get("event") == "sf_ack"
                and e.get("status") == "OK"
                and e.get("frequency_hz") is not None
                and e.get("pc_monotonic_ns") is not None]
    sf_x = host_to_scope(
        np.asarray([int(e["pc_monotonic_ns"]) for e in ack_rows], dtype=np.int64),
        mapping, first_s, last_s)
    sf_hz = np.asarray([float(e["frequency_hz"]) for e in ack_rows])
    ack_inside = (sf_x >= first_s) & (sf_x <= last_s)
    # SF400 is transport activity while SC=0, NOT a physically emitted
    # 400-Hz frequency. Keep its count visible in the title, but omit it
    # from the shared measured-frequency axis so genuine data remain legible.
    muted = np.asarray([bool(e.get("zero_current_workaround"))
                        for e in ack_rows], dtype=bool)
    muted_sf_count = int(np.count_nonzero(ack_inside & muted))
    plotted_ack = ack_inside & ~muted
    dt = np.diff(onsets)
    # Long pauses represent unobserved stimulation, not a slow running rate.
    periodic = (dt >= 1/400) & (dt <= .5)
    physical_mid = (onsets[:-1] + onsets[1:])/2
    output = analysis_root(root) / f"record_{record_index:05d}" / "integrated_overview.png"
    plot_overview(
        output, x=x, lo=lo, hi=hi, first_s=first_s, last_s=last_s,
        onsets=onsets, scope_x=t[inside], pressure=pressure[inside],
        requested=requested[inside], sf_x=sf_x[plotted_ack],
        sf_hz=sf_hz[plotted_ack], physical_mid=physical_mid[periodic],
        physical_hz=(1/dt[periodic]), taxel=taxel, encoding=encoding,
        clock_status=cm.get("status", "unknown"), record_index=record_index,
        muted_sf_count=muted_sf_count)
    print(f"[INTEGRATED OVERVIEW] Saved {output} ({len(onsets)} approved onsets, "
          f"{int(np.count_nonzero(inside))} mapped MCU readings, "
          f"{int(np.count_nonzero(ack_inside))} SF ACKs ({muted_sf_count} muted SF400 omitted from frequency axis); clock uncalibrated)")
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--record", type=int, default=1)
    parser.add_argument("--taxel", type=int, choices=range(12))
    parser.add_argument("--bins", type=int, default=2400,
                        help="CH1 waveform min/max display bins")
    args = parser.parse_args(argv)
    return generate(args.run_dir, record_index=args.record,
                    taxel=args.taxel, bins=args.bins)


if __name__ == "__main__":
    main()
