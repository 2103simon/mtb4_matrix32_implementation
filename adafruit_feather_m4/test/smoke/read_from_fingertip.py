"""
Tactile sensor data analyzer and visualization tool.

This script connects to the Phalanx fingertip sensor via serial port,
collects raw sensor data including taxel pressures and neuromorphic events,
and generates visualization plots showing:
- Raw taxel pressure values over time
- SA-II (Slowly Adapting) spike events
- Taxel pressure derivatives (rate of change)
- RA-I (Rapidly Adapting) spike events

The tool provides timing analysis for data packet arrival rates and
generates both full-session and zoomed-in plots for detailed inspection.

Usage:
    python read_from_fingertip.py

Output:
    - taxel_readings.png: Full session visualization
    - taxel_readings_cut.png: 2-second zoomed window visualization
    - taxel_data_YYYYMMDD_HHMMSS.h5: HDF5 data file
    - Console timing statistics for IMU, taxel, and event data

Author: Simon F. Muller-Cleve
Date: 2026-03-30
"""
import struct
import time

import h5py as h5
import matplotlib.pyplot as plt
import numpy as np
import serial
from tqdm import tqdm

# This smoke script is nested in the Feather PlatformIO project.
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from communication.serial_ports import resolve_port

# sometimes permission denied on linux, use:
# sudo chmod 666 /dev/ttyACM0


