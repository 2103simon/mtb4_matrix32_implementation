"""
HDF5 data reader and visualization tool for Phalanx sensor data.

This script loads tactile sensor data from HDF5 files created by 
read_from_fingertip.py and provides analysis and visualization capabilities.

Features:
- Load taxel pressure, IMU, and neuromorphic event data
- Display file metadata and data structure
- Generate publication-quality plots
- Export selected data ranges to new files
- Statistical analysis of sensor data

Usage:
    # Basic usage - loads file and generates plots
    python read_h5_data.py data.h5

    # Show file structure
    python read_h5_data.py data.h5 --structure

    # Plot specific time window (1000ms start, 2000ms duration)
    python read_h5_data.py data.h5 --window 1000 2000

    # Only show statistics, no plots
    python read_h5_data.py data.h5 --stats-only

Author: Simon F. Muller-Cleve
Date: 2026-03-30
"""

import argparse
import sys
from pathlib import Path

import h5py as h5
import matplotlib.pyplot as plt
import numpy as np


def print_h5_structure(filename):
    """Print the complete structure and metadata of an HDF5 file.

    Args:
        filename (str): Path to the HDF5 file.
    """
    print(f"\n{'='*70}")
    print(f"HDF5 File Structure: {filename}")
    print(f"{'='*70}\n")

    with h5.File(filename, 'r') as hf:
        # Print top-level attributes
        print("File Attributes:")
        for key, value in hf.attrs.items():
            print(f"  {key}: {value}")

        print("\nData Groups:")

        def print_group(name, obj):
            """Recursively print group structure."""
            if isinstance(obj, h5.Group):
                print(f"\n  /{name}/")
                for key, value in obj.attrs.items():
                    print(f"    Attribute - {key}: {value}")
            elif isinstance(obj, h5.Dataset):
                print(
                    f"    Dataset - {name}: shape={obj.shape}, dtype={obj.dtype}")

        hf.visititems(print_group)

    print(f"\n{'='*70}\n")


def load_h5_data(filename):
    """Load all sensor data from an HDF5 file.

    Args:
        filename (str): Path to the HDF5 file.

    Returns:
        dict: Dictionary containing:
            - 'taxel_times': Taxel timestamps (ms)
            - 'taxel_values': Taxel pressure values (N, 12)
            - 'imu_times': IMU timestamps (ms)
            - 'imu_x/y/z': Accelerometer data (g)
            - 'ra_times': RA-I event timestamps (ms)
            - 'ra_channels': RA-I event channel IDs
            - 'sa_times': SA-II event timestamps (ms)
            - 'sa_channels': SA-II event channel IDs
            - 'metadata': File metadata dictionary
    """
    data = {}

    with h5.File(filename, 'r') as hf:
        # Load metadata
        data['metadata'] = dict(hf.attrs)

        # Load taxel data
        data['taxel_times'] = hf['taxel_data/timestamps'][:]
        data['taxel_values'] = hf['taxel_data/values'][:]

        # Load IMU data
        data['imu_times'] = hf['imu_data/timestamps'][:]
        data['imu_x'] = hf['imu_data/acceleration_x'][:]
        data['imu_y'] = hf['imu_data/acceleration_y'][:]
        data['imu_z'] = hf['imu_data/acceleration_z'][:]

        # Load RA-I events
        if 'events/ra_i/timestamps' in hf:
            data['ra_times'] = hf['events/ra_i/timestamps'][:]
            data['ra_channels'] = hf['events/ra_i/channel_id'][:]
        else:
            data['ra_times'] = np.array([])
            data['ra_channels'] = np.array([])

        # Load SA-II events
        if 'events/sa_ii/timestamps' in hf:
            data['sa_times'] = hf['events/sa_ii/timestamps'][:]
            data['sa_channels'] = hf['events/sa_ii/channel_id'][:]
        else:
            data['sa_times'] = np.array([])
            data['sa_channels'] = np.array([])

    return data


