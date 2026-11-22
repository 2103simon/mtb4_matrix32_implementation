"""Plot MCU taxels and unconstrained RA-I / SA-II requested rates.

New firmware: YYYYMMDD_HHMMSS_tactile_rates.csv
Legacy firmware: YYYYMMDD_HHMMSS_taxel.csv + _events.csv

Examples (from repository root):
    python analyze_mcu_recording.py
    python analyze_mcu_recording.py --datetime 20261119_082300
    python analyze_mcu_recording.py --taxel 3 --zoom-sec 0.5
"""
import argparse
import csv
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config" / "config.json"
OUTPUT_FOLDER = BASE_DIR / "analysis"
CHANNEL_COUNT = 12


def choose_recording(data_dir, requested=None):
    """Choose a recording ID, prioritizing complete rate CSVs."""
    if requested:
        return requested
    candidates = list(data_dir.glob("*_tactile_rates.csv"))
    # In legacy mode only taxel files exist. New-mode recorder also creates
    # empty taxel/event CSVs, so rate CSVs must take precedence.
    if not candidates:
        candidates = list(data_dir.glob("*_taxel.csv"))
    if not candidates:
        raise FileNotFoundError(
            f"No *_tactile_rates.csv or *_taxel.csv recordings in {data_dir}"
        )
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    suffix = ("_tactile_rates.csv" if latest.name.endswith("_tactile_rates.csv")
              else "_taxel.csv")
    return latest.name[:-len(suffix)]


def _read_rows(path):
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def _array(rows, prefix):
    return np.asarray(
        [[float(row[f"{prefix}{i}"]) for i in range(CHANNEL_COUNT)] for row in rows],
        dtype=float,
    )


def load_recordings(data_dir, recording_id):
    """Return time-aligned pressure and requested frequency arrays.

    When the new 0x05 rate recording exists, it is authoritative and the
    header-only legacy event/taxel CSVs are deliberately ignored.
    """
    data_dir = Path(data_dir)
    rates_path = data_dir / f"{recording_id}_tactile_rates.csv"
    data = {"recording_id": recording_id}
    settings_path = data_dir / f"{recording_id}_settings.json"
    if settings_path.is_file():
        with settings_path.open(encoding="utf-8") as handle:
            manifest = json.load(handle)
        if manifest.get("recording_id") != recording_id:
            raise ValueError(f"Manifest recording_id mismatch: {settings_path}")
        data["experiment_settings"] = manifest

    if rates_path.exists():
        rows = _read_rows(rates_path)
        if not rows:
            raise ValueError(f"Rate recording is empty: {rates_path}")
        corrected = np.asarray(
            [float(row["corrected_timestamp_ms"]) for row in rows], dtype=float
        )
        data.update({
            "mode": "rates",
            "taxel_data": _array(rows, "taxel_"),
            "ra_rate_hz": _array(rows, "ra_rate_hz_"),
            "sa_rate_hz": _array(rows, "sa_rate_hz_"),
            "taxel_corrected_timestamps": corrected - corrected[0],
            "taxel_raw_timestamps": np.asarray(
                [int(row["raw_timestamp_ms"]) for row in rows], dtype=np.uint64
            ),
            "taxel_pc_ns": np.asarray(
                [int(row["pc_arrival_monotonic_ns"]) for row in rows], dtype=np.int64
            ),
        })
    else:
        taxel_rows = _read_rows(data_dir / f"{recording_id}_taxel.csv")
        if not taxel_rows:
            raise ValueError(f"Legacy taxel recording is empty for {recording_id}")
        corrected = np.asarray(
            [float(row["corrected_timestamp_ms"]) for row in taxel_rows], dtype=float
        )
        event_path = data_dir / f"{recording_id}_events.csv"
        event_rows = _read_rows(event_path) if event_path.exists() else []
        events = np.asarray(
            [[float(row.get("corrected_timestamp_ms", row.get("timestamp_ms"))) - corrected[0],
              int(row["id0"]), int(row["id1"])]
             for row in event_rows],
            dtype=float,
        ).reshape(-1, 3)
        data.update({
            "mode": "events",
            "taxel_data": _array(taxel_rows, "taxel_"),
            "taxel_corrected_timestamps": corrected - corrected[0],
            "taxel_raw_timestamps": np.asarray(
                [int(row["raw_timestamp_ms"]) for row in taxel_rows], dtype=np.uint64
            ),
            "events": events,
        })
        if "pc_arrival_monotonic_ns" in taxel_rows[0]:
            data["taxel_pc_ns"] = np.asarray(
                [int(row["pc_arrival_monotonic_ns"]) for row in taxel_rows],
                dtype=np.int64,
            )

    omega_path = data_dir / f"{recording_id}_omega.csv"
    if omega_path.exists():
        omega_rows = _read_rows(omega_path)
        if omega_rows:
            data["omega_pc_ns"] = np.asarray(
                [int(row["pc_monotonic_ns"]) for row in omega_rows], dtype=np.int64
            )
            for key in ("pos", "vel", "force"):
                data[f"omega_{key}"] = np.asarray(
                    [[float(row[f"{key}_{axis}"]) for axis in "xyz"]
                     for row in omega_rows], dtype=float
                )
    return data