def analyze_taxel_timing(port=None, baudrate=115200, duration_sec=10.0):
    """Analyze timing characteristics of sensor data packets from the MCU.

    Connects to the serial port and collects data for the specified duration,
    measuring inter-arrival times for IMU, taxel, and event packets. Also
    accumulates all sensor data for subsequent analysis and visualization.

    Args:
        port (str | None): Optional explicit Feather M4 serial device path.
            Omitted port selects the board using USB identity.
        baudrate (int, optional): Serial communication speed. Defaults to 115200.
        duration_sec (float, optional): Recording duration in seconds. Defaults to 10.0.

    Returns:
        tuple: (taxel_data, imu_data, ev_data) where:
            - taxel_data (np.ndarray): Array of shape (N, 13) containing
              [timestamp, taxel0, taxel1, ..., taxel11] for each sample.
            - imu_data (np.ndarray): Array of shape (M, 4) containing
              [timestamp, X, Y, Z] for each IMU sample.
            - ev_data (np.ndarray): Array of shape (K, 3) containing
              [ID0, ID1, timestamp] for each neuromorphic event.

    Raises:
        serial.SerialException: If unable to connect to the specified port.
        KeyboardInterrupt: If user stops the collection with Ctrl+C.

    Example:
        >>> taxel_data, imu_data, events = analyze_taxel_timing('/dev/ttyACM0', duration_sec=5.0)
        >>> print(f"Collected {len(taxel_data)} taxel samples in 5 seconds")
    """
    imu_struct = struct.Struct('fff')  # For unpacking IMU data

    port = resolve_port(port, "feather")
    try:
        with serial.Serial(port, baudrate, timeout=1) as ser:
            print(f"Connected to {port} at {baudrate} baud.")
            print(f"Recording for {duration_sec:.1f} seconds...")

            taxel = [0 for _ in range(12)]  # Placeholder for taxel data
            taxel_data = []
            ev_data = []
            imu_data = []
            ev_intervals = []
            imu_intervals = []
            taxel_intervals = []
            last_ev_time = time.time()
            last_imu_time = time.time()
            last_taxel_time = time.time()

            # Start timing
            start_time = time.time()
            end_time = start_time + duration_sec
            
            # Progress bar based on time with 2 decimal places
            with tqdm(total=duration_sec, desc="Recording", unit="s", 
                     bar_format='{l_bar}{bar}| {n:.2f}/{total:.2f}s [{elapsed}<{remaining}]') as pbar:
                last_update = start_time
                
                while time.time() < end_time:
                    # Update progress bar
                    current_time = time.time()
                    elapsed = current_time - last_update
                    if elapsed >= 0.1:  # Update every 100ms
                        pbar.update(elapsed)
                        last_update = current_time
                    
                    # Check for data
                    if ser.in_waiting == 0:
                        time.sleep(0.0001)  # Brief sleep to avoid busy-waiting
                        continue

                    bs = ser.read(1)
                    if bs == b'A':  # IMU data
                        time_bytes = ser.read(4)
                        imu_time = struct.unpack('>I', time_bytes)[0]
                        imu_bytes = ser.read(12)
                        X, Y, Z = imu_struct.unpack(imu_bytes)
                        combined_data = [imu_time] + [X, Y, Z]
                        imu_data.append(combined_data)

                        current_time = time.time()
                        imu_interval = current_time - last_imu_time
                        imu_intervals.append(imu_interval)
                        last_imu_time = current_time

                    elif bs == b'C':  # Taxel data
                        time_bytes = ser.read(4)
                        taxel_time = struct.unpack('>I', time_bytes)[0]
                        taxel_bytes = ser.read(12)
                        taxel = list(taxel_bytes)

                        combined_data = [taxel_time] + taxel
                        taxel_data.append(combined_data)

                        current_time = time.time()
                        taxel_interval = current_time - last_taxel_time
                        taxel_intervals.append(taxel_interval)
                        last_taxel_time = current_time

                    elif bs == b'E':  # event data
                        nb_events_byte = ser.read(1)
                        nb_events = list(nb_events_byte)[0]
                        bytes_to_read = nb_events * 6
                        ev_bytes = ser.read(bytes_to_read)

                        for i in range(nb_events):
                            offset = i * 6
                            ID0 = ev_bytes[offset]
                            ID1 = ev_bytes[offset + 1]
                            time_bytes = ev_bytes[offset + 2:offset + 6]
                            ev_time = struct.unpack('>I', time_bytes)[0]
                            ev_data.append((ID0, ID1, ev_time))

                        current_time = time.time()
                        ev_interval = current_time - last_ev_time
                        ev_intervals.append(ev_interval)
                        last_ev_time = current_time
                
                # Final update to complete progress bar
                pbar.update(end_time - last_update)

            # Print timing statistics
            print(f"\nRecorded {len(taxel_data)} taxel samples, {len(imu_data)} IMU samples, {len(ev_data)} events")
            
            if ev_intervals:
                avg_ev_interval = sum(ev_intervals) / len(ev_intervals)
                min_ev_interval = min(ev_intervals)
                max_ev_interval = max(ev_intervals)
                print("\nEvent Timing Analysis:")
                print(f"  Average Interval: {avg_ev_interval:.6f} seconds")
                print(f"  Minimum Interval: {min_ev_interval:.6f} seconds")
                print(f"  Maximum Interval: {max_ev_interval:.6f} seconds")

            if imu_intervals:
                avg_imu_interval = sum(imu_intervals) / len(imu_intervals)
                min_imu_interval = min(imu_intervals)
                max_imu_interval = max(imu_intervals)
                print("\nIMU Timing Analysis:")
                print(f"  Average Interval: {avg_imu_interval:.6f} seconds")
                print(f"  Minimum Interval: {min_imu_interval:.6f} seconds")
                print(f"  Maximum Interval: {max_imu_interval:.6f} seconds")

            if taxel_intervals:
                avg_taxel_interval = sum(taxel_intervals) / len(taxel_intervals)
                min_taxel_interval = min(taxel_intervals)
                max_taxel_interval = max(taxel_intervals)
                print("\nTaxel Timing Analysis:")
                print(f"  Average Interval: {avg_taxel_interval:.6f} seconds")
                print(f"  Minimum Interval: {min_taxel_interval:.6f} seconds")
                print(f"  Maximum Interval: {max_taxel_interval:.6f} seconds")

    except serial.SerialException as e:
        print(f"Error: {e}")
    except KeyboardInterrupt:
        print("\nStopped timing analysis.")
    except Exception as e:
        print(f"Unexpected error: {e}")

    return np.array(taxel_data), np.array(imu_data), np.array(ev_data)


def post_process_data(taxel_data, imu_data, ev_data):
    # Split event data into RA-I, SA-II, and US encodings
    ra_data = ev_data[ev_data[:, 0] == 0]
    sa_data = ev_data[ev_data[:, 0] == 1]
    us_data = ev_data[ev_data[:, 0] == 2]

    # Rebase time to start at 0
    t_first_sample = taxel_data[0, 0] 
    taxel_data[:, 0] = taxel_data[:, 0] - t_first_sample
    imu_data[:, 0] = imu_data[:, 0] - t_first_sample
    sa_data[:, -1] = sa_data[:, -1] - t_first_sample
    ra_data[:, -1] = ra_data[:, -1] - t_first_sample
    us_data[:, -1] = us_data[:, -1] - t_first_sample
    
    # Remove any data with t < 0 (data that arrived before first taxel sample)
    imu_data = imu_data[imu_data[:, 0] >= 0]
    ra_data = ra_data[ra_data[:, -1] >= 0]
    sa_data = sa_data[sa_data[:, -1] >= 0]
    us_data = us_data[us_data[:, -1] >= 0]

    return taxel_data, imu_data, ra_data, sa_data, us_data