def plot_full_session(data, output_file='h5_full_session.png', input_filename=None):
    """Generate a comprehensive visualization of the entire recording session.

    Args:
        data (dict): Data dictionary from load_h5_data().
        output_file (str): Output filename for the plot.
        input_filename (str, optional): Original input filename to include in plot.
    """
    fig, axes = plt.subplots(5, 1, figsize=(12, 10))
    
    # Determine time range from data
    t_min = min(data['taxel_times'][0], data['imu_times'][0])
    t_max = max(data['taxel_times'][-1], data['imu_times'][-1])

    # Plot 1: Taxel pressure values
    ax1 = axes[0]
    for ch in range(12):
        ax1.plot(data['taxel_times'], data['taxel_values'][:, ch],
                 label=f'Ch{ch}', linewidth=1.0)
    ax1.set_ylabel('Taxel Pressure')
    ax1.set_ylim([0, 256])
    ax1.set_xlim([t_min, t_max])
    ax1.grid(True, alpha=0.3)
    ax1.set_xticklabels([])
    ax1.legend(ncol=6, fontsize=8, loc='upper right')

    # Plot 2: IMU data
    ax2 = axes[1]
    ax2.plot(data['imu_times'], data['imu_x'], 'r-', label='X', linewidth=0.8)
    ax2.plot(data['imu_times'], data['imu_y'], 'g-', label='Y', linewidth=0.8)
    ax2.plot(data['imu_times'], data['imu_z'], 'b-', label='Z', linewidth=0.8)
    ax2.set_ylabel('Acceleration (g)')
    ax2.set_xlim([t_min, t_max])
    ax2.grid(True, alpha=0.3)
    ax2.set_xticklabels([])
    ax2.legend(ncol=3, fontsize=8)

    # Plot 3: Taxel derivatives
    ax3 = axes[2]
    diff_taxel = np.diff(data['taxel_values'], axis=0)
    for ch in range(12):
        ax3.plot(data['taxel_times'][1:], diff_taxel[:, ch],
                 linewidth=1.0)
    ax3.set_ylabel('Taxel Rate of Change')
    ax3.set_xlim([t_min, t_max])
    ax3.grid(True, alpha=0.3)
    ax3.set_xticklabels([])

    # Plot 4: RA-I events
    ax4 = axes[3]
    if len(data['ra_times']) > 0:
        ax4.scatter(data['ra_times'], data['ra_channels'],
                    marker='|', s=50, c='red')
    ax4.set_ylabel('RA-I Events\n(Channel)')
    ax4.set_ylim([0, 13])
    ax4.set_xlim([t_min, t_max])
    ax4.grid(True, alpha=0.3)
    ax4.set_xticklabels([])

    # Plot 5: SA-II events
    ax5 = axes[4]
    if len(data['sa_times']) > 0:
        ax5.scatter(data['sa_times'], data['sa_channels'],
                    marker='|', s=50, c='blue')
    ax5.set_ylabel('SA-II Events\n(Channel)')
    ax5.set_ylim([0, 13])
    ax5.set_xlim([t_min, t_max])
    ax5.set_xlabel('Time (ms)')
    ax5.grid(True, alpha=0.3)

    # Add title with metadata and filename
    title = f"Falange Sensor Data - {data['metadata'].get('acquisition_date', 'Unknown')}"
    if input_filename:
        title += f"\nFile: {input_filename}"
    fig.suptitle(title, fontsize=14, fontweight='bold')
    fig.align_ylabels()
    
    plt.tight_layout()
    plt.savefig(output_file, dpi=500, bbox_inches='tight')
    plt.savefig(output_file.split(".")[0] + ".pdf", bbox_inches='tight')
    print(f"Full session plot saved: {output_file}")
    plt.close()