def find_max_activity_window(data, window_sec=0.5):
    """Return the highest-activity interval on the MCU time axis (ms)."""
    t = data["taxel_corrected_timestamps"]
    values = data["taxel_data"]
    if len(t) < 2:
        return float(t[0]), float(t[0] + window_sec * 1000)
    changes = np.zeros(len(t), dtype=float)
    changes[1:] = np.abs(np.diff(values, axis=0)).sum(axis=1)
    prefix = np.r_[0., np.cumsum(changes)]
    end_idx = np.searchsorted(t, t + window_sec * 1000, side="right")
    scores = prefix[end_idx] - prefix[np.arange(len(t))]
    best = int(np.argmax(scores))
    return float(t[best]), float(t[best] + window_sec * 1000)


def _plot_rate_axes(axs, t, data, mask, channel_colors, selected_taxel=None):
    """Plot requested frequency traces; never infer rates from event spikes."""
    for ax, key, label in zip(
        axs,
        ("ra_rate_hz", "sa_rate_hz"),
        ("RA-I requested rate (Hz)", "SA-II requested rate (Hz)"),
    ):
        rates = data[key][mask]
        if selected_taxel is None:
            for channel in range(CHANNEL_COUNT):
                ax.plot(t, rates[:, channel], color=channel_colors[channel],
                        linewidth=1, alpha=0.85)
        else:
            ax.plot(t, rates[:, selected_taxel],
                    color=channel_colors[selected_taxel], linewidth=1.5)
        ax.set_ylabel(label)
        ax.set_ylim(bottom=0)
        ax.grid(True, alpha=0.25)


def _experiment_label(data):
    """Concise plot annotation sourced from the saved experiment manifest."""
    settings = data.get("experiment_settings")
    if not settings:
        return ""
    config = settings.get("configuration", {})
    encoding = config.get("tactile_encoding", {})
    source = config.get("stimulation_source", {})
    if not encoding or not source:
        return ""
    return (
        f"RA threshold={encoding.get('raThreshold', '?')}, "
        f"SA max={encoding.get('saMaxFrequencyHz', '?')} Hz, "
        f"selected taxel={source.get('taxel', '?')} "
        f"({source.get('encoding', '?')})"
    )


