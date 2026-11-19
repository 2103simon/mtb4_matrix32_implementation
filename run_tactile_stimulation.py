"""MTB4 -> Feather M4 -> latest tactile rate -> Matrix32 channel 1.

No stimulation without --arm and explicit, positive output parameters in the
dedicated config/stim_tactile_ch1.yaml. Do not use on a person before
independent bench/oscilloscope verification.
"""
import argparse
import asyncio
import json
import math
import queue
import time
from pathlib import Path

from communication.mcu_configuration import McuConfigurationHandshake
from communication.serial_manager import SerialManager
from connect_matrix32 import check_bleak_version, check_pc_bluetooth
from matrix32 import Matrix32Device
from matrix32.config import load_config
from recording.data_recorder import DataRecorder


ROOT = Path(__file__).resolve().parent
MCU_CONFIG = ROOT / "config" / "config.json"
STIM_CONFIG = ROOT / "config" / "stim_tactile_ch1.yaml"
STALE_SECONDS = 0.25
POLL_SECONDS = 0.005
STOP_TIMEOUT_SECONDS = 4.0
DISCONNECT_TIMEOUT_SECONDS = 5.0


def validate_settings(config, stim_config, *, arm):
    enc = config["tactile_encoding"]
    source = config["stimulation_source"]
    threshold, sa_max = enc["raThreshold"], enc["saMaxFrequencyHz"]
    taxel, receptor = source["taxel"], source["encoding"]
    if type(threshold) is not int or not 1 <= threshold <= 255:
        raise ValueError("raThreshold must be an integer 1..255")
    if type(sa_max) is not int or not 10 <= sa_max <= 1000:
        raise ValueError("saMaxFrequencyHz must be an integer 10..1000")
    if type(taxel) is not int or not 0 <= taxel < 12:
        raise ValueError("stimulation_source.taxel must be 0..11")
    if receptor not in ("ra_i", "sa_ii"):
        raise ValueError("stimulation_source.encoding must be ra_i or sa_ii")
    if stim_config["sa"] != [1] or len(stim_config["sc"]) != 1 or len(stim_config["sw"]) != 1:
        raise ValueError("Only Matrix32 channel 1 is permitted (sa:[1], one sc/sw)")
    if not isinstance(stim_config["sf"], int) or not 2 <= stim_config["sf"] <= 400:
        raise ValueError("Matrix32 initial sf must be integer 2..400 Hz")
    if arm and (
        type(stim_config["sc"][0]) is not int or stim_config["sc"][0] <= 0
        or type(stim_config["sw"][0]) is not int or stim_config["sw"][0] <= 0
    ):
        raise ValueError(
            "Before --arm, set positive CH1 current and pulse width in "
            "config/stim_tactile_ch1.yaml; confirm safe values with your protocol"
        )
    return taxel, receptor


def requested_frequency(snapshot, taxel, receptor, *, max_hz=400):
    """Round the selected requested rate to valid Matrix32 Hz; 0 means STOP."""
    key = "ra_rates_hz" if receptor == "ra_i" else "sa_rates_hz"
    value = float(snapshot[key][taxel])
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"Invalid MCU requested frequency: {value}")
    # The device rejects SF1 START. Sub-2 Hz cannot safely be represented:
    # treat it as OFF rather than silently increasing the requested rate.
    if value < 2.0:
        return 0
    return max(2, min(max_hz, int(value + 0.5)))


async def mcu_receiver(sm, config, recorder, ready, finished):
    """Lossless CSV consumer; configure MCU before accepting any recording."""
    handshake = McuConfigurationHandshake(
        config["tactile_encoding"]["raThreshold"],
        config["tactile_encoding"]["saMaxFrequencyHz"],
    )
    deadline = time.monotonic() + 15.0
    try:
        while sm.is_running() or not sm.data_queue.empty():
            if not handshake.started and time.monotonic() > deadline:
                raise TimeoutError("No MCU telemetry within 15 seconds")
            handshake.check_timeout()
            try:
                data = sm.data_queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.003)
                continue
            kind = data[0]
            if kind == "link_ready":
                if not handshake.started:
                    handshake.start(sm)
                    print("Sent MCU RA_THR and SA_FREQ; waiting for both ACKs.")
                continue
            if kind == "ack":
                command, status, value = data[1:]
                print(f"[MCU ACK] cmd=0x{command:02X}, status={status}, value={value}")
                if handshake.accept_ack(command, status, value) and not ready.is_set():
                    # Any samples queued before the last ACK must not be
                    # recorded or used to trigger stimulation.
                    recorder.start(
                        config=config,
                        applied_settings={
                            "raThreshold": handshake.expected[0x01],
                            "saMaxFrequencyHz": handshake.expected[0x03],
                            "acknowledged": True,
                        },
                    )
                    ready.configured_ns = time.monotonic_ns()
                    ready.set()
                    print(f"[MCU] Configured; recording {recorder.recording_id}")
                continue
            if not ready.is_set():
                continue
            if kind == "tactile_rates":
                recorder.record_tactile_rates(data[1])
            elif kind == "taxel":
                recorder.record_taxel(data[1], data[2], data[3], data[4])
            elif kind == "events":
                recorder.record_events(data[1], data[2])
        raise ConnectionError("MCU serial reader stopped")
    finally:
        finished.set()


