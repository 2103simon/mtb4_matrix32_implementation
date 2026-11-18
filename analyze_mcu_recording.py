import json
import numpy as np
from pathlib import Path
import matplotlib.pyplot as plt

# Project root directory
BASE_DIR = Path(__file__).resolve().parent

# Configuration file
CONFIG_PATH = BASE_DIR / "config" / "config.json"

DATETIME = "20261118_084333"

OUTPUT_FOLDER = "./analysis"

def load_recordings(data_path, date_time):
    """Load MCU CSV files, including optional host arrival timestamps."""
    data_path = Path(data_path)

    def read_csv(path):
        # Named columns tolerate the additional host timestamp and older CSVs.
        return np.genfromtxt(
            path, delimiter=",", names=True, dtype=None, encoding=None, ndmin=1
        )

    taxels = read_csv(data_path / f"{date_time}_taxel.csv")
    events = read_csv(data_path / f"{date_time}_events.csv")
    if len(taxels) == 0:
        raise ValueError("Taxel recording is empty")

    corrected = np.asarray(taxels["corrected_timestamp_ms"], dtype=float)
    raw = np.asarray(taxels["raw_timestamp_ms"], dtype=float)
    values = np.column_stack([taxels[f"taxel_{i}"] for i in range(12)])

    event_names = events.dtype.names
    event_time = (
        events["corrected_timestamp_ms"]
        if "corrected_timestamp_ms" in event_names
        else events["timestamp_ms"]
    )
    event_time = np.asarray(event_time, dtype=float) - corrected[0]
    event_ids = np.asarray(events["id0"], dtype=int)
    channels = np.asarray(events["id1"], dtype=int)

    ra_i = np.column_stack((event_time[event_ids == 0], channels[event_ids == 0]))
    sa_ii = np.column_stack((event_time[event_ids == 1], channels[event_ids == 1]))

    data = {
        "taxel_raw_timestamps": raw,
        "taxel_corrected_timestamps": corrected - corrected[0],
        "taxel_data": values,
        "ra_i_data": ra_i,
        "sa_ii_data": sa_ii,
    }

    # Optional combined recording (newer MCU recordings only).
    if "pc_arrival_monotonic_ns" in taxels.dtype.names:
        data["taxel_pc_ns"] = np.asarray(
            taxels["pc_arrival_monotonic_ns"], dtype=np.int64
        )
        data["event_corrected_ms"] = np.asarray(event_time, dtype=float)
        data["event_ids"] = event_ids
        data["event_channels"] = channels

    omega_path = data_path / f"{date_time}_omega.csv"
    if omega_path.exists():
        omega = read_csv(omega_path)
        if len(omega) == 0:
            raise ValueError("Omega recording is empty")
        data["omega_pc_ns"] = np.asarray(omega["pc_monotonic_ns"], dtype=np.int64)
        data["omega_pos"] = np.column_stack(
            [omega[f"pos_{axis}"] for axis in "xyz"]
        )
        data["omega_vel"] = np.column_stack(
            [omega[f"vel_{axis}"] for axis in "xyz"]
        )
        data["omega_force"] = np.column_stack(
            [omega[f"force_{axis}"] for axis in "xyz"]
        )

    return data


def calculate_taxel_derivatives(data):

    taxel_data = data["taxel_data"]
    timestamps = data["taxel_corrected_timestamps"]

    derivatives = np.gradient(
        taxel_data,
        timestamps / 1000.0,
        axis=0
    )

    return derivatives

def find_max_activity_window(data, window_sec=2.0):

    timestamps = np.asarray(
        data["taxel_corrected_timestamps"]
    )

    values = np.asarray(
        data["taxel_data"]
    )

    # Convert window duration to milliseconds
    window_ms = window_sec * 1000

    # Calculate absolute changes between consecutive samples
    dx = np.abs(np.diff(values.astype(float), axis=0))

    # Sum changes across all 12 taxels
    activity = np.sum(dx, axis=1)

    # Cumulative activity
    cumulative = np.concatenate((
        [0],
        np.cumsum(activity)
    ))

    # Sliding window start positions
    start_indices = np.arange(len(timestamps))

    # Find corresponding end indices
    end_indices = np.searchsorted(
        timestamps,
        timestamps + window_ms,
        side="right"
    )

    # Accumulated activity in each window
    window_activity = (
        cumulative[end_indices - 1]
        - cumulative[start_indices]
    )

    # Find most active window
    max_idx = np.argmax(window_activity)

    start_time = timestamps[max_idx]
    end_time = start_time + window_ms

    print(
        f"Most active window: "
        f"{start_time:.1f} - {end_time:.1f} ms"
    )

    print(
        f"Accumulated activity: "
        f"{window_activity[max_idx]:.2f}"
    )

    return start_time, end_time