def plot_time_window(data, t_start=0, duration=2000, output_file='h5_window.png', input_filename=None):
    """Generate a detailed plot of a specific time window.

    Args:
        data (dict): Data dictionary from load_h5_data().
        t_start (float): Start time in milliseconds.
        duration (float): Window duration in milliseconds.
        output_file (str): Output filename for the plot.
        input_filename (str, optional): Original input filename to include in plot.
    """
    # Determine available time range from data
    data_t_min = min(data['taxel_times'][0], data['imu_times'][0])
    data_t_max = max(data['taxel_times'][-1], data['imu_times'][-1])
    
    # Validate and adjust window parameters
    original_t_start = t_start
    original_duration = duration
    adjusted = False
    
    if t_start < data_t_min:
        print(f"Warning: Requested start time {t_start:.2f} ms is before data start {data_t_min:.2f} ms")
        print(f"         Adjusting start time to {data_t_min:.2f} ms")
        t_start = data_t_min
        adjusted = True
    
    t_end = t_start + duration
    
    if t_end > data_t_max:
        print(f"Warning: Requested end time {t_end:.2f} ms exceeds data end {data_t_max:.2f} ms")
        print(f"         Adjusting end time to {data_t_max:.2f} ms")
        t_end = data_t_max
        duration = t_end - t_start
        adjusted = True
    
    if adjusted:
        print(f"Final window: {t_start:.2f} - {t_end:.2f} ms (duration: {duration:.2f} ms)")

    fig, axes = plt.subplots(5, 1, figsize=(12, 10))

    # Filter data for time window
    taxel_mask = (data['taxel_times'] >= t_start) & (data['taxel_times'] <= t_end)
    imu_mask = (data['imu_times'] >= t_start) & (data['imu_times'] <= t_end)
    ra_mask = (data['ra_times'] >= t_start) & (data['ra_times'] <= t_end)
    sa_mask = (data['sa_times'] >= t_start) & (data['sa_times'] <= t_end)

    # Plot 1: Taxel pressure
    ax1 = axes[0]
    for ch in range(12):
        ax1.plot(data['taxel_times'][taxel_mask],
                 data['taxel_values'][taxel_mask, ch],
                 label=f'Ch{ch}', linewidth=1.5)
    ax1.set_ylabel('Taxel Pressure')
    ax1.set_ylim([0, 256])
    ax1.set_xlim([t_start, t_end])
    ax1.grid(True, alpha=0.3)
    ax1.set_xticklabels([])
    ax1.legend(ncol=6, fontsize=8)

    # Plot 2: IMU
    ax2 = axes[1]
    ax2.plot(data['imu_times'][imu_mask], data['imu_x'][imu_mask], 
             'r-', label='X', linewidth=1.5)
    ax2.plot(data['imu_times'][imu_mask], data['imu_y'][imu_mask], 
             'g-', label='Y', linewidth=1.5)
    ax2.plot(data['imu_times'][imu_mask], data['imu_z'][imu_mask], 
             'b-', label='Z', linewidth=1.5)
    ax2.set_ylabel('Acceleration (g)')
    ax2.set_xlim([t_start, t_end])
    ax2.grid(True, alpha=0.3)
    ax2.set_xticklabels([])
    ax2.legend(ncol=3, fontsize=8)

    # Plot 3: Derivatives
    ax3 = axes[2]
    diff_times = data['taxel_times'][taxel_mask][1:]
    diff_values = np.diff(data['taxel_values'][taxel_mask, :], axis=0)
    for ch in range(12):
        ax3.plot(diff_times, diff_values[:, ch], linewidth=1.5)
    ax3.set_ylabel('Taxel Rate of Change')
    ax3.set_xlim([t_start, t_end])
    ax3.grid(True, alpha=0.3)
    ax3.set_xticklabels([])

    # Plot 4: RA-I
    ax4 = axes[3]
    if ra_mask.any():
        ax4.scatter(data['ra_times'][ra_mask], data['ra_channels'][ra_mask],
                    marker='|', s=100, c='red', linewidths=2)
    ax4.set_ylabel('RA-I Events')
    ax4.set_ylim([0, 13])
    ax4.set_xlim([t_start, t_end])
    ax4.grid(True, alpha=0.3)
    ax4.set_xticklabels([])

    # Plot 5: SA-II
    ax5 = axes[4]
    if sa_mask.any():
        ax5.scatter(data['sa_times'][sa_mask], data['sa_channels'][sa_mask],
                    marker='|', s=100, c='blue', linewidths=2)
    ax5.set_ylabel('SA-II Events')
    ax5.set_ylim([0, 13])
    ax5.set_xlim([t_start, t_end])
    ax5.set_xlabel('Time (ms)')
    ax5.grid(True, alpha=0.3)

    # Add title with window info and filename
    title = f"Time Window: {t_start:.0f} - {t_end:.0f} ms"
    if adjusted:
        title += f" (adjusted from {original_t_start:.0f} - {original_t_start + original_duration:.0f} ms)"
    if input_filename:
        title += f"\nFile: {input_filename}"
    fig.suptitle(title, fontsize=14, fontweight='bold')

    fig.align_ylabels()
    plt.tight_layout()
    plt.savefig(output_file, dpi=500, bbox_inches='tight')
    plt.savefig(output_file.split(".")[0] + ".pdf", bbox_inches='tight')
    print(f"Time window plot saved: {output_file}")
    plt.close()


