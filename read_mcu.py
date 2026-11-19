import argparse
import os
import json
import queue
import time
from pathlib import Path
from datetime import datetime

from communication.serial_manager import SerialManager
from communication.mcu_configuration import McuConfigurationHandshake, McuConfigurationError

from processing.taxel_processing import TaxelProcessor
from processing.event_processing import EventProcessor
from processing.data_analysis import DataAnalysis
from recording.data_recorder import DataRecorder

# Project root directory
BASE_DIR = Path(__file__).resolve().parent

# Configuration file
CONFIG_PATH = BASE_DIR / "config" / "config.json"


def main():
    parser = argparse.ArgumentParser(description="Receive and record MCU tactile snapshots")
    parser.add_argument("--print-every", type=int, default=10,
                        help="Print every Nth rate snapshot (default 10; 1 = all; 0 = none)")
    args = parser.parse_args()
    if args.print_every < 0:
        parser.error("--print-every must be non-negative")
    rate_samples = 0

    # ----------------------------------
    # 1. Load configuration
    # ----------------------------------

    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)

    # Validate the fixed encoding parameters and the future CH1 signal source.
    encoding_settings = config["tactile_encoding"]
    ra_threshold = encoding_settings["raThreshold"]
    sa_max_hz = encoding_settings["saMaxFrequencyHz"]
    source = config["stimulation_source"]
    taxel_id = source["taxel"]
    rate_encoding = source["encoding"]
    if type(ra_threshold) is not int or not 1 <= ra_threshold <= 255:
        raise ValueError("tactile_encoding.raThreshold must be an integer 1..255")
    if type(sa_max_hz) is not int or not 10 <= sa_max_hz <= 1000:
        raise ValueError("tactile_encoding.saMaxFrequencyHz must be an integer 10..1000")
    if type(taxel_id) is not int or not 0 <= taxel_id < 12:
        raise ValueError("stimulation_source.taxel must be an integer 0..11")
    if rate_encoding not in ("ra_i", "sa_ii"):
        raise ValueError("stimulation_source.encoding must be 'ra_i' or 'sa_ii'")

    print(
        f"Configuration loaded. RA threshold={ra_threshold}, "
        f"SA max={sa_max_hz} Hz, selected taxel={taxel_id}, "
        f"selected encoding={rate_encoding}"
    )

    # ----------------------------------
    # 2. Initialize communication
    # ----------------------------------

    sm = SerialManager(config=config)
    handshake = McuConfigurationHandshake(ra_threshold, sa_max_hz)
    recording_started = False

    # ----------------------------------
    # 3. Initialize processing modules
    # ----------------------------------

    taxel_processor = TaxelProcessor(
        max_history=1000
    )

    event_processor = EventProcessor(
        max_history=1000
    )

    analysis = DataAnalysis()

    # let's first create the data dir
    os.makedirs(config["recordings_dir"], exist_ok=True)
    recorder = DataRecorder(
        recording_dir=config["recordings_dir"]
    )

    # ----------------------------------
    # 4. Start serial communication
    # ----------------------------------

    port = config["serial"]["port"]

    sm.start_comm(port)

    if not sm.ser.is_open:
        print("Could not open serial port.")
        return

    print(f"Connected to MCU on {port}")

    # Wait for both parameter ACKs before creating recording files.
    # Any tactile data received during configuration is intentionally dropped.
    print("Waiting for first MCU telemetry before sending encoding settings...")
    link_deadline = time.monotonic() + 15.0

    # ----------------------------------
    # 5. Main processing loop
    # ----------------------------------

    try:

        while sm.is_running() or not sm.data_queue.empty():
            if not handshake.started and time.monotonic() >= link_deadline:
                raise McuConfigurationError(
                    "Timed out waiting for MCU telemetry; no recording started"
                )
            handshake.check_timeout()

            try:
                data = sm.data_queue.get(timeout=0.1)
                data_type = data[0]

                # Do not record any data before successful ACKs of both
                # encoding settings. SerialManager preserves packet order in
                # its queue, so samples before the final ACK are discarded.
                if data_type == "link_ready":
                    if not handshake.started:
                        print("MCU telemetry received. Applying encoding settings...")
                        handshake.start(sm)
                        print("Commands sent; awaiting RA_THR and SA_FREQ ACKs.")
                    continue

                if data_type == "ack":
                    command_id, status, value = data[1:4]
                    print(
                        f"[ACK] cmd=0x{command_id:02X}, status={status}, value={value}"
                    )
                    if handshake.accept_ack(command_id, status, value):
                        if not recording_started:
                            applied_settings = {
                                "raThreshold": handshake.expected[0x01],
                                "saMaxFrequencyHz": handshake.expected[0x03],
                                "acknowledged": True,
                            }
                            recorder.start(
                                config=config,
                                applied_settings=applied_settings,
                            )
                            recording_started = True
                            print("Both MCU settings confirmed; recording started.")
                            print(f"Experiment settings: {recorder.recording_id}_settings.json")
                    continue

                if not recording_started:
                    continue

                if data_type == "tactile_rates":
                    snapshot = data[1]
                    rate_samples += 1
                    recorder.record_tactile_rates(snapshot)
                    if args.print_every and rate_samples % args.print_every == 0:
                        selected_rate_key = ("ra_rates_hz" if rate_encoding == "ra_i"
                                             else "sa_rates_hz")
                        selected_rate = snapshot[selected_rate_key][taxel_id]
                        print(
                            f"[RATES #{rate_samples}] selected taxel={taxel_id} "
                            f"{rate_encoding}={selected_rate:.2f} Hz | "
                            f"MCU={snapshot['raw_timestamp_ms']} ms "
                            f"taxels={snapshot['taxels']} "
                            f"RA(Hz)={[round(x, 2) for x in snapshot['ra_rates_hz']]} "
                            f"SA(Hz)={[round(x, 2) for x in snapshot['sa_rates_hz']]}"
                        )

                elif data_type == "taxel":

                    # read taxel data: taxel_values, corrected_timestamp_ms, raw_timestamp_ms
                    values = data[1]
                    corrected_timestamp = data[2]
                    raw_timestamp = data[3]

                    # process taxel measurements
                    sample = taxel_processor.process(
                        values,
                        corrected_timestamp,
                        raw_timestamp
                    )

                    # Record taxel measurements
                    recorder.record_taxel(values, corrected_timestamp, raw_timestamp, data[4])

                    analysis.analyze_taxel(sample)

                elif data_type == "events":

                    # read event data: raw_event_time_ms, corrected_event_time_ms, id0, id1
                    events = data[1]

                    # record event data
                    recorder.record_events(events, data[2])

                    # process event data
                    event_processor.process(events)

                elif data_type == "taxel_pc_time":

                    values = data[1]
                    raw_timestamp = data[2]
                    pc_timestamp = data[3]

                    print("[PC TIME]", raw_timestamp, pc_timestamp)


            except queue.Empty:
                continue

        if not handshake.ready:
            raise McuConfigurationError(
                "Serial connection ended before both MCU settings were acknowledged"
            )

    except KeyboardInterrupt:
        print("Stopping application...")
    except McuConfigurationError as exc:
        print(f"[MCU CONFIGURATION ERROR] {exc}")
        raise
    finally:

        # we stop recording on keyboard interrupt
        recorder.stop()

        sm.stop_serial()

        print("Application stopped.")


if __name__ == "__main__":
    main()