def plot_results(taxel_data, ra_data, sa_data, us_data, timestamp_str=None):
    """Generate visualization plots of sensor data.
    
    Args:
        taxel_data (np.ndarray): Taxel pressure data array.
        ra_data (np.ndarray): RA-I event data array.
        sa_data (np.ndarray): SA-II event data array.
        us_data (np.ndarray): US event data array.
        timestamp_str (str, optional): Timestamp string for filename. Defaults to current time.
    """
    if timestamp_str is None:
        timestamp_str = time.strftime('%Y%m%d_%H%M%S')

    ra_data[:, 1] += 1  # Shift channel ID for plotting
    sa_data[:, 1] += 1  # Shift channel ID for plotting
    
    # Generate full session plot
    fig = plt.figure()
    ax1 = fig.add_subplot(5, 1, 1)
    ax1.plot(taxel_data[:, 0], taxel_data[:, 1:])
    ax1.set_ylabel("Taxel")
    ax1.set_ylim([0, 256])
    ax1.set_xlim([taxel_data[0, 0], taxel_data[-1, 0]])
    ax1.set_xticklabels([])

    ax2 = fig.add_subplot(5, 1, 2)
    ax2.scatter(sa_data[:, -1], sa_data[:, 1], marker='|')
    ax2.set_ylabel("SA-II")
    ax2.set_xlim([taxel_data[0, 0], taxel_data[-1, 0]])
    ax2.set_xticklabels([])

    ax3 = fig.add_subplot(5, 1, 3)
    ax3.plot(taxel_data[1:, 0], np.diff(taxel_data[:, 1:], axis=0))
    ax3.set_ylabel("diff Taxel")
    ax3.set_xlim([taxel_data[0, 0], taxel_data[-1, 0]])
    ax3.set_xticklabels([])

    ax4 = fig.add_subplot(5, 1, 4)
    ax4.scatter(ra_data[:, -1], ra_data[:, 1], marker='|')
    ax4.set_xlim([taxel_data[0, 0], taxel_data[-1, 0]])
    ax4.set_ylabel("RA-I")
    ax4.set_xlabel("Time [ms]")

    ax5 = fig.add_subplot(5, 1, 5)
    ax5.scatter(us_data[:, -1], us_data[:, 1], marker='|')
    ax5.set_xlim([taxel_data[0, 0], taxel_data[-1, 0]])
    ax5.set_ylabel("US")
    ax5.set_xlabel("Time [ms]")

    fig.align_ylabels()
    full_png = f"./taxel_readings_{timestamp_str}.png"
    full_pdf = f"./taxel_readings_{timestamp_str}.pdf"
    plt.savefig(full_png, dpi=500)
    plt.savefig(full_pdf)
    plt.close(fig)

    # Generate zoomed-in plot (2-second window)
    time_frame = 2000
    t_start = taxel_data[0, 0]
    t_stop = t_start + time_frame

    taxel_start = np.where(taxel_data[:, 0] > t_start)[0][0]
    taxel_stop = np.where(taxel_data[:, 0] < t_stop)[0][-1]

    sa_start = np.where(sa_data[:, -1] > t_start)[0][0]
    sa_stop = np.where(sa_data[:, -1] < t_stop)[0][-1]

    ra_start = np.where(ra_data[:, -1] > t_start)[0][0]
    ra_stop = np.where(ra_data[:, -1] < t_stop)[0][-1]

    us_start = np.where(us_data[:, -1] > t_start)[0][0]
    us_stop = np.where(us_data[:, -1] < t_stop)[0][-1]

    fig_cut = plt.figure()

    ax1 = fig_cut.add_subplot(5, 1, 1)
    ax1.plot(taxel_data[taxel_start:taxel_stop, 0],
             taxel_data[taxel_start:taxel_stop, 1:])
    ax1.set_ylabel("Taxel")
    ax1.set_ylim([0, 256])
    ax1.set_xlim([taxel_data[taxel_start, 0], taxel_data[taxel_stop, 0]])
    ax1.set_xticklabels([])

    ax2 = fig_cut.add_subplot(5, 1, 2)
    ax2.scatter(sa_data[sa_start:sa_stop, -1],
                sa_data[sa_start:sa_stop, 1], marker='|')
    ax2.set_ylabel("SA-II")
    ax2.set_xlim([taxel_data[taxel_start, 0], taxel_data[taxel_stop, 0]])
    ax2.set_xticklabels([])

    ax3 = fig_cut.add_subplot(5, 1, 3)
    ax3.plot(taxel_data[taxel_start+1:taxel_stop, 0],
             np.diff(taxel_data[taxel_start:taxel_stop, 1:], axis=0))
    ax3.set_ylabel("diff Taxel")
    ax3.set_xlim([taxel_data[taxel_start, 0], taxel_data[taxel_stop, 0]])
    ax3.set_xticklabels([])

    ax4 = fig_cut.add_subplot(5, 1, 4)
    ax4.scatter(ra_data[ra_start:ra_stop, -1],
                ra_data[ra_start:ra_stop, 1], marker='|')
    ax4.set_ylabel("RA-I")
    ax4.set_xlim([taxel_data[taxel_start, 0], taxel_data[taxel_stop, 0]])
    ax4.set_xlabel("Time [ms]")

    ax5 = fig_cut.add_subplot(5, 1, 5)
    ax5.scatter(us_data[us_start:us_stop, -1],
                us_data[us_start:us_stop, 1], marker='|')
    ax5.set_ylabel("US")
    ax5.set_xlim([taxel_data[taxel_start, 0], taxel_data[taxel_stop, 0]])
    ax5.set_xlabel("Time [ms]")

    fig_cut.align_ylabels()
    cut_png = f"./taxel_readings_{timestamp_str}_cut.png"
    cut_pdf = f"./taxel_readings_{timestamp_str}_cut.pdf"
    plt.savefig(cut_png, dpi=500)
    plt.savefig(cut_pdf)
    plt.close(fig_cut)

    print("\nPlots saved:")
    print(f"  - {full_png} (full session)")
    print(f"  - {cut_png} (2-second window)")