def print_statistics(data):
    """Print statistical summary of the recorded data.

    Args:
        data (dict): Data dictionary from load_h5_data().
    """
    print(f"\n{'='*70}")
    print("Data Statistics")
    print(f"{'='*70}\n")

    # Taxel statistics
    print("Taxel Data:")
    print(f"  Samples: {len(data['taxel_times'])}")
    duration_ms = data['taxel_times'][-1] - data['taxel_times'][0]
    print(f"  Duration: {duration_ms:.2f} ms ({duration_ms/1000.0:.2f} sec)")
    
    taxel_dt = np.diff(data['taxel_times'])
    mean_taxel_dt = np.mean(taxel_dt)
    median_taxel_dt = np.median(taxel_dt)
    print(f"  Mean interval: {mean_taxel_dt:.2f} ms")
    print(f"  Median interval: {median_taxel_dt:.2f} ms")
    print(f"  Std dev interval: {np.std(taxel_dt):.2f} ms")
    print(f"  Min interval: {np.min(taxel_dt):.2f} ms")
    print(f"  Max interval: {np.max(taxel_dt):.2f} ms")
    print(f"  25th percentile: {np.percentile(taxel_dt, 25):.2f} ms")
    print(f"  75th percentile: {np.percentile(taxel_dt, 75):.2f} ms")
    print(f"  Sampling rate: {1000.0/median_taxel_dt:.2f} Hz (based on median)")
    
    print("  Per-Channel Statistics:")
    for ch in range(12):
        mean_val = np.mean(data['taxel_values'][:, ch])
        median_val = np.median(data['taxel_values'][:, ch])
        std_val = np.std(data['taxel_values'][:, ch])
        print(f"    Ch {ch:2d}: mean={mean_val:.1f}, median={median_val:.1f}, std={std_val:.1f}")

    # IMU statistics
    print("\nIMU Data:")
    print(f"  Samples: {len(data['imu_times'])}")
    
    imu_dt = np.diff(data['imu_times'])
    mean_imu_dt = np.mean(imu_dt)
    median_imu_dt = np.median(imu_dt)
    unique_intervals = len(np.unique(np.round(imu_dt, 2)))  # Round to 2 decimals for counting
    
    print(f"  Mean interval: {mean_imu_dt:.2f} ms")
    print(f"  Median interval: {median_imu_dt:.2f} ms")
    print(f"  Std dev interval: {np.std(imu_dt):.2f} ms")
    print(f"  Min interval: {np.min(imu_dt):.2f} ms")
    print(f"  Max interval: {np.max(imu_dt):.2f} ms")
    print(f"  25th percentile: {np.percentile(imu_dt, 25):.2f} ms")
    print(f"  75th percentile: {np.percentile(imu_dt, 75):.2f} ms")
    print(f"  Unique intervals: {unique_intervals} (precision: 0.01ms)")
    print(f"  Sampling rate: {1000.0/median_imu_dt:.2f} Hz (based on median)")
    
    print(
        f"  X: mean={np.mean(data['imu_x']):.3f}g, median={np.median(data['imu_x']):.3f}g, std={np.std(data['imu_x']):.3f}g")
    print(
        f"  Y: mean={np.mean(data['imu_y']):.3f}g, median={np.median(data['imu_y']):.3f}g, std={np.std(data['imu_y']):.3f}g")
    print(
        f"  Z: mean={np.mean(data['imu_z']):.3f}g, median={np.median(data['imu_z']):.3f}g, std={np.std(data['imu_z']):.3f}g")

    # Event statistics
    recording_duration_sec = (data['taxel_times'][-1] - data['taxel_times'][0]) / 1000.0
    
    print("\nRA-I Event Data:")
    print(f"  Total Events: {len(data['ra_times'])}")
    if len(data['ra_times']) > 0:
        overall_ra_rate = len(data['ra_times']) / recording_duration_sec
        print(f"  Overall Rate: {overall_ra_rate:.2f} events/sec")
        print(f"  Per-Channel Rates:")
        for ch in range(12):
            ch_events = np.sum(data['ra_channels'] == ch)
            if ch_events > 0:
                ch_rate = ch_events / recording_duration_sec
                # Calculate inter-spike interval stats for this channel
                ch_times = data['ra_times'][data['ra_channels'] == ch]
                if len(ch_times) > 1:
                    isi = np.diff(ch_times)
                    mean_isi = np.mean(isi)
                    median_isi = np.median(isi)
                    print(f"    Ch {ch:2d}: {ch_events:4d} events ({ch_rate:6.2f} evt/s, ISI: mean={mean_isi:.1f}ms, median={median_isi:.1f}ms)")
                else:
                    print(f"    Ch {ch:2d}: {ch_events:4d} events ({ch_rate:6.2f} evt/s)")

    print("\nSA-II Event Data:")
    print(f"  Total Events: {len(data['sa_times'])}")
    if len(data['sa_times']) > 0:
        overall_sa_rate = len(data['sa_times']) / recording_duration_sec
        print(f"  Overall Rate: {overall_sa_rate:.2f} events/sec")
        print(f"  Per-Channel Rates:")
        for ch in range(12):
            ch_events = np.sum(data['sa_channels'] == ch)
            if ch_events > 0:
                ch_rate = ch_events / recording_duration_sec
                # Calculate inter-spike interval stats for this channel
                ch_times = data['sa_times'][data['sa_channels'] == ch]
                if len(ch_times) > 1:
                    isi = np.diff(ch_times)
                    mean_isi = np.mean(isi)
                    median_isi = np.median(isi)
                    print(f"    Ch {ch:2d}: {ch_events:4d} events ({ch_rate:6.2f} evt/s, ISI: mean={mean_isi:.1f}ms, median={median_isi:.1f}ms)")
                else:
                    print(f"    Ch {ch:2d}: {ch_events:4d} events ({ch_rate:6.2f} evt/s)")

    print(f"\n{'='*70}\n")