def plot_traces(data, output, interval=None, selected_taxel=None, x_axis="mcu"):
    """Three aligned panels: raw taxels, RA requested rate, SA requested rate."""
    if x_axis == "pc":
        if "taxel_pc_ns" not in data:
            raise ValueError("Recording has no PC arrival timestamps")
        t = (data["taxel_pc_ns"] - data["taxel_pc_ns"][0]) / 1e6
    else:
        t = data["taxel_corrected_timestamps"]

    if interval is None:
        mask = np.ones(len(t), dtype=bool)
    else:
        mask = (t >= interval[0]) & (t <= interval[1])
    if not mask.any():
        raise ValueError("No samples in selected plot interval")

    tx = t[mask]
    fig, axs = plt.subplots(3, 1, figsize=(15, 11), sharex=True,
                            layout="constrained")
    taxels = data["taxel_data"][mask]
    if selected_taxel is None:
        lines = axs[0].plot(tx, taxels, linewidth=1)
        colors = [line.get_color() for line in lines]
        axs[0].legend([f"Taxel {i}" for i in range(CHANNEL_COUNT)],
                      ncol=6, fontsize=8, loc="upper center")
    else:
        # Still assign stable channel colors across plots.
        color_cycle = plt.rcParams["axes.prop_cycle"].by_key()["color"]
        colors = [color_cycle[i % len(color_cycle)] for i in range(CHANNEL_COUNT)]
        axs[0].plot(tx, taxels[:, selected_taxel],
                    color=colors[selected_taxel], linewidth=1.5,
                    label=f"Taxel {selected_taxel}")
        axs[0].legend(loc="upper right")
    axs[0].set_ylabel("Taxel pressure (0–255)")
    axs[0].set_ylim(0, 255)
    axs[0].grid(True, alpha=0.25)

    if data["mode"] == "rates":
        _plot_rate_axes(axs[1:], tx, data, mask, colors, selected_taxel)
    else:
        # Retain legacy support only when no rate recording exists.
        events = data["events"]
        for ax, kind, name in zip(
            axs[1:], (0, 1), ("RA-I spikes (legacy)", "SA-II spikes (legacy)")
        ):
            matches = events[events[:, 1] == kind]
            if interval is not None:
                matches = matches[(matches[:, 0] >= interval[0])
                                  & (matches[:, 0] <= interval[1])]
            if selected_taxel is not None:
                matches = matches[matches[:, 2] == selected_taxel]
            ax.scatter(matches[:, 0], matches[:, 2], marker="|", s=25,
                       c=[colors[int(channel)] for channel in matches[:, 2]])
            ax.set_ylabel(name)
            ax.set_yticks([selected_taxel] if selected_taxel is not None
                          else range(CHANNEL_COUNT))
            ax.grid(True, alpha=0.25)

    axs[-1].set_xlabel("Elapsed PC arrival time (ms)" if x_axis == "pc"
                        else "Elapsed corrected MCU time (ms)")
    if interval is not None:
        axs[-1].set_xlim(*interval)
    fig.suptitle(
        f"{data['recording_id']} — {'MCU rates' if data['mode'] == 'rates' else 'legacy spikes'}"
        + (f" — taxel {selected_taxel}" if selected_taxel is not None else "")
        + (f"\n{_experiment_label(data)}" if _experiment_label(data) else "")
    )
    fig.savefig(output, dpi=150)
    plt.close(fig)