async def stop_output(device, *, reason):
    """STOP independently of current SF command; unknown if BLE already lost."""
    if device.client is None or not device.connected:
        print(f"[WARNING] {reason}: BLE unavailable; output state UNKNOWN")
        return
    print(f"[STOP] {reason}")
    try:
        await asyncio.wait_for(device.stop(), timeout=STOP_TIMEOUT_SECONDS)
    except Exception as exc:
        print(f"[WARNING] STOP not confirmed; output state UNKNOWN: {exc}")
        raise


async def frequency_worker(sm, device, ready, taxel, receptor, *, arm,
                           stale_seconds=STALE_SECONDS):
    """Single sequential BLE command worker. Never queues older frequencies."""
    await ready.wait()
    print(f"[ROUTING] taxel={taxel}, encoding={receptor}, Matrix32 CH1")
    last_sample_ns = None
    last_command = None
    stopped = True
    while True:
        if not sm.is_running():
            raise ConnectionError("MCU connection lost")
        if not device.connected:
            raise ConnectionError("Matrix32 BLE link lost; output state UNKNOWN")
        snapshot = sm.get_latest_rates_snapshot()
        # PC arrival timestamp is monotonic; avoid clock-domain mistakes.
        age = (time.monotonic_ns() - snapshot["pc_arrival_monotonic_ns"]) / 1e9 if snapshot else float("inf")
        if age > stale_seconds:
            if not stopped:
                await stop_output(device, reason=f"stale tactile input ({age:.3f}s)")
                stopped = True
                last_command = None
            await asyncio.sleep(POLL_SECONDS)
            continue
        sample_ns = snapshot["pc_arrival_monotonic_ns"]
        # A latest-slot sample may have been parsed BEFORE the configuration
        # ACKs. Only a newly received post-handshake sample may drive output.
        if sample_ns <= ready.configured_ns:
            await asyncio.sleep(POLL_SECONDS)
            continue
        if sample_ns == last_sample_ns:
            await asyncio.sleep(POLL_SECONDS)
            continue
        last_sample_ns = sample_ns
        target_hz = requested_frequency(snapshot, taxel, receptor)
        if not arm:
            # Dry-run mode makes no Matrix32 configuration/START/SF writes.
            if target_hz != last_command:
                print(f"[DRY RUN] Latest requested CH1 frequency: {target_hz} Hz")
            last_command = target_hz
            continue
        if target_hz == 0:
            if not stopped:
                await stop_output(device, reason="requested frequency is zero")
                stopped, last_command = True, None
            continue
        if stopped:
            # The initial SF must match the first live sample, not a stale
            # default. Every START follows exact ACK-verified configuration.
            device.set_global_frequency(target_hz)
            records = await device.apply(settle_time=0.02, require_ack=True)
            if not device.config_verified or len(records) != 7:
                raise RuntimeError("Matrix32 configuration ACK verification failed")
            # If the sensor went stale during the config writes, do not START.
            fresh = sm.get_latest_rates_snapshot()
            if fresh is None or ((time.monotonic_ns() - fresh["pc_arrival_monotonic_ns"]) / 1e9) > stale_seconds:
                raise TimeoutError("Tactile input went stale before Matrix32 START")
            if requested_frequency(fresh, taxel, receptor) == 0:
                continue
            # If it changed during apply, START only with a frequency that
            # still matches a recently observed request.
            if requested_frequency(fresh, taxel, receptor) != target_hz:
                continue
            await device.start()
            stopped = False
            last_command = target_hz
            print(f"[START] Matrix32 CH1: {target_hz} Hz")
            continue
        if target_hz == last_command:
            continue
        await device.apply_live_frequency(target_hz)
        last_command = target_hz
        print(f"[SF ACK] Matrix32 CH1: {target_hz} Hz")