def save_data(taxel_data, imu_data, ra_data, sa_data, us_data, port, baudrate, ev_data):
    """Save collected data to HDF5 file.
    
    Args:
        taxel_data (np.ndarray): Taxel pressure data array.
        imu_data (np.ndarray): IMU acceleration data array.
        ra_data (np.ndarray): RA-I event data array.
        sa_data (np.ndarray): SA-II event data array.
        us_data (np.ndarray): US event data array.
        port (str): Serial port used for data collection.
        baudrate (int): Baudrate used for serial communication.
        ev_data (np.ndarray): Raw event data array.
        
    Returns:
        str: Timestamp string used in filename.
    """
    # Generate timestamp for consistent naming
    timestamp_str = time.strftime('%Y%m%d_%H%M%S')
    h5_filename = f"./taxel_data_{timestamp_str}.h5"

    print(f"\nSaving data to HDF5: {h5_filename}")
    
    # Calculate statistics
    mean_taxel_time = np.mean(np.diff(taxel_data[:, 0]))
    mean_imu_time = np.mean(np.diff(imu_data[:, 0]))

    with h5.File(h5_filename, 'w') as hf:
        # Create groups for organized data storage
        taxel_group = hf.create_group('taxel_data')
        imu_group = hf.create_group('imu_data')
        events_group = hf.create_group('events')

        # Store taxel data
        taxel_group.create_dataset('timestamps', data=taxel_data[:, 0],
                                   compression='gzip', compression_opts=9)
        taxel_group.create_dataset('values', data=taxel_data[:, 1:],
                                   compression='gzip', compression_opts=9)
        taxel_group.attrs['num_channels'] = 12
        taxel_group.attrs['sampling_info'] = f"Mean interval: {mean_taxel_time:.2f} ms"

        # Store IMU data
        imu_group.create_dataset('timestamps', data=imu_data[:, 0],
                                 compression='gzip', compression_opts=9)
        imu_group.create_dataset('acceleration_x', data=imu_data[:, 1],
                                 compression='gzip', compression_opts=9)
        imu_group.create_dataset('acceleration_y', data=imu_data[:, 2],
                                 compression='gzip', compression_opts=9)
        imu_group.create_dataset('acceleration_z', data=imu_data[:, 3],
                                 compression='gzip', compression_opts=9)
        imu_group.attrs['units'] = 'g (gravity)'
        imu_group.attrs['sampling_info'] = f"Mean interval: {mean_imu_time:.2f} ms"

        # Store RA-I events
        ra_group = events_group.create_group('ra_i')
        if len(ra_data) > 0:
            ra_group.create_dataset('channel_id', data=ra_data[:, 1].astype(int),
                                    compression='gzip', compression_opts=9)
            ra_group.create_dataset('timestamps', data=ra_data[:, 2],
                                    compression='gzip', compression_opts=9)
            ra_group.attrs['total_events'] = len(ra_data)
            ra_group.attrs['description'] = 'Rapidly Adapting (RA-I) mechanoreceptor events'
        else:
            ra_group.attrs['total_events'] = 0
            ra_group.attrs['description'] = 'No RA-I events recorded'

        # Store SA-II events
        sa_group = events_group.create_group('sa_ii')
        if len(sa_data) > 0:
            sa_group.create_dataset('channel_id', data=sa_data[:, 1].astype(int),
                                    compression='gzip', compression_opts=9)
            sa_group.create_dataset('timestamps', data=sa_data[:, 2],
                                    compression='gzip', compression_opts=9)
            sa_group.attrs['total_events'] = len(sa_data)
            sa_group.attrs['description'] = 'Slowly Adapting (SA-II) mechanoreceptor events'
        else:
            sa_group.attrs['total_events'] = 0
            sa_group.attrs['description'] = 'No SA-II events recorded'

        # Store US events
        us_group = events_group.create_group('us')
        if len(us_data) > 0:
            us_group.create_dataset('channel_id', data=us_data[:, 1].astype(int),
                                    compression='gzip', compression_opts=9)
            us_group.create_dataset('timestamps', data=us_data[:, 2],
                                    compression='gzip', compression_opts=9)
            us_group.attrs['total_events'] = len(us_data)
            us_group.attrs['description'] = 'Unified Stimulation (US) events'
        else:
            us_group.attrs['total_events'] = 0
            us_group.attrs['description'] = 'No US events recorded'

        # Store metadata
        hf.attrs['acquisition_date'] = time.strftime('%Y-%m-%d %H:%M:%S')
        hf.attrs['serial_port'] = port
        hf.attrs['baudrate'] = baudrate
        hf.attrs['sample_count'] = len(taxel_data)
        hf.attrs['total_taxel_samples'] = len(taxel_data)
        hf.attrs['total_imu_samples'] = len(imu_data)
        hf.attrs['total_events'] = len(ev_data)
        hf.attrs['description'] = 'Phalanx fingertip sensor data - tactile pressures and neuromorphic events'
        hf.attrs['author'] = 'Simon F. Muller-Cleve'

    print(f"Data successfully saved to {h5_filename}")
    print(f"\nHDF5 file structure:")
    print(f"  /taxel_data/")
    print(f"    - timestamps: {len(taxel_data)} samples")
    print(f"    - values: {taxel_data[:, 1:].shape} (12 channels)")
    print(f"  /imu_data/")
    print(f"    - timestamps: {len(imu_data)} samples")
    print(f"    - acceleration_x/y/z: 3-axis data")
    print(f"  /events/")
    print(f"    - ra_i/: {len(ra_data)} events")
    print(f"    - sa_ii/: {len(sa_data)} events")
    print(f"    - us/: {len(us_data)} events")
    return timestamp_str