def plot_combined(data, output, selected_taxel=None):
    """Optional Omega + MCU plot using the shared PC monotonic arrival axis."""
    if "omega_pc_ns" not in data or "taxel_pc_ns" not in data:
        return False
    t0 = min(int(data["omega_pc_ns"][0]), int(data["taxel_pc_ns"][0]))
    omega_t = (data["omega_pc_ns"] - t0) / 1e9
    tactile_t = (data["taxel_pc_ns"] - t0) / 1e9
    fig, axs = plt.subplots(6, 1, figsize=(15, 16), sharex=True,
                            layout="constrained")
    for ax, key, label in zip(
        axs[:3],
        ("omega_pos", "omega_vel", "omega_force"),
        ("Omega position (m)", "Omega velocity (m/s)", "Omega force (N)"),
    ):
        ax.plot(omega_t, data[key])
        ax.set_ylabel(label)
        ax.grid(True, alpha=0.25)

    color_cycle = plt.rcParams["axes.prop_cycle"].by_key()["color"]
    colors = [color_cycle[i % len(color_cycle)] for i in range(CHANNEL_COUNT)]
    indices = [selected_taxel] if selected_taxel is not None else range(CHANNEL_COUNT)
    for i in indices:
        axs[3].plot(tactile_t, data["taxel_data"][:, i], color=colors[i],
                    label=f"Taxel {i}")
    axs[3].set_ylabel("Taxel pressure")
    axs[3].legend(ncol=6, fontsize=8)

    if data["mode"] == "rates":
        for ax, key, label in zip(
            axs[4:],
            ("ra_rate_hz", "sa_rate_hz"),
            ("RA-I requested rate (Hz)", "SA-II requested rate (Hz)"),
        ):
            for i in indices:
                ax.plot(tactile_t, data[key][:, i], color=colors[i], linewidth=1)
            ax.set_ylabel(label)
            ax.set_ylim(bottom=0)
            ax.grid(True, alpha=0.25)
    else:
        # Legacy event times are corrected MCU timestamps, aligned using the
        # median PC arrival offset. This is only approximate.
        offset_s = np.median(
            tactile_t - data["taxel_corrected_timestamps"] / 1000.0
        )
        events = data["events"]
        for ax, kind, label in zip(
            axs[4:], (0, 1), ("RA-I spikes", "SA-II spikes")
        ):
            selected = events[events[:, 1] == kind]
            if selected_taxel is not None:
                selected = selected[selected[:, 2] == selected_taxel]
            ax.scatter(selected[:, 0] / 1000.0 + offset_s, selected[:, 2],
                       marker="|", s=24,
                       c=[colors[int(ch)] for ch in selected[:, 2]])
            ax.set_ylabel(label)
            ax.set_yticks(indices)

    axs[-1].set_xlabel("PC monotonic time since first sample (s)")
    fig.suptitle("Omega and tactile measurements (host-clock alignment)"
                 + (f"\n{_experiment_label(data)}" if _experiment_label(data) else ""))
    fig.savefig(output, dpi=150)
    plt.close(fig)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--datetime",
        help="Full recording ID (including settings suffix), or omit for latest",
    )
    parser.add_argument("--taxel", type=int, choices=range(CHANNEL_COUNT),
                        metavar="0-11", help="Show only this taxel across all three panels")
    parser.add_argument("--zoom-sec", type=float, default=0.5,
                        help="Duration of most-active zoom plot in seconds (default 0.5)")
    parser.add_argument("--pc-time", action="store_true",
                        help="Use PC arrival timestamps instead of corrected MCU time")
    args = parser.parse_args()
    if args.zoom_sec <= 0:
        parser.error("--zoom-sec must be positive")

    with CONFIG_PATH.open() as handle:
        config = json.load(handle)
    data_dir = Path(config["recordings_dir"])
    if not data_dir.is_absolute():
        data_dir = BASE_DIR / data_dir
    recording_id = choose_recording(data_dir, args.datetime)
    data = load_recordings(data_dir, recording_id)
    if "experiment_settings" in data:
        print(f"Loaded experiment settings: {data_dir / (recording_id + '_settings.json')}")
        print(_experiment_label(data))
    else:
        print("No settings manifest found; plotting legacy recording metadata.")
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)

    axis = "pc" if args.pc_time else "mcu"
    suffix = f"_taxel{args.taxel}" if args.taxel is not None else ""
    full_path = OUTPUT_FOLDER / f"{recording_id}_traces{suffix}.png"
    plot_traces(data, full_path, selected_taxel=args.taxel, x_axis=axis)
    print(f"Saved {full_path} ({len(data['taxel_data'])} samples, {data['mode']} mode)")

    # Active-window detection is on corrected MCU time. Convert its bounds to
    # PC arrival time only when plotting on the PC clock.
    start_ms, end_ms = find_max_activity_window(data, args.zoom_sec)
    if axis == "pc":
        mcu_t = data["taxel_corrected_timestamps"]
        pc_t = (data["taxel_pc_ns"] - data["taxel_pc_ns"][0]) / 1e6
        start_ms = float(np.interp(start_ms, mcu_t, pc_t))
        end_ms = float(np.interp(end_ms, mcu_t, pc_t))
    zoom_path = OUTPUT_FOLDER / f"{recording_id}_traces_zoom{suffix}.png"
    plot_traces(data, zoom_path, interval=(start_ms, end_ms),
                selected_taxel=args.taxel, x_axis=axis)
    print(f"Saved {zoom_path}")

    combined_path = OUTPUT_FOLDER / f"{recording_id}_traces_combined{suffix}.png"
    if plot_combined(data, combined_path, selected_taxel=args.taxel):
        print(f"Saved {combined_path}")


if __name__ == "__main__":
    main()
