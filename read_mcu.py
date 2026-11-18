import os
import json
import queue
from pathlib import Path
from datetime import datetime

from communication.serial_manager import SerialManager

from processing.taxel_processing import TaxelProcessor
from processing.event_processing import EventProcessor
from processing.data_analysis import DataAnalysis
from recording.data_recorder import DataRecorder

# Project root directory
BASE_DIR = Path(__file__).resolve().parent

# Configuration file
CONFIG_PATH = BASE_DIR / "config" / "config.json"


def main():

    # ----------------------------------
    # 1. Load configuration
    # ----------------------------------

    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)

    print("Configuration loaded.")

    # ----------------------------------
    # 2. Initialize communication
    # ----------------------------------

    sm = SerialManager(config=config)

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

    # ----------------------------------
    # 6. Start recording
    # ----------------------------------

    recorder.start()

    # ----------------------------------
    # 5. Main processing loop
    # ----------------------------------

    try:

        while sm.is_running() or not sm.data_queue.empty():

            try:

                data = sm.data_queue.get(timeout=0.1)

                data_type = data[0]

                if data_type == "taxel":

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

                elif data_type == "ack":

                    command_id = data[1]
                    status = data[2]
                    value = data[3]

                    print(
                        f"[ACK] {command_id}, {status}, {value}"
                    )

                elif data_type == "link_ready":

                    print("MCU connection ready!")

            except queue.Empty:
                continue

    except KeyboardInterrupt:

        print("Stopping application...")

    finally:

        # we stop recording on keyboard interrupt
        recorder.stop()

        sm.stop_serial()

        print("Application stopped.")


if __name__ == "__main__":
    main()