def plot_traces(data):

    # Extract data
    taxel_timestamps = data["taxel_corrected_timestamps"]
    taxel_values = data["taxel_data"]
    taxel_deriv = data["taxel_deriv"]
    ra_i_events = data["ra_i_data"]
    sa_ii_events = data["sa_ii_data"]

    # Create figure
    fig, axs = plt.subplots(
        3, 1,
        figsize=(14, 10),
        sharex=True,
        layout="constrained"
    )

    # 1. Raw taxel traces
    lines = axs[0].plot(taxel_timestamps, taxel_values)

    # Extract channel colors
    channel_colors = [line.get_color() for line in lines]

    axs[0].legend(
        [f"Taxel {i}" for i in range(12)],
        ncol=6,
        fontsize=8,
        loc="upper center"
    )
    axs[0].set_ylabel("Taxel value")

    # 4. RA-I events
    axs[1].scatter(
        ra_i_events[:, 0],
        ra_i_events[:, 1],
        c=[channel_colors[int(ch)] for ch in ra_i_events[:, 1]],
        marker="|"
    )
    axs[1].set_ylabel("RA-I channel")
    axs[1].set_yticks(range(12))
    

    # # 3. Taxel derivatives
    # derivative_lines = axs[2].plot(
    #     taxel_timestamps,
    #     taxel_deriv
    # )

    # # Give derivatives the same colors
    # for i, line in enumerate(derivative_lines):
    #     line.set_color(channel_colors[i])

    # axs[2].set_ylabel("dx/dt")

    # 2. SA-II events
    axs[2].scatter(
        sa_ii_events[:, 0],
        sa_ii_events[:, 1],
        c=[channel_colors[int(ch)] for ch in sa_ii_events[:, 1]],
        marker="|"
    )
    axs[2].set_ylabel("SA-II channel")
    axs[2].set_yticks(range(12))
    axs[2].set_xlabel("Time (ms)")

    fig.align_ylabels()

    plt.savefig(f"{OUTPUT_FOLDER}/{DATETIME}_traces.png")

    plt.close(fig)

def plot_traces_zoom(data, window_sec=2.0):

    # ----------------------------------
    # 1. Find most active window
    # ----------------------------------

    start_time, end_time = find_max_activity_window(
        data,
        window_sec=window_sec
    )

    # ----------------------------------
    # 2. Extract data
    # ----------------------------------

    taxel_timestamps = data["taxel_corrected_timestamps"]
    taxel_values = data["taxel_data"]
    taxel_deriv = data["taxel_deriv"]

    ra_i_events = data["ra_i_data"]
    sa_ii_events = data["sa_ii_data"]

    # ----------------------------------
    # 3. Filter taxel measurements
    # ----------------------------------

    taxel_mask = (
        (taxel_timestamps >= start_time) &
        (taxel_timestamps <= end_time)
    )

    t_zoom = taxel_timestamps[taxel_mask]
    values_zoom = taxel_values[taxel_mask]

    # Handle derivative timestamps
    if len(taxel_deriv) == len(taxel_timestamps):
        deriv_timestamps = taxel_timestamps
    elif len(taxel_deriv) == len(taxel_timestamps) - 1:
        deriv_timestamps = taxel_timestamps[1:]
    else:
        raise ValueError("Unexpected derivative array length")

    deriv_mask = (
        (deriv_timestamps >= start_time) &
        (deriv_timestamps <= end_time)
    )

    deriv_t_zoom = deriv_timestamps[deriv_mask]
    deriv_zoom = taxel_deriv[deriv_mask]

    # ----------------------------------
    # 4. Filter events
    # ----------------------------------

    ra_mask = (
        (ra_i_events[:, 0] >= start_time) &
        (ra_i_events[:, 0] <= end_time)
    )

    sa_mask = (
        (sa_ii_events[:, 0] >= start_time) &
        (sa_ii_events[:, 0] <= end_time)
    )

    ra_zoom = ra_i_events[ra_mask]
    sa_zoom = sa_ii_events[sa_mask]

    # ----------------------------------
    # 5. Create figure
    # ----------------------------------

    fig, axs = plt.subplots(
        3, 1,
        figsize=(14, 10),
        sharex=True,
        layout="constrained"
    )

    # ----------------------------------
    # 6. Raw taxel measurements
    # ----------------------------------

    lines = axs[0].plot(t_zoom, values_zoom)

    channel_colors = [
        line.get_color() for line in lines
    ]

    axs[0].legend(
        [f"Taxel {i}" for i in range(12)],
        ncol=6,
        fontsize=8,
        loc="upper center"
    )

    axs[0].set_ylabel("Taxel value")

    axs[1].scatter(
        ra_zoom[:, 0],
        ra_zoom[:, 1],
        c=[
            channel_colors[int(ch)]
            for ch in ra_zoom[:, 1]
        ],
        marker="|"
    )

    axs[1].set_ylabel("RA-I channel")
    axs[1].set_yticks(range(12))
