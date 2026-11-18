import csv
from pathlib import Path
from datetime import datetime


class DataRecorder:

    def __init__(self, recording_dir="recordings"):

        self.recording_dir = Path(recording_dir)
        self.recording_dir.mkdir(parents=True, exist_ok=True)

        self.is_recording = False

        self.taxel_file = None
        self.event_file = None
        self.rates_file = None

        self.taxel_writer = None
        self.event_writer = None
        self.rates_writer = None

    def start(self, timestamp=None):

        if self.is_recording:
            return

        timestamp = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")

        taxel_path = self.recording_dir / f"{timestamp}_taxel.csv"
        event_path = self.recording_dir / f"{timestamp}_events.csv"
        rates_path = self.recording_dir / f"{timestamp}_tactile_rates.csv"

        self.taxel_file = open(
            taxel_path, "w", newline=""
        )

        self.event_file = open(
            event_path, "w", newline=""
        )

        self.taxel_writer = csv.writer(self.taxel_file)
        self.event_writer = csv.writer(self.event_file)
        self.rates_file = open(rates_path, "w", newline="")
        self.rates_writer = csv.writer(self.rates_file)

        # CSV headers
        self.taxel_writer.writerow(
            ["corrected_timestamp_ms"] +
            ["raw_timestamp_ms", "pc_arrival_monotonic_ns"] +
            [f"taxel_{i}" for i in range(12)]
        )

        self.event_writer.writerow([
            "raw_timestamp_ms",
            "corrected_timestamp_ms",
            "id0",
            "id1",
            "pc_arrival_monotonic_ns"
        ])

        self.rates_writer.writerow(
            ["corrected_timestamp_ms", "raw_timestamp_ms", "pc_arrival_monotonic_ns"]
            + [f"taxel_{i}" for i in range(12)]
            + [f"ra_rate_hz_{i}" for i in range(12)]
            + [f"sa_rate_hz_{i}" for i in range(12)]
        )

        self.is_recording = True

        print("Recording started.")

    def record_taxel(self, values, corrected_timestamp, raw_timestamp, pc_arrival_monotonic_ns):

        if not self.is_recording:
            return

        self.taxel_writer.writerow(
            [corrected_timestamp, raw_timestamp, pc_arrival_monotonic_ns] + list(values)
        )

    def record_tactile_rates(self, snapshot):
        """Save one atomic MCU rate snapshot per CSV row."""
        if not self.is_recording:
            return
        self.rates_writer.writerow([
            snapshot["corrected_timestamp_ms"],
            snapshot["raw_timestamp_ms"],
            snapshot["pc_arrival_monotonic_ns"],
            *snapshot["taxels"],
            *snapshot["ra_rates_hz"],
            *snapshot["sa_rates_hz"],
        ])

    def record_events(self, events, pc_arrival_monotonic_ns):

        if not self.is_recording:
            return

        self.event_writer.writerows([*event, pc_arrival_monotonic_ns] for event in events)

    def stop(self):

        if not self.is_recording:
            return

        self.is_recording = False

        if self.taxel_file:
            self.taxel_file.close()

        if self.event_file:
            self.event_file.close()

        if self.rates_file:
            self.rates_file.close()

        self.taxel_file = None
        self.event_file = None
        self.rates_file = None
        self.taxel_writer = None
        self.event_writer = None
        self.rates_writer = None

        print("Recording stopped.")