def main():
    """Main function with command-line interface."""
    parser = argparse.ArgumentParser(
        description='Read and visualize Phalanx HDF5 sensor data files',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Plot full session
  python read_h5_data.py data.h5
  
  # Plot window from 1000ms with 2000ms duration (1000-3000ms)
  python read_h5_data.py data.h5 --window 1000 2000
  
  # Plot window from 1000ms to 3000ms
  python read_h5_data.py data.h5 --window-range 1000 3000
  
  # Only show statistics
  python read_h5_data.py data.h5 --stats-only
        """)

    parser.add_argument('filename', type=str, help='HDF5 data file to read')
    parser.add_argument('--window', type=float, nargs=2, metavar=('START_MS', 'DURATION_MS'),
                        help='Plot specific time window: START_MS DURATION_MS (e.g., 1000 2000 plots 1000-3000ms)')
    parser.add_argument('--window-range', type=float, nargs=2, metavar=('START_MS', 'END_MS'),
                        help='Plot specific time range: START_MS END_MS (e.g., 1000 3000 plots 1000-3000ms)')
    parser.add_argument('--stats-only', action='store_true',
                        help='Only print statistics, do not generate plots')
    parser.add_argument('--structure', action='store_true',
                        help='Print detailed HDF5 file structure')

    args = parser.parse_args()
    
    # Check for conflicting arguments
    if args.window and args.window_range:
        print("Error: Cannot use both --window and --window-range simultaneously")
        sys.exit(1)

    # Check if file exists
    if not Path(args.filename).exists():
        print(f"Error: File '{args.filename}' not found!")
        sys.exit(1)

    # Print structure if requested
    if args.structure:
        print_h5_structure(args.filename)

    # Load data
    print(f"Loading data from {args.filename}...")
    data = load_h5_data(args.filename)
    print("Data loaded successfully!")
    
    # Print data time range
    data_t_min = min(data['taxel_times'][0], data['imu_times'][0])
    data_t_max = max(data['taxel_times'][-1], data['imu_times'][-1])
    print(f"Data time range: {data_t_min:.2f} - {data_t_max:.2f} ms (duration: {data_t_max - data_t_min:.2f} ms)")

    # Print statistics
    print_statistics(data)

    # Extract base filename without path and extension
    input_basename = Path(args.filename).stem
    
    # Generate plots unless stats-only
    if not args.stats_only:
        if args.window:
            t_start, duration = args.window
            print(f"\n--window interpreted as: start={t_start:.0f}ms, duration={duration:.0f}ms")
            print(f"  This plots the range: {t_start:.0f} - {t_start + duration:.0f} ms")
            # Filename indicates it's duration-based
            output_file = f"{input_basename}_window_{int(t_start)}ms_dur{int(duration)}ms.png"
            plot_time_window(data, t_start=t_start, duration=duration, 
                           output_file=output_file, input_filename=args.filename)
        elif args.window_range:
            t_start, t_end = args.window_range
            duration = t_end - t_start
            print(f"\n--window-range interpreted as: start={t_start:.0f}ms, end={t_end:.0f}ms")
            print(f"  This plots the range: {t_start:.0f} - {t_end:.0f} ms (duration={duration:.0f}ms)")
            # Filename indicates it's range-based (start to end)
            output_file = f"{input_basename}_range_{int(t_start)}-{int(t_end)}ms.png"
            plot_time_window(data, t_start=t_start, duration=duration, 
                           output_file=output_file, input_filename=args.filename)
        else:
            # Full session plot
            output_full = f"{input_basename}_full_session.png"
            print("\nGenerating full session plot...")
            plot_full_session(data, output_file=output_full, input_filename=args.filename)
            
            # Also generate a 2-second window from the start (with validation)
            output_window = f"{input_basename}_window_{int(data_t_min)}ms_dur2000ms.png"
            print("\nGenerating 2-second window plot...")
            plot_time_window(data, t_start=data_t_min, duration=2000, 
                           output_file=output_window, input_filename=args.filename)

    print("\nDone!")


if __name__ == "__main__":
    main()