# ----------------------------------
    # 7. SA-II events
    # ----------------------------------

    axs[2].scatter(
        sa_zoom[:, 0],
        sa_zoom[:, 1],
        c=[
            channel_colors[int(ch)]
            for ch in sa_zoom[:, 1]
        ],
        marker="|"
    )

    axs[2].set_ylabel("SA-II channel")
    axs[2].set_yticks(range(12))

    # ----------------------------------
    # 8. Taxel derivatives
    # ----------------------------------

    # derivative_lines = axs[2].plot(
    #     deriv_t_zoom,
    #     deriv_zoom
    # )

    # for i, line in enumerate(derivative_lines):
    #     line.set_color(channel_colors[i])

    # axs[2].set_ylabel("dx/dt")

    # ----------------------------------
    # 9. RA-I events
    # ----------------------------------

    
    axs[2].set_xlabel("Time (ms)")

    # ----------------------------------
    # 10. Finalize
    # ----------------------------------

    for ax in axs:
        ax.set_xlim(start_time, end_time)

    fig.suptitle(
        f"Highest taxel activity ({window_sec:.2f} s window)"
    )

    fig.align_ylabels()

    plt.savefig(f"{OUTPUT_FOLDER}/{DATETIME}_traces_zoom.png")

    plt.close(fig)


def plot_combined(data):
    """Plot Omega and MCU measurements in one figure on the host clock axis.

    Packet reception times include serial latency. Event positions are estimated
    from corrected MCU time using the taxel arrival-time offset (not calibrated).
    """
    if "taxel_pc_ns" not in data:
        raise ValueError("MCU recording lacks monotonic arrival timestamps")

    t0 = min(int(data["taxel_pc_ns"][0]), int(data["omega_pc_ns"][0]))
    def seconds(ns):
        return (np.asarray(ns, dtype=np.int64) - t0) / 1e9

    omega_t = seconds(data["omega_pc_ns"])
    taxel_t = seconds(data["taxel_pc_ns"])

    # Recover individual event spacing instead of placing entire packets at
    # their shared arrival time. Assumes corrected MCU clock is already close
    # to PC clock rate; the fixed serial latency remains unknown.
    corrected_ms = data["taxel_corrected_timestamps"]
    expected_taxel_t = corrected_ms / 1000.0
    offset_s = np.median(taxel_t - expected_taxel_t)
    event_t = data["event_corrected_ms"] / 1000.0 + offset_s

    fig, axs = plt.subplots(
        6, 1, figsize=(15, 16), sharex=True, layout="constrained"
    )
    labels = ("X", "Y", "Z")
    for ax, key, ylabel in zip(
        axs[:3],
        ("omega_pos", "omega_vel", "omega_force"),
        ("Omega position (m)", "Omega velocity (m/s)", "Omega force (N)"),
    ):
        ax.plot(omega_t, data[key], label=labels)
        ax.set_ylabel(ylabel)
        ax.legend(ncol=3, fontsize=8, loc="upper right")

    lines = axs[3].plot(taxel_t, data["taxel_data"])
    colors = [line.get_color() for line in lines]
    axs[3].set_ylabel("Taxel value")
    axs[3].legend(
        [f"Taxel {i}" for i in range(12)],
        ncol=6, fontsize=8, loc="upper right"
    )

    for ax, event_type, ylabel in zip(
        axs[4:], (0, 1), ("RA-I channel", "SA-II channel")
    ):
        mask = data["event_ids"] == event_type
        channels = data["event_channels"][mask]
        ax.scatter(
            event_t[mask], channels,
            c=[colors[int(ch)] for ch in channels],
            marker="|", s=24
        )
        ax.set_ylabel(ylabel)
        ax.set_yticks(range(12))

    axs[-1].set_xlabel("PC monotonic time since first sample (s)")
    fig.suptitle("Omega and MCU recordings (approximate host-clock alignment)")
    fig.savefig(f"{OUTPUT_FOLDER}/{DATETIME}_traces_combined.png", dpi=150)
    plt.close(fig)


def main():
    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)
    
    data = load_recordings(config["recordings_dir"], DATETIME)
    
    data["taxel_deriv"] = calculate_taxel_derivatives(data)
    
    if "omega_pc_ns" in data and "taxel_pc_ns" in data:
        plot_combined(data)
        print("Saved traces_combined.png")
    else:
        # Keep the original MCU-only plots for older recordings.
        plot_traces(data)
        plot_traces_zoom(data, 0.5)
    

if __name__ == "__main__":
    main()