async def stale_supervisor(sm, device, ready, *, arm, stale_seconds):
    """Independent stale-input STOP, even if a BLE SF update is still awaiting ACK."""
    if not arm:
        return
    await ready.wait()
    while True:
        await asyncio.sleep(0.02)
        if not device.connected or not sm.is_running():
            raise ConnectionError("Matrix32 or MCU link lost; stimulation state may be UNKNOWN")
        if not device.running:
            continue
        snapshot = sm.get_latest_rates_snapshot()
        age = (time.monotonic_ns() - snapshot["pc_arrival_monotonic_ns"]) / 1e9 if snapshot else float("inf")
        if age > stale_seconds:
            await stop_output(device, reason=f"watchdog: tactile sample age {age:.3f}s")
            # Cancel the worker rather than permitting it to issue a delayed
            # START/SF command after the STOP.
            raise TimeoutError("Stale tactile input: watchdog stopped stimulation")


async def run(config, stim_config, *, arm, stale_seconds=STALE_SECONDS,
              serial_factory=SerialManager, device_factory=Matrix32Device):
    taxel, receptor = validate_settings(config, stim_config, arm=arm)
    check_bleak_version()
    await asyncio.to_thread(check_pc_bluetooth)
    device = device_factory(config=stim_config)
    sm = serial_factory(config=config)
    recorder = DataRecorder(config["recordings_dir"])
    ready = asyncio.Event()
    ready.configured_ns = 0
    finished = asyncio.Event()
    try:
        await device.connect()
        if not device.connected or device.last_battery_level is None:
            raise RuntimeError("Matrix32 readiness check failed")
        print(f"[BLE] Ready. Battery={device.last_battery_level}%; stimulation OFF")
        sm.start_comm(config["serial"]["port"])
        if not sm.is_running():
            raise ConnectionError("Failed to open MCU serial connection")
        print(f"[MCU] Connected: {config['serial']['port']}")
        async with asyncio.TaskGroup() as group:
            group.create_task(mcu_receiver(sm, config, recorder, ready, finished))
            group.create_task(frequency_worker(sm, device, ready, taxel, receptor, arm=arm,
                                               stale_seconds=stale_seconds))
            group.create_task(stale_supervisor(sm, device, ready, arm=arm,
                                               stale_seconds=stale_seconds))
    finally:
        # In the normal no-output path this is a harmless best-effort STOP.
        # STOP is also attempted when START may have reached hardware but
        # failed before setting device.running.
        if device.client is not None:
            if device.connected:
                try:
                    await asyncio.wait_for(device.stop(), timeout=STOP_TIMEOUT_SECONDS)
                    print("[SHUTDOWN] Matrix32 STOP sent.")
                except BaseException as exc:
                    print(f"[WARNING] Matrix32 STOP uncertain: {exc}")
            else:
                print("[WARNING] Matrix32 disconnected: output state UNKNOWN")
            try:
                await asyncio.wait_for(device.disconnect(), timeout=DISCONNECT_TIMEOUT_SECONDS)
            except BaseException as exc:
                print(f"[WARNING] Matrix32 disconnect uncertain: {exc}")
        sm.stop_serial()
        recorder.stop()
        print("[SHUTDOWN] Finished.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", action="store_true",
                        help="Explicitly enable ACK-verified CH1 stimulation; otherwise DRY RUN")
    parser.add_argument("--stale-ms", type=int, default=250,
                        help="Stop output when no tactile sample arrives for this many ms (default 250)")
    args = parser.parse_args()
    if not 50 <= args.stale_ms <= 2000:
        parser.error("--stale-ms must be between 50 and 2000")
    with MCU_CONFIG.open(encoding="utf-8") as file:
        config = json.load(file)
    if not STIM_CONFIG.is_file():
        parser.error(f"Missing dedicated CH1 stimulation configuration: {STIM_CONFIG}")
    stim_config = load_config(STIM_CONFIG)
    try:
        asyncio.run(run(config, stim_config, arm=args.arm, stale_seconds=args.stale_ms / 1000))
    except KeyboardInterrupt:
        print("[STOP] Ctrl+C requested.")
    except Exception as exc:
        print(f"[ERROR] {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
