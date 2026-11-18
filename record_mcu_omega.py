"""Record MCU and Omega states while running a circular trajectory.

CAUTION: Commands robot motion. Verify the path is safe before running.
"""
import json
import math
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

from omega_control import OmegaRobot

from communication.serial_manager import SerialManager
from recording.data_recorder import DataRecorder
from recording.omega_recorder import record_omega


CONFIG_PATH = Path(__file__).resolve().parent / "config" / "config.json"


def record_mcu(sm, recorder, stop_event):
    """Drain the serial queue into the existing CSV recorder."""
    while not stop_event.is_set() or not sm.data_queue.empty():
        try:
            data = sm.data_queue.get(timeout=0.1)
        except queue.Empty:
            continue

        if data[0] == "taxel":
            recorder.record_taxel(data[1], data[2], data[3], data[4])
        elif data[0] == "events":
            recorder.record_events(data[1], data[2])


def main():
    with open(CONFIG_PATH) as f:
        config = json.load(f)

    radius = 0.04
    frequency = 0.25
    duration = 60.0

    sm = SerialManager(config=config)
    recorder = DataRecorder(config["recordings_dir"])
    robot = OmegaRobot(verbose=False, auto_open=False, auto_initialize=False)

    mcu_stop = threading.Event()
    omega_stop = threading.Event()
    omega_errors = []
    mcu_thread = threading.Thread(target=record_mcu, args=(sm, recorder, mcu_stop))
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    omega_path = recorder.recording_dir / f"{timestamp}_omega.csv"
    omega_thread = threading.Thread(
        target=record_omega,
        args=(robot, omega_path, omega_stop, omega_errors),
    )

    try:
        robot.open(
            device_index=config["omega"]["device_index"],
            expected_serial=config["omega"]["serial_number"],
        )
        print(f"Verified Omega serial: {robot.get_serial_number()}")
        robot.initialize()

        sm.start_comm(config["serial"]["port"])
        if not sm.is_running():
            raise RuntimeError("Could not connect to MCU")

        robot.set_pos([radius, 0.0, 0.0])
        time.sleep(0.5)

        recorder.start(timestamp=timestamp)
        mcu_thread.start()
        omega_thread.start()
        try:
            start = time.monotonic()
            omega = 2 * math.pi * frequency
            while (t := time.monotonic() - start) < duration:
                if omega_stop.is_set():
                    raise RuntimeError("Omega state recording stopped unexpectedly")
                if not sm.is_running():
                    raise RuntimeError("MCU serial communication stopped unexpectedly")
                x = radius * math.cos(omega * t)
                y = radius * math.sin(omega * t)
                robot.set_pos([x, y, 0.0], tracking=True)
                time.sleep(0.001)
        except KeyboardInterrupt:
            print("Trajectory interrupted.")
    finally:
        try:
            if robot.is_open():
                robot.stop()
        finally:
            omega_stop.set()
            if omega_thread.is_alive():
                omega_thread.join()
            if robot.is_open():
                robot.close()

            sm.stop_serial()
            mcu_stop.set()
            if mcu_thread.is_alive():
                mcu_thread.join()
            recorder.stop()

    if omega_errors:
        raise RuntimeError("Omega state recording failed") from omega_errors[0]
    print(f"Omega states saved to {omega_path}")


if __name__ == "__main__":
    main()
