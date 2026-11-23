"""MTB4 -> Feather M4 -> latest tactile rate -> Matrix32 channel 1.

No stimulation without --arm. Fixed CH1 settings are in config/config.json
under matrix32_stimulation. Do not use on a person before
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
from recording.matrix32_trace import OptionalCapture


ROOT = Path(__file__).resolve().parent
MCU_CONFIG = ROOT / "config" / "config.json"
STALE_SECONDS = 0.25
POLL_SECONDS = 0.005
STOP_TIMEOUT_SECONDS = 4.0
DISCONNECT_TIMEOUT_SECONDS = 5.0


def build_stim_config(config):
    """Convert fixed CH1 settings to Matrix32 protocol configuration.

    Frequency is intentionally not configurable here: the MCU provides it.
    SF=20 is an untransmitted placeholder, overwritten from a fresh MCU
    snapshot before the first ACK-verified configuration/START.
    """
    settings = config["matrix32_stimulation"]
    channel = settings["channel"]
    current = settings["currentCommand"]
    width = settings["phaseWidthUs"]
    voltage = settings["outputVoltageCommand"]
    inter_pulse = settings["interPulseTimingCommand"]

    if type(channel) is not int or channel != 1:
        raise ValueError("Only Matrix32 channel 1 is supported")
    if type(current) is not int or not 1 <= current <= 65535:
        raise ValueError("matrix32_stimulation.currentCommand must be 1..65535")
    if type(width) is not int or not 1 <= width <= 65535:
        raise ValueError("matrix32_stimulation.phaseWidthUs must be 1..65535")
    if type(voltage) is not int or not 1 <= voltage <= 255:
        raise ValueError("matrix32_stimulation.outputVoltageCommand must be 1..255")
    if type(inter_pulse) is not int or not 1 <= inter_pulse <= 255:
        raise ValueError("matrix32_stimulation.interPulseTimingCommand must be 1..255")

    # A phase width is the commanded width of ONE polarity; nominal
    # biphasic occupied time starts at 2*width before device overhead.
    # At SF=400, physical output ~401 Hz gives 2493.77 us per cycle.
    # 2*100=200 us and 2*200=400 us fit the full CH1-only cycle.
    # A hypothetical /32 slot is only ~77.93 us, which fits neither.
    # There is NO evidence that CH1-only must obey such a /32 slot.
    # This arithmetic is NOT hardware acceptance or waveform validation:
    # verify the exact SF=400/SW setting on a dummy load and scope.
    cycle_us = 1_000_000 / 401.0
    if 2 * width >= cycle_us:
        raise ValueError(
            "Biphasic phase widths exceed one 401 Hz output cycle; "
            "choose a narrower phaseWidthUs"
        )
    return {
        "sa": [1], "sc": [current], "sw": [width],
        "pw": width, "sv": voltage, "st": inter_pulse,
        "sf": 20,
    }


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
            "config/config.json matrix32_stimulation; confirm safe values with your protocol"
        )
    return taxel, receptor


def requested_frequency(snapshot, taxel, receptor, *, max_hz=400):
    """Round selected request to Matrix32 Hz; 0 means muted SC1=0/SF=2."""
    key = "ra_rates_hz" if receptor == "ra_i" else "sa_rates_hz"
    value = float(snapshot[key][taxel])
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"Invalid MCU requested frequency: {value}")
    # The device rejects SF1 START. Sub-2 Hz cannot safely be represented:
    # treat it as OFF rather than silently increasing the requested rate.
    if value < 2.0:
        return 0
    # Upper device command clamp: any requested rate >400 Hz sends SF=400.
    # Bench observation: commanding SF=400 yields ~401 pulses/s at the
    # physical Matrix32 output. This ~1 Hz offset is accepted for now;
    # do not compensate by sending an out-of-range command.
    if value > max_hz:
        return max_hz
    return min(max_hz, int(value + 0.5))


async def mcu_receiver(sm, config, capture, ready):
    """Lossless CSV consumer; configure MCU before accepting any recording."""
    handshake = McuConfigurationHandshake(
        config["tactile_encoding"]["raThreshold"],
        config["tactile_encoding"]["saMaxFrequencyHz"],
    )
    deadline = time.monotonic() + 15.0
    processed_since_yield = 0
    while sm.is_running() or not sm.data_queue.empty():
        if not handshake.started and time.monotonic() > deadline:
            raise TimeoutError("No MCU telemetry within 15 seconds")
        handshake.check_timeout()
        try:
            data = sm.data_queue.get_nowait()
        except queue.Empty:
            await asyncio.sleep(0.003)
            continue
        processed_since_yield += 1
        if processed_since_yield >= 32:
            processed_since_yield = 0
            # Give the independent stale-input watchdog scheduling priority
            # even when many CSV samples are waiting to be drained.
            await asyncio.sleep(0)
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
                capture.start(applied_settings={
                    "raThreshold": handshake.expected[0x01],
                    "saMaxFrequencyHz": handshake.expected[0x03],
                    "acknowledged": True,
                })
                ready.configured_ns = time.monotonic_ns()
                ready.set()
                print("[MCU] Configured; ready for live data.")
            continue
        if not ready.is_set():
            continue
        if capture.recorder is not None:
            if kind == "tactile_rates":
                capture.recorder.record_tactile_rates(data[1])
            elif kind == "taxel":
                capture.recorder.record_taxel(data[1], data[2], data[3], data[4])
            elif kind == "events":
                capture.recorder.record_events(data[1], data[2])
    raise ConnectionError("MCU serial reader stopped")


async def stop_output(device, *, reason, capture=None):
    """STOP independently of current SF command; unknown if BLE already lost."""
    if device.client is None or not device.connected:
        print(f"[WARNING] {reason}: BLE unavailable; output state UNKNOWN")
        if capture is not None:
            capture.emit("stop_unavailable", reason=reason, output_state="UNKNOWN")
        return
    print(f"[STOP] {reason}")
    if capture is not None:
        capture.emit("stop_requested", reason=reason)
    try:
        await asyncio.wait_for(device.stop(), timeout=STOP_TIMEOUT_SECONDS)
        if capture is not None:
            capture.emit("stop_write_completed", reason=reason, output_verified=False)
    except Exception as exc:
        if capture is not None:
            capture.emit("stop_failed", reason=reason, error=repr(exc), output_state="UNKNOWN")
        print(f"[WARNING] STOP not confirmed; output state UNKNOWN: {exc}")
        raise


async def send_live_current(device, current, *, capture=None, source_sample_ns=None):
    """ACK-verified SC update; only verified protocol acceptance, not output-off."""
    if capture is not None:
        capture.emit("sc_requested", channel=1, current_command=current,
                     source_sample_ns=source_sample_ns, bench_unqualified=True)
    try:
        record = await device.apply_live_current(1, current)
    except BaseException as exc:
        if capture is not None:
            capture.emit("sc_failed", channel=1, current_command=current,
                         error=repr(exc), evidence=getattr(exc, "record", None))
        raise
    if capture is not None:
        capture.emit("sc_ack", channel=1, current_command=current,
                     status=record["status"], evidence=record,
                     output_verified=False)
    if record["status"] != "OK":
        raise RuntimeError("Live SC update was not ACK-verified")
    return record


# SC=0 mute: request the fastest supported scheduler period after zero-current ACK.
# This only bounds the NEXT scheduler opportunity in the modeled device;
# an ACK does not prove the CH1 output is electrically off.
ZERO_CURRENT_FAST_SF_HZ = 400


async def frequency_worker(sm, device, ready, taxel, receptor, *, arm,
                           stale_seconds=STALE_SECONDS, capture=None):
    """Single sequential BLE command worker. Never queues older frequencies."""
    await ready.wait()
    print(f"[ROUTING] taxel={taxel}, encoding={receptor}, Matrix32 CH1")
    last_sample_ns = None
    last_command = None
    stopped = True
    muted_current = False
    original_current = device.config["sc"][0]
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
                await stop_output(device, reason=f"stale tactile input ({age:.3f}s)", capture=capture)
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
            if stopped:
                continue  # Never START with no positive sensor request.
            if not muted_current:
                # Previous bench observation: CH1 pulses were not visible
                # with SC1=0/SF=2. This does not establish off state at SF400.
                # Matrix32 SF changes apply at a cycle boundary: after SC1=0
                # is ACKed, set SF=400 to shorten future scheduling periods.
                # The SC=0 write itself may wait for the PREVIOUS cycle;
                # this sequence is NOT an immediate physical off guarantee.
                # Write SC=0 BEFORE fast SF400 to reduce the risk of a
                # rate increase at the original current setting. However,
                # BLE ACK ordering does NOT prove physical application
                # ordering or that current is already zero at the output.
                # Operate only with verified resistor load and safe probing.
                await send_live_current(
                    device, 0, capture=capture, source_sample_ns=sample_ns
                )
                muted_current = True
                print("[SC ACK] SC1=0 accepted (no visible pulses in prior bench test; verify scope)")
                if device.config["sf"] != ZERO_CURRENT_FAST_SF_HZ:
                    if capture is not None:
                        capture.emit("sf_requested", frequency_hz=ZERO_CURRENT_FAST_SF_HZ,
                                     source_sample_ns=sample_ns,
                                     zero_current_workaround=True,
                                     fast_zero_current_scheduler=True)
                    try:
                        sf_record = await device.apply_live_frequency(ZERO_CURRENT_FAST_SF_HZ)
                    except BaseException as exc:
                        if capture is not None:
                            capture.emit("sf_failed", frequency_hz=ZERO_CURRENT_FAST_SF_HZ,
                                         error=repr(exc),
                                         evidence=getattr(exc, "record", None))
                        raise
                    if capture is not None:
                        capture.emit("sf_ack", frequency_hz=ZERO_CURRENT_FAST_SF_HZ, status=sf_record["status"],
                                     evidence=sf_record, zero_current_workaround=True,
                                     fast_zero_current_scheduler=True)
                    if sf_record["status"] != "OK":
                        raise RuntimeError("Zero-current fast SF=400 was not ACK-verified")
                last_command = 0
            continue
        if stopped:
            # The initial SF must match the first live sample, not a stale
            # default. Every START follows exact ACK-verified configuration.
            device.set_global_frequency(target_hz)
            if capture is not None:
                capture.emit("configuration_requested", frequency_hz=target_hz,
                             source_sample_ns=sample_ns, configuration=device.config)
            try:
                records = await device.apply(settle_time=0.02, require_ack=True)
            except BaseException as exc:
                if capture is not None:
                    capture.emit("configuration_failed", error=repr(exc),
                                 evidence=list(device.config_ack_records))
                raise
            if capture is not None:
                capture.emit("configuration_ack", frequency_hz=target_hz,
                             evidence=records,
                             verified=device.config_verified)
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
            if capture is not None:
                capture.emit("start_requested", frequency_hz=target_hz)
            try:
                await device.start()
            except BaseException as exc:
                if capture is not None:
                    capture.emit("start_failed", error=repr(exc), output_state="UNKNOWN")
                raise
            if capture is not None:
                capture.emit("start_write_completed", frequency_hz=target_hz,
                             output_verified=False)
            stopped = False
            last_command = target_hz
            print(f"[START] Matrix32 CH1: {target_hz} Hz")
            continue
        if muted_current:
            # Restore frequency first, while current remains commanded zero.
            # Restore the original current only after the positive SF ACK.
            if device.config["sf"] != target_hz:
                if capture is not None:
                    capture.emit("sf_requested", frequency_hz=target_hz,
                                 source_sample_ns=sample_ns, restoring_current=True)
                try:
                    record = await device.apply_live_frequency(target_hz)
                except BaseException as exc:
                    if capture is not None:
                        capture.emit("sf_failed", frequency_hz=target_hz,
                                     error=repr(exc),
                                     evidence=getattr(exc, "record", None))
                    raise
                if capture is not None:
                    capture.emit("sf_ack", frequency_hz=target_hz,
                                 status=record["status"], evidence=record,
                                 restoring_current=True)
                if record["status"] != "OK":
                    raise RuntimeError("Positive SF restore was not ACK-verified")
            await send_live_current(
                device, original_current, capture=capture, source_sample_ns=sample_ns
            )
            muted_current = False
            last_command = target_hz
            print(f"[SC ACK] Restored SC1={original_current}; SF={target_hz} Hz")
            continue
        if target_hz == last_command:
            continue
        if capture is not None:
            capture.emit("sf_requested", frequency_hz=target_hz,
                         source_sample_ns=sample_ns)
        try:
            sf_record = await device.apply_live_frequency(target_hz)
        except BaseException as exc:
            if capture is not None:
                capture.emit("sf_failed", frequency_hz=target_hz,
                             error=repr(exc),
                             evidence=getattr(exc, "record", None))
            raise
        if capture is not None:
            capture.emit("sf_ack", frequency_hz=target_hz, status=sf_record["status"],
                         evidence=sf_record)
        if sf_record["status"] != "OK":
            raise RuntimeError("Matrix32 SF update not ACK-verified")
        last_command = target_hz
        print(f"[SF ACK] Matrix32 CH1: {target_hz} Hz")


async def stale_supervisor(sm, device, ready, *, arm, stale_seconds, capture=None):
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
            await stop_output(device, reason=f"watchdog: tactile sample age {age:.3f}s", capture=capture)
            # Cancel the worker rather than permitting it to issue a delayed
            # START/SF command after the STOP.
            raise TimeoutError("Stale tactile input: watchdog stopped stimulation")


async def run(config, stim_config=None, *, arm, record=False,
              stale_seconds=STALE_SECONDS,
              serial_factory=SerialManager, device_factory=Matrix32Device):
    stim_config = build_stim_config(config) if stim_config is None else stim_config
    taxel, receptor = validate_settings(config, stim_config, arm=arm)
    check_bleak_version()
    await asyncio.to_thread(check_pc_bluetooth)
    device = device_factory(config=stim_config)
    sm = serial_factory(config=config)
    ready = asyncio.Event()
    ready.configured_ns = 0
    manifest_config = {
        **config,
        "matrix32_stimulation": stim_config,
        "stimulation_control": {
            "armed": arm,
            "record": record,
            "zero_request_strategy": "SC1=0_then_SF=2; restore_SF_then_SC1",
            "stale_timeout_ms": int(stale_seconds * 1000),
            "source": config["stimulation_source"],
            "matrix32_channel": 1,
        },
    }
    capture = OptionalCapture(record, config["recordings_dir"], manifest_config)
    try:
        await device.connect()
        if not device.connected or device.last_battery_level is None:
            raise RuntimeError("Matrix32 readiness check failed")
        print(f"[BLE] Ready. Battery={device.last_battery_level}%; stimulation OFF")
        sm.start_comm(config.get("serial", {}).get("port"))
        if not sm.is_running():
            raise ConnectionError("Failed to open MCU serial connection")
        print(f"[MCU] Connected: {sm.connected_port or config.get('serial', {}).get('port')}")
        async with asyncio.TaskGroup() as group:
            group.create_task(mcu_receiver(sm, config, capture, ready))
            group.create_task(frequency_worker(sm, device, ready, taxel, receptor, arm=arm,
                                               stale_seconds=stale_seconds, capture=capture))
            group.create_task(stale_supervisor(sm, device, ready, arm=arm,
                                               stale_seconds=stale_seconds, capture=capture))
    finally:
        # In the normal no-output path this is a harmless best-effort STOP.
        # STOP is also attempted when START may have reached hardware but
        # failed before setting device.running.
        if device.client is not None:
            if device.connected:
                try:
                    capture.emit("shutdown_stop_requested")
                    await asyncio.wait_for(device.stop(), timeout=STOP_TIMEOUT_SECONDS)
                    capture.emit("shutdown_stop_write_completed", output_verified=False)
                    print("[SHUTDOWN] Matrix32 STOP sent.")
                except BaseException as exc:
                    capture.emit("shutdown_stop_failed", error=repr(exc),
                                 output_state="UNKNOWN")
                    print(f"[WARNING] Matrix32 STOP uncertain: {exc}")
            else:
                capture.emit("ble_lost", output_state="UNKNOWN")
                print("[WARNING] Matrix32 disconnected: output state UNKNOWN")
            try:
                await asyncio.wait_for(device.disconnect(), timeout=DISCONNECT_TIMEOUT_SECONDS)
                capture.emit("ble_disconnect_completed")
            except BaseException as exc:
                capture.emit("ble_disconnect_failed", error=repr(exc))
                print(f"[WARNING] Matrix32 disconnect uncertain: {exc}")
        sm.stop_serial()
        capture.close()
        print("[SHUTDOWN] Finished.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", action="store_true",
                        help="Explicitly enable ACK-verified CH1 stimulation; otherwise DRY RUN")
    parser.add_argument("--record", action="store_true",
                        help="Save all MCU samples and timestamped Matrix32 commands/ACKs")
    parser.add_argument("--recordings-dir", type=Path, default=None,
                        help="Override recording output directory (for integrated scope sessions)")
    parser.add_argument("--stale-ms", type=int, default=250,
                        help="Stop output when no tactile sample arrives for this many ms (default 250)")
    args = parser.parse_args()
    if not 50 <= args.stale_ms <= 2000:
        parser.error("--stale-ms must be between 50 and 2000")
    with MCU_CONFIG.open(encoding="utf-8") as file:
        config = json.load(file)
    if args.recordings_dir is not None:
        config["recordings_dir"] = str(args.recordings_dir)
    stim_config = build_stim_config(config)
    print(
        f"[CONFIG] Fixed Matrix32 CH1: SC={stim_config['sc'][0]}, "
        f"SW={stim_config['sw'][0]} us/phase, "
        f"SV={stim_config['sv']}, ST={stim_config['st']}; "
        "SF comes from the selected MCU rate."
    )
    try:
        asyncio.run(run(config, stim_config, arm=args.arm,
                        record=args.record,
                        stale_seconds=args.stale_ms / 1000))
    except KeyboardInterrupt:
        print("[STOP] Ctrl+C requested.")
    except Exception as exc:
        print(f"[ERROR] {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