def report_statistics(taxel_data, imu_data, ra_data=None, sa_data=None, us_data=None):
    # Taxel packet timing analysis
    taxel_dt = np.diff(taxel_data[:, 0])
    mean_taxel_time = np.mean(taxel_dt)
    median_taxel_time = np.median(taxel_dt)
    std_taxel_time = np.std(taxel_dt)
    min_taxel_time = np.min(taxel_dt)
    max_taxel_time = np.max(taxel_dt)
    p25_taxel = np.percentile(taxel_dt, 25)
    p75_taxel = np.percentile(taxel_dt, 75)
    unique_taxel_intervals = len(np.unique(np.round(taxel_dt, 2)))  # Round to 2 decimals
    
    print(f"\nTaxel Packet Timing Analysis:")
    print(f"  Total Samples: {len(taxel_data)}")
    print(f"  Mean Interval: {mean_taxel_time:.2f} ms")
    print(f"  Median Interval: {median_taxel_time:.2f} ms")
    print(f"  Std Dev Interval: {std_taxel_time:.2f} ms")
    print(f"  Min Interval: {min_taxel_time:.2f} ms")
    print(f"  Max Interval: {max_taxel_time:.2f} ms")
    print(f"  25th Percentile: {p25_taxel:.2f} ms")
    print(f"  75th Percentile: {p75_taxel:.2f} ms")
    print(f"  Unique Intervals: {unique_taxel_intervals} (precision: 0.01ms)")
    print(f"  Sampling Rate: {1000.0/median_taxel_time:.2f} Hz (based on median)")

    # IMU packet timing analysis
    imu_dt = np.diff(imu_data[:, 0])
    mean_imu_time = np.mean(imu_dt)
    median_imu_time = np.median(imu_dt)
    std_imu_time = np.std(imu_dt)
    min_imu_time = np.min(imu_dt)
    max_imu_time = np.max(imu_dt)
    p25_imu = np.percentile(imu_dt, 25)
    p75_imu = np.percentile(imu_dt, 75)
    unique_imu_intervals = len(np.unique(np.round(imu_dt, 2)))  # Round to 2 decimals
    
    print(f"\nIMU Packet Timing Analysis:")
    print(f"  Total Samples: {len(imu_data)}")
    print(f"  Mean Interval: {mean_imu_time:.2f} ms")
    print(f"  Median Interval: {median_imu_time:.2f} ms")
    print(f"  Std Dev Interval: {std_imu_time:.2f} ms")
    print(f"  Min Interval: {min_imu_time:.2f} ms")
    print(f"  Max Interval: {max_imu_time:.2f} ms")
    print(f"  25th Percentile: {p25_imu:.2f} ms")
    print(f"  75th Percentile: {p75_imu:.2f} ms")
    print(f"  Unique Intervals: {unique_imu_intervals} (precision: 0.01ms)")
    print(f"  Sampling Rate: {1000.0/median_imu_time:.2f} Hz (based on median)")
    
    # Per-channel event rate analysis
    recording_duration_sec = (taxel_data[-1, 0] - taxel_data[0, 0]) / 1000.0
    if ra_data is not None and len(ra_data) > 0:
        print(f"\nRA-I Event Analysis (duration: {recording_duration_sec:.2f}s):")
        print(f"  Total Events: {len(ra_data)}")
        print(f"  Overall Rate: {len(ra_data) / recording_duration_sec:.2f} events/sec")
        print(f"  Per-Channel Rates:")
        for ch in range(12):
            ch_events = np.sum(ra_data[:, 1] == ch)
            if ch_events > 0:
                ch_rate = ch_events / recording_duration_sec
                print(f"    Channel {ch:2d}: {ch_events:4d} events ({ch_rate:6.2f} events/sec)")
    
    if sa_data is not None and len(sa_data) > 0:
        print(f"\nSA-II Event Analysis (duration: {recording_duration_sec:.2f}s):")
        print(f"  Total Events: {len(sa_data)}")
        print(f"  Overall Rate: {len(sa_data) / recording_duration_sec:.2f} events/sec")
        print(f"  Per-Channel Rates:")
        for ch in range(12):
            ch_events = np.sum(sa_data[:, 1] == ch)
            if ch_events > 0:
                ch_rate = ch_events / recording_duration_sec
                print(f"    Channel {ch:2d}: {ch_events:4d} events ({ch_rate:6.2f} events/sec)")
    
    if us_data is not None and len(us_data) > 0:
        print(f"\nUS Event Analysis (duration: {recording_duration_sec:.2f}s):")
        print(f"  Total Events: {len(us_data)}")
        print(f"  Overall Rate: {len(us_data) / recording_duration_sec:.2f} events/sec")
        print(f"  Per-Channel Rates:")
        for ch in range(12):
            ch_events = np.sum(us_data[:, 1] == ch)
            if ch_events > 0:
                ch_rate = ch_events / recording_duration_sec
                print(f"    Channel {ch:2d}: {ch_events:4d} events ({ch_rate:6.2f} events/sec)")


if __name__ == "__main__":
    port = resolve_port(None, "feather")
    baudrate = 115200
    duration_sec = 10.0  # Record for 10 seconds by default

    taxel_data, imu_data, ev_data = analyze_taxel_timing(
        port, baudrate, duration_sec)

    taxel_data, imu_data, ra_data, sa_data, us_data = post_process_data(taxel_data, imu_data, ev_data)
    
    report_statistics(taxel_data, imu_data, ra_data, sa_data, us_data)

    # Save data first to get consistent timestamp
    timestamp_str = save_data(taxel_data, imu_data, ra_data, sa_data, us_data, port, baudrate, ev_data)
    
    # Use same timestamp for plots
    plot_results(taxel_data, ra_data, sa_data, us_data, timestamp_str)