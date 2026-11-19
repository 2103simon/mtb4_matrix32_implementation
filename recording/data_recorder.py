import csv
import json
from pathlib import Path
from datetime import datetime


class DataRecorder:

    def __init__(self, recording_dir="recordings"):

        self.recording_dir = Path(recording_dir)
        self.recording_dir.mkdir(parents=True, exist_ok=True)

        self.is_recording = False
        self.recording_id = None

        self.taxel_file = None
        self.event_file = None
        self.rates_file = None

        self.taxel_writer = None
        self.event_writer = None
        self.rates_writer = None

    @staticmethod
    def make_recording_id(config, timestamp=None):
        """Readable run ID; exact experiment settings go in the JSON manifest."""
        timestamp = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
        encoding = config["tactile_encoding"]
        source = config["stimulation_source"]
        ra = encoding["raThreshold"]
        sa = encoding["saMaxFrequencyHz"]
        taxel = source["taxel"]
        receptor = source["encoding"].replace("_", "")
        return f"{timestamp}_ra{ra}_sa{sa}Hz_t{taxel}_{receptor}"

    def start(self, timestamp=None, config=None, applied_settings=None):
        """Start a run after MCU ACKs and write a frozen configuration manifest.

        The timestamp argument is the complete filename prefix / run ID.
        Without config, preserve the legacy timestamp-only file naming.
        """
        if self.is_recording:
            return

        if config is not None:
            timestamp = timestamp or self.make_recording_id(config)
        else:
            timestamp = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")

        # Do not silently overwrite a recording with the same run ID.
        for suffix in ("_taxel.csv", "_events.csv", "_tactile_rates.csv", "_settings.json"):
            if (self.recording_dir / f"{timestamp}{suffix}").exists():
                raise FileExistsError(f"Recording already exists: {timestamp}{suffix}")

        self.recording_id = timestamp

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

        if config is not None:
            manifest = {
                "schema_version": 1,
                "recording_id": timestamp,
                "recording_start_local_iso": datetime.now().astimezone().isoformat(),
                "telemetry_format": "MCU tactile rates 0x05 (12 taxels, RA-I and SA-II Hz)",
                "mcu_configuration_confirmed": applied_settings,
                "configuration": config,
                "files": {
                    "tactile_rates": f"{timestamp}_tactile_rates.csv",
                    "taxel_legacy": f"{timestamp}_taxel.csv",
                    "events_legacy": f"{timestamp}_events.csv",
                    **({
                        "matrix32_commands": f"{timestamp}_matrix32_commands.jsonl",
                    } if config.get("stimulation_control", {}).get("record")
                    else {}),
                },
            }
            settings_path = self.recording_dir / f"{timestamp}_settings.json"
            with settings_path.open("x", encoding="utf-8") as stream:
                json.dump(manifest, stream, indent=2, ensure_ascii=False)
                stream.write("\n")

        self.is_recording = True

        print(f"Recording started: {timestamp}")

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