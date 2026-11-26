import asyncio
from copy import deepcopy
import math
import time

from bleak import BleakClient

from .ble import query_battery_report, scan_ble_device, stim_stop
from .ack import (
    ConfigurationAckError,
    PersistentConfigAckSession,
    write_config_with_ack,
)
from .config import SAFE_DEFAULTS, build_config_messages


TARGET_DEVICE = "E-Matrix32"
SERVICE_UUID = "00009b22-0000-1000-8000-00805f9b34fb"
CONNECT_MAX_ATTEMPTS = 20
CONNECT_RETRY_DELAY_SECONDS = 1.5
CONNECT_STABILIZATION_SECONDS = 3.0
RECONNECT_SETTLE_SECONDS = 2.0
LIVE_FREQUENCY_MIN_ACK_TO_WRITE_MS = 7.5
LIVE_FREQUENCY_MAX_ACK_TO_WRITE_MS = 5000.0


class LiveCurrentUpdateError(RuntimeError):
    """A live SC transaction failed; .record retains host-side evidence."""

    def __init__(self, record):
        self.record = record
        super().__init__(
            "Live current update {} (CH{}: {} -> {} command units)".format(
                record["status"], record["channel"],
                record["old_current_command"], record["new_current_command"]))


class LiveWidthUpdateError(RuntimeError):
    """A live SW transaction failed; .record retains its partial evidence."""

    def __init__(self, record):
        self.record = record
        super().__init__(
            "Live width update {} (CH{}: {} -> {} us)".format(
                record["status"], record["channel"],
                record["old_width_us"], record["new_width_us"]))


class LiveFrequencyUpdateError(RuntimeError):
    """A live SF transaction failed; .record retains its partial evidence."""

    def __init__(self, record):
        self.record = record
        super().__init__(
            "Live frequency update {} ({} -> {} Hz)".format(
                record["status"], record["old_frequency_hz"],
                record["new_frequency_hz"]))


class Matrix32Device:
    """Programmatic Python interface for the E-Matrix32.

    The protocol exposed by the current project supports:
    - per-channel current (SC)
    - per-channel pulse width (SW)
    - active-channel mask (SA)
    - global stimulation frequency (SF)
    - global output voltage (SV)
    - global inter-pulse timing (ST)

    No separate per-channel frequency command is known in the available protocol.

    Consecutive live global-frequency updates use the qualified persistent
    indication transport by default; see docs/live_frequency_updates.md.
    """

    def __init__(self, config=None, target_device=TARGET_DEVICE, *,
                 experimental_shared_sf_sc=False):
        if type(experimental_shared_sf_sc) is not bool:
            raise ValueError("Experimental shared SF/SC mode must be boolean")
        self.experimental_shared_sf_sc = experimental_shared_sf_sc
        self.target_device = target_device
        self.config = {
            "sa": [],
            "sc": [],
            "sf": SAFE_DEFAULTS["sf"],
            "pw": SAFE_DEFAULTS["pw"],
            "sw": [],
            "st": SAFE_DEFAULTS["st"],
            "sv": SAFE_DEFAULTS["sv"],
        }
        if config:
            self._load_config(config)

        self.client = None
        self.write_char_uuid = None
        self.read_char_uuid = None
        self.running = False
        self._stop_cleanup_needed = False
        self.last_battery_level = None
        self.config_ack_records = []
        self.config_verified = False
        self.verified_config_snapshot = None
        # Configuration ACKs and battery reports share one indication channel.
        # STOP deliberately does not acquire this lock: an independent stop
        # deadline must not wait for a stalled configuration transaction.
        self._configuration_lock = asyncio.Lock()
        self._control_generation = 0
        self._live_current_task = None
        self.live_current_records = []
        self._live_width_task = None
        self.live_width_records = []
        self._live_frequency_task = None
        self._live_frequency_cleanup_task = None
        # The persistent exchange slot also owns SC in the experimental mode.
        self._live_persistent_record = None
        self._live_frequency_session = None
        self._live_frequency_last_ack_monotonic_s = None
        self._live_frequency_last_write_begin_monotonic_s = None
        self._live_config_last_ack_monotonic_s = None
        self._live_config_last_write_begin_monotonic_s = None
        self.live_frequency_records = []
        self.disconnect_observations = []

    def _load_config(self, config):
        cfg = deepcopy(config)
        channels = list(cfg.get("sa", []))
        currents = cfg.get("sc", SAFE_DEFAULTS["sc"])
        widths = cfg.get("sw", SAFE_DEFAULTS["sw"])

        if not isinstance(currents, list):
            currents = [currents] * len(channels)
        if not isinstance(widths, list):
            widths = [widths] * len(channels)

        self.config.update(cfg)
        self.config["sa"] = channels
        self.config["sc"] = list(currents[: len(channels)])
        self.config["sw"] = list(widths[: len(channels)])

        while len(self.config["sc"]) < len(channels):
            self.config["sc"].append(SAFE_DEFAULTS["sc"])
        while len(self.config["sw"]) < len(channels):
            self.config["sw"].append(SAFE_DEFAULTS["sw"])

    def _on_disconnect(self, client):
        # Record transport-level link loss separately from completion of the
        # coroutine that requested disconnect. On Linux/BlueZ the callback can
        # arrive after an awaited Bleak disconnect() exceeds our supervisory
        # timeout even though the physical link has in fact gone down.
        self.disconnect_observations.append({
            "monotonic_s": time.monotonic(),
            "current_client": client is self.client,
        })
        # Do not assume stimulation state survives a transport loss.
        self.abort_pending_commands()
        # The physical link is gone, so any persistent indication subscription
        # is gone with it. No asynchronous unsubscribe is possible from this
        # synchronous Bleak disconnect callback.
        self._live_frequency_session = None
        self._live_frequency_last_ack_monotonic_s = None
        self._live_frequency_last_write_begin_monotonic_s = None
        self._live_config_last_ack_monotonic_s = None
        self._live_config_last_write_begin_monotonic_s = None
        print("[Matrix32] BLE link disconnected.")

    def abort_pending_commands(self):
        """Invalidate pending START/SW/SF work before an independent STOP attempt.

        This only changes host state; it does not send STOP or prove output-off.
        The caller must still cancel/supervise tasks and issue bounded R/OFF.
        A write already handed to the BLE transport cannot be retracted here.
        A later explicit START requires a fresh ACK-verified configuration.
        """
        if self.running:
            self._stop_cleanup_needed = True
        self._control_generation += 1
        self.running = False
        self.config_verified = False
        self.verified_config_snapshot = None
        if self._live_current_task is not None and not self._live_current_task.done():
            self.live_current_records[-1]["ack"]["abort_requested"] = True
        if self._live_width_task is not None and not self._live_width_task.done():
            self.live_width_records[-1]["ack"]["abort_requested"] = True
        if (self._live_frequency_task is not None
                and not self._live_frequency_task.done()):
            record = self._live_persistent_record
            if record is None:  # legacy per-command SF path
                record = self.live_frequency_records[-1]
            record["ack"]["abort_requested"] = True

    def _check_control_generation(self, generation):
        task = asyncio.current_task()
        cancelling = getattr(task, "cancelling", lambda: 0)()
        if generation != self._control_generation or cancelling:
            raise asyncio.CancelledError("Matrix32 control operation was aborted")

    async def connect(
        self,
        max_attempts=CONNECT_MAX_ATTEMPTS,
        retry_delay=CONNECT_RETRY_DELAY_SECONDS,
        stabilization_time=CONNECT_STABILIZATION_SECONDS,
    ):
        """Connect only after the Matrix32 BLE link has proven stable.

        On the current Linux/BlueZ host, Bleak can finish connect() and GATT
        discovery before the controller completes the peripheral-requested
        connection-parameter update. Unstable links typically disappear about
        0.6 s after establishment. We therefore require the link to survive a
        stabilization window and then complete the documented non-stimulating
        battery-report command/response round trip before exposing it to callers.
        """
        if self.client is not None and self.client.is_connected:
            return self

        last_error = None

        for attempt in range(1, max_attempts + 1):
            device = await scan_ble_device(self.target_device, attempts=1)
            if device is None:
                last_error = RuntimeError(f"Could not find {self.target_device}.")
            else:
                client = BleakClient(
                    device,
                    disconnected_callback=self._on_disconnect,
                )
                write_char_uuid = None
                read_char_uuid = None

                try:
                    print(
                        f"[Matrix32 connect {attempt}/{max_attempts}] "
                        f"Connecting to {device.name or self.target_device}..."
                    )
                    await client.connect(dangerous_use_bleak_cache=True)

                    for service in client.services:
                        if service.uuid != SERVICE_UUID:
                            continue
                        for char in service.characteristics:
                            if "write-without-response" in char.properties:
                                write_char_uuid = char.uuid
                            if "indicate" in char.properties:
                                read_char_uuid = char.uuid

                    if write_char_uuid is None:
                        raise RuntimeError(
                            "E-Matrix32 write characteristic was not found."
                        )
                    if read_char_uuid is None:
                        raise RuntimeError(
                            "E-Matrix32 readable indication characteristic was not found."
                        )

                    print(
                        f"[Matrix32 connect {attempt}/{max_attempts}] "
                        f"GATT resolved; proving link stability for "
                        f"{stabilization_time:.1f}s..."
                    )
                    await asyncio.sleep(stabilization_time)

                    if not client.is_connected:
                        raise RuntimeError(
                            "BLE link disconnected during stabilization window."
                        )

                    battery_level, battery_payload = await query_battery_report(
                        client,
                        write_char_uuid,
                        read_char_uuid,
                    )

                    if not client.is_connected:
                        raise RuntimeError(
                            "BLE link disconnected during battery health check."
                        )

                    self.client = client
                    self.write_char_uuid = write_char_uuid
                    self.read_char_uuid = read_char_uuid
                    self.last_battery_level = battery_level
                    print(
                        f"[Matrix32 connect {attempt}/{max_attempts}] "
                        f"Battery report {battery_level}% "
                        f"({battery_payload.hex(' ')}); "
                        "stable two-way BLE connection ready."
                    )
                    return self

                except Exception as exc:
                    last_error = exc
                    print(
                        f"[Matrix32 connect {attempt}/{max_attempts}] "
                        f"Rejected unstable/failed link: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    try:
                        # Always ask Bleak to tear down the client object. This
                        # mirrors the validated lifecycle harness and avoids
                        # carrying stale BlueZ state into the next scan.
                        await client.disconnect()
                    except Exception:
                        pass

            if attempt < max_attempts:
                await asyncio.sleep(retry_delay)

        raise RuntimeError(
            f"Could not establish a stable Matrix32 BLE connection after "
            f"{max_attempts} attempts."
        ) from last_error

    async def disconnect(self):
        if self.client is None:
            return

        client = self.client
        try:
            if (self.running or self._stop_cleanup_needed) and client.is_connected:
                try:
                    await self.stop()
                except Exception as exc:
                    print(f"Could not stop stimulation cleanly during disconnect: {exc}")
        finally:
            live_sf_in_flight = (
                self._live_frequency_task is not None
                and not self._live_frequency_task.done())
            if (client.is_connected
                    and self._live_frequency_session is not None
                    and not live_sf_in_flight):
                try:
                    await self._close_live_frequency_session()
                except Exception as exc:
                    print(
                        "Could not close persistent live-frequency indication "
                        "session during disconnect: {}: {}".format(
                            type(exc).__name__, exc))
            try:
                if client.is_connected:
                    await client.disconnect()
            finally:
                self.client = None
                self.write_char_uuid = None
                self.read_char_uuid = None
                self.running = False
                self.config_verified = False

    async def reconnect(self, settle_time=RECONNECT_SETTLE_SECONDS, **connect_kwargs):
        """Cleanly disconnect and establish a new proven-stable BLE session.

        Stimulation is never resumed automatically after reconnect.
        """
        await self.disconnect()
        if settle_time:
            await asyncio.sleep(settle_time)
        return await self.connect(**connect_kwargs)

    async def __aenter__(self):
        return await self.connect()

    async def __aexit__(self, exc_type, exc, tb):
        await self.disconnect()

    @property
    def connected(self):
        return bool(self.client is not None and self.client.is_connected)

    async def _close_live_frequency_session(self):
        """Close the reusable SF indication subscription, if one is open."""
        session = self._live_frequency_session
        if session is None:
            self._live_frequency_last_ack_monotonic_s = None
            self._live_frequency_last_write_begin_monotonic_s = None
            self._live_config_last_ack_monotonic_s = None
            self._live_config_last_write_begin_monotonic_s = None
            return
        try:
            await session.close()
        finally:
            self._live_frequency_session = None
            self._live_frequency_last_ack_monotonic_s = None
            self._live_frequency_last_write_begin_monotonic_s = None
            self._live_config_last_ack_monotonic_s = None
            self._live_config_last_write_begin_monotonic_s = None

    async def close_live_frequency_session(self):
        """Explicitly release the optimized live-SF indication subscription.

        Consecutive apply_live_frequency() calls normally reuse one persistent
        indication subscription automatically. Other indication-owning device
        operations close it themselves, so callers rarely need this method.
        It is exposed for applications that want to release the subscription
        while stimulation remains running.
        """
        if self._configuration_lock.locked():
            raise RuntimeError(
                "Another Matrix32 configuration/indication transaction is pending")
        async with self._configuration_lock:
            await self._close_live_frequency_session()

    async def prepare_live_frequency_session(self):
        """Pre-open the reusable indication subscription for time-critical SF.

        apply_live_frequency() opens the session lazily when needed, so this is
        optional for ordinary code. Call it after START and before a
        time-critical update burst to keep the one-time subscription setup out
        of the first measured live-frequency transaction.
        """
        if self._configuration_lock.locked():
            raise RuntimeError(
                "Another Matrix32 configuration/indication transaction is pending")
        async with self._configuration_lock:
            self._require_connection()
            if not self.running:
                raise RuntimeError(
                    "Live frequency session preparation requires running stimulation")
            if self.read_char_uuid is None:
                raise RuntimeError(
                    "Matrix32Device indication characteristic is unavailable")
            if (not self.config_verified
                    or self.config != self.verified_config_snapshot):
                raise RuntimeError(
                    "Live frequency session preparation requires the exact "
                    "ACK-verified configuration")
            session, reused = await self._ensure_live_frequency_session()
            return {
                "persistent_session_reused": reused,
                "subscribe_begin_monotonic_s":
                    session.subscribe_begin_monotonic_s,
                "subscribe_return_monotonic_s":
                    session.subscribe_return_monotonic_s,
            }

    async def _ensure_live_frequency_session(self):
        session = self._live_frequency_session
        if session is not None:
            if session.client is self.client and self.connected:
                return session, True
            await self._close_live_frequency_session()
        session = PersistentConfigAckSession(
            self.client, self.write_char_uuid, self.read_char_uuid)
        await session.open()
        self._live_frequency_session = session
        return session, False

    def set_config(self, config):
        """Replace the in-memory stimulation configuration."""
        self._load_config(config)
        self.config_verified = False

    async def read_battery(self):
        """Return the current battery percentage over the proven BLE session."""
        async with self._configuration_lock:
            await self._close_live_frequency_session()
            self._require_connection()
            if self.read_char_uuid is None:
                raise RuntimeError("Matrix32Device indication characteristic is unavailable.")

            level, _payload = await query_battery_report(
                self.client,
                self.write_char_uuid,
                self.read_char_uuid,
            )
            self.last_battery_level = level
            return level

    def set_global_frequency(self, hz):
        if not isinstance(hz, int) or not 1 <= hz <= 400:
            raise ValueError("Manufacturer SF range is integer 1..400 Hz")
        self.config["sf"] = hz
        self.config_verified = False

    def set_output_voltage(self, volts):
        self.config["sv"] = int(volts)
        self.config_verified = False

    def set_inter_pulse_timing(self, value):
        self.config["st"] = int(value)
        self.config_verified = False

    def set_active_channels(self, channels):
        self.config_verified = False
        channels = sorted(set(int(ch) for ch in channels))
        for channel in channels:
            self._validate_channel(channel)

        old = {
            ch: (current, width)
            for ch, current, width in zip(
                self.config["sa"],
                self.config["sc"],
                self.config["sw"],
            )
        }

        self.config["sa"] = channels
        self.config["sc"] = [
            old.get(ch, (SAFE_DEFAULTS["sc"], SAFE_DEFAULTS["sw"]))[0]
            for ch in channels
        ]
        self.config["sw"] = [
            old.get(ch, (SAFE_DEFAULTS["sc"], SAFE_DEFAULTS["sw"]))[1]
            for ch in channels
        ]

    def set_channel(self, channel, current=None, pulse_width=None, active=True):
        self.config_verified = False
        channel = int(channel)
        self._validate_channel(channel)

        channel_map = {
            ch: [current_value, width]
            for ch, current_value, width in zip(
                self.config["sa"],
                self.config["sc"],
                self.config["sw"],
            )
        }

        if channel not in channel_map:
            channel_map[channel] = [SAFE_DEFAULTS["sc"], SAFE_DEFAULTS["sw"]]

        if current is not None:
            channel_map[channel][0] = int(current)
        if pulse_width is not None:
            channel_map[channel][1] = int(pulse_width)

        if not active:
            channel_map.pop(channel, None)

        channels = sorted(channel_map)
        self.config["sa"] = channels
        self.config["sc"] = [channel_map[ch][0] for ch in channels]
        self.config["sw"] = [channel_map[ch][1] for ch in channels]

    def set_channel_current(self, channel, current):
        self.set_channel(channel, current=current, active=True)

    def set_channel_pulse_width(self, channel, pulse_width):
        self.set_channel(channel, pulse_width=pulse_width, active=True)

    def deactivate_channel(self, channel):
        self.set_channel(channel, active=False)

    async def apply(self, settle_time=0.02, require_ack=False):
        async with self._configuration_lock:
            return await self._apply_config(settle_time, require_ack)

    async def _apply_config(self, settle_time, require_ack):
        await self._close_live_frequency_session()
        self._require_connection()
        self.config_verified = False
        self.verified_config_snapshot = None
        self.config_ack_records = []
        for message in build_config_messages(self.config):
            if require_ack:
                try:
                    record = await write_config_with_ack(
                        self.client, self.write_char_uuid, self.read_char_uuid,
                        message)
                except ConfigurationAckError as exc:
                    self.config_ack_records.append(exc.record)
                    raise
                self.config_ack_records.append(record)
            else:
                await self.client.write_gatt_char(self.write_char_uuid, message)
            if settle_time:
                await asyncio.sleep(settle_time)
        self.config_verified = bool(require_ack)
        self.verified_config_snapshot = deepcopy(self.config) if require_ack else None
        return list(self.config_ack_records)

    async def apply_live_pulse_width(
            self, channel, pulse_width, *, timeout_s=2.0, timing_marker=None):
        """Send exactly one ACK-required SW vector while output is running.

        The channel must already be active in an unchanged, ACK-verified
        configuration. All other fields and channels are preserved. A same-
        value request is transmitted, making A->A a real command control.
        Integer 1..65535 is only a serialization bound, not a manufacturer or
        bench approval. No STOP, START, retry, or implicit activation is sent.

        timeout_s covers the complete exchange, including an optional timing
        marker handshake, subscribe, write and unsubscribe. When supplied,
        timing_marker.mark() must ACK before the BLE write is issued. If a
        transport ignores cancellation, the caller returns
        with an error while the lock remains held until that task finishes.
        An independent STOP supervisor must remain active; cancellation does
        not prove that a pending write never reached the device.
        """
        if type(channel) is not int or not 1 <= channel <= 32:
            raise ValueError("Live update channel must be an integer in 1..32")
        if type(pulse_width) is not int or not 1 <= pulse_width <= 65535:
            raise ValueError("Live SW must be an exact integer in protocol range 1..65535 us")
        if (type(timeout_s) not in (int, float) or not math.isfinite(timeout_s)
                or not 0 < timeout_s <= 10):
            raise ValueError("Live update operation timeout must be >0 and <=10 seconds")
        # Do not queue live transitions behind another indication owner.
        if self._configuration_lock.locked():
            raise RuntimeError("Another Matrix32 configuration/indication transaction is pending")
        await self._configuration_lock.acquire()
        try:
            await self._close_live_frequency_session()
            self._require_connection()
            if not self.running:
                raise RuntimeError("Live width update requires running stimulation")
            if self.read_char_uuid is None:
                raise RuntimeError("Matrix32Device indication characteristic is unavailable")
            if (not self.config_verified
                    or self.config != self.verified_config_snapshot):
                raise RuntimeError("Live width update requires the exact ACK-verified configuration")
            old_config = deepcopy(self.config)
            channels = old_config["sa"]
            widths = old_config["sw"]
            if (any(type(ch) is not int or not 1 <= ch <= 32 for ch in channels)
                    or len(set(channels)) != len(channels)
                    or len(widths) != len(channels)
                    or any(type(w) is not int or not 1 <= w <= 65535 for w in widths)):
                raise ValueError("Verified active-channel widths must be explicit valid integers")
            if channel not in channels:
                raise ValueError("Live width update cannot activate an inactive channel")
            proposed = deepcopy(old_config)
            index = channels.index(channel)
            old_width = widths[index]
            proposed["sw"][index] = pulse_width
            message = next(
                item for item in build_config_messages(proposed)
                if item.startswith(b">SW;"))
        except BaseException:
            self._configuration_lock.release()
            raise

        requested = time.monotonic()
        generation = self._control_generation
        ack_record = {}
        record = {
            "sequence_id": len(self.live_width_records) + 1,
            "command": "SW", "channel": channel,
            "old_width_us": old_width, "new_width_us": pulse_width,
            "same_value": old_width == pulse_width,
            "old_config": old_config, "requested_config": proposed,
            "request_hex": message.hex(" "), "response_hex": None,
            "status": "IN_FLIGHT", "timeout_s": timeout_s,
            "request_monotonic_s": requested,
            "write_begin_monotonic_s": None, "write_return_monotonic_s": None,
            "ack_monotonic_s": None, "completed_monotonic_s": None,
            "elapsed_ms": None, "configuration_verified_after": False,
            "cleanup_pending": False, "ack": ack_record,
            "marker": None, "marker_ack_to_ble_write_ms": None,
        }
        self.live_width_records.append(record)
        self.config_verified = False
        self.verified_config_snapshot = None
        exchange = asyncio.create_task(write_config_with_ack(
            self.client, self.write_char_uuid, self.read_char_uuid, message,
            timeout_s=timeout_s, record=ack_record, marker=timing_marker))
        self._live_width_task = exchange

        def copy_evidence():
            for key in ("response_hex", "write_begin_monotonic_s",
                        "write_return_monotonic_s", "ack_monotonic_s"):
                record[key] = ack_record.get(key)
            record["marker"] = ack_record.get("marker")
            marker_record = record.get("marker")
            marker_ack = (
                marker_record.get("ack_monotonic_s")
                if isinstance(marker_record, dict) else None)
            write_begin = record.get("write_begin_monotonic_s")
            record["marker_ack_to_ble_write_ms"] = (
                round((write_begin - marker_ack) * 1000, 3)
                if marker_ack is not None and write_begin is not None else None)

        def release_exchange(task):
            # Consume the result even after the caller timed out/cancelled.
            try:
                task.result()
            except BaseException:
                pass
            copy_evidence()
            record["cleanup_pending"] = False
            record["exchange_completed_monotonic_s"] = time.monotonic()
            if self._live_width_task is task:
                self._live_width_task = None
            self._configuration_lock.release()

        try:
            done, _pending = await asyncio.wait({exchange}, timeout=timeout_s)
            if not done:
                record["status"] = "TIMEOUT"
                ack_record["abort_requested"] = True
                exchange.cancel()
                raise LiveWidthUpdateError(record)
            try:
                exchange.result()
            except ConfigurationAckError as exc:
                record["status"] = exc.record["status"]
                raise LiveWidthUpdateError(record) from exc
            if generation != self._control_generation:
                record["status"] = "CONTROL_ABORTED"
                raise LiveWidthUpdateError(record)
            if not self.connected:
                record["status"] = "BLE_DISCONNECTED"
                raise LiveWidthUpdateError(record)
            if not self.running:
                record["status"] = "STOPPED_DURING_UPDATE"
                raise LiveWidthUpdateError(record)
            if self.config != old_config:
                record["status"] = "CONFIG_CHANGED_DURING_UPDATE"
                raise LiveWidthUpdateError(record)
            self.config = deepcopy(proposed)
            self.verified_config_snapshot = deepcopy(proposed)
            self.config_verified = True
            record["configuration_verified_after"] = True
            record["status"] = "OK"
            return record
        except asyncio.CancelledError:
            record["status"] = "CANCELLED"
            ack_record["abort_requested"] = True
            exchange.cancel()
            raise
        except LiveWidthUpdateError:
            raise
        except Exception as exc:
            record["status"] = "TRANSPORT_ERROR"
            record["error"] = "{}: {}".format(type(exc).__name__, exc)
            raise LiveWidthUpdateError(record) from exc
        finally:
            copy_evidence()
            record["completed_monotonic_s"] = time.monotonic()
            record["elapsed_ms"] = round(
                (record["completed_monotonic_s"] - requested) * 1000, 3)
            if exchange.done():
                release_exchange(exchange)
            else:
                record["cleanup_pending"] = True
                exchange.add_done_callback(release_exchange)

    def _propose_live_current(self, channel, current_command):
        old_config = deepcopy(self.config)
        channels = old_config["sa"]
        currents = old_config["sc"]
        if (any(type(ch) is not int or not 1 <= ch <= 32 for ch in channels)
                or len(set(channels)) != len(channels)
                or len(currents) != len(channels)
                or any(type(w) is not int or not 0 <= w <= 65535 for w in currents)):
            raise ValueError("Verified active-channel currents must be explicit valid integers")
        if channel not in channels:
            raise ValueError("Live current update cannot activate an inactive channel")
        proposed = deepcopy(old_config)
        index = channels.index(channel)
        old_current = currents[index]
        proposed["sc"][index] = current_command
        message = next(
            item for item in build_config_messages(proposed)
            if item.startswith(b">SC;"))
        return old_config, proposed, message, old_current

    async def apply_live_current(
            self, channel, current_command, *, timeout_s=2.0, timing_marker=None):
        """Send one ACK-required 32-current SC vector during running output.

        The channel must already be active in an unchanged, ACK-verified
        configuration. All other fields and channels are preserved. A same-
        value request is transmitted, making A->A a real command control.
        Integer 0..65535 is ONLY a protocol serialization bound. SC=0 while
        running is unqualified: ACK does not prove physical zero output.
        No STOP, START, retry, or implicit activation is sent.

        The default transport timeout covers the complete exchange, including an optional timing
        marker handshake, subscribe, write and unsubscribe. When supplied,
        timing_marker.mark() must ACK before the BLE write is issued. If a
        transport ignores cancellation, the caller returns
        with an error while the lock remains held until that task finishes.
        Experimental shared mode uses the persistent SF transport and its guard\n        and exchange timeout semantics.\n        An independent STOP supervisor must remain active; cancellation does
        not prove that a pending write never reached the device.
        """
        if type(channel) is not int or not 1 <= channel <= 32:
            raise ValueError("Live update channel must be an integer in 1..32")
        if type(current_command) is not int or not 0 <= current_command <= 65535:
            raise ValueError("Live SC must be an integer 0..65535 (zero is bench-only)")
        if (type(timeout_s) not in (int, float) or not math.isfinite(timeout_s)
                or not 0 < timeout_s <= 10):
            raise ValueError("Live update operation timeout must be >0 and <=10 seconds")
        if self.experimental_shared_sf_sc:
            return await self._apply_live_persistent_config(
                command="SC", value=current_command, channel=channel,
                timeout_s=timeout_s, timing_marker=timing_marker,
                min_ack_to_write_ms=LIVE_FREQUENCY_MIN_ACK_TO_WRITE_MS)
        # Do not queue live transitions behind another indication owner.
        if self._configuration_lock.locked():
            raise RuntimeError("Another Matrix32 configuration/indication transaction is pending")
        await self._configuration_lock.acquire()
        try:
            await self._close_live_frequency_session()
            self._require_connection()
            if not self.running:
                raise RuntimeError("Live current update requires running stimulation")
            if self.read_char_uuid is None:
                raise RuntimeError("Matrix32Device indication characteristic is unavailable")
            if (not self.config_verified
                    or self.config != self.verified_config_snapshot):
                raise RuntimeError("Live current update requires the exact ACK-verified configuration")
            old_config, proposed, message, old_current = self._propose_live_current(
                channel, current_command)
        except BaseException:
            self._configuration_lock.release()
            raise

        requested = time.monotonic()
        generation = self._control_generation
        ack_record = {}
        record = {
            "sequence_id": len(self.live_current_records) + 1,
            "command": "SC", "channel": channel,
            "old_current_command": old_current, "new_current_command": current_command,
            "same_value": old_current == current_command,
            "old_config": old_config, "requested_config": proposed,
            "request_hex": message.hex(" "), "response_hex": None,
            "status": "IN_FLIGHT", "timeout_s": timeout_s,
            "request_monotonic_s": requested,
            "write_begin_monotonic_s": None, "write_return_monotonic_s": None,
            "ack_monotonic_s": None, "completed_monotonic_s": None,
            "elapsed_ms": None, "configuration_verified_after": False,
            "cleanup_pending": False, "ack": ack_record,
            "marker": None, "marker_ack_to_ble_write_ms": None,
        }
        self.live_current_records.append(record)
        self.config_verified = False
        self.verified_config_snapshot = None
        exchange = asyncio.create_task(write_config_with_ack(
            self.client, self.write_char_uuid, self.read_char_uuid, message,
            timeout_s=timeout_s, record=ack_record, marker=timing_marker))
        self._live_current_task = exchange

        def copy_evidence():
            for key in ("response_hex", "write_begin_monotonic_s",
                        "write_return_monotonic_s", "ack_monotonic_s"):
                record[key] = ack_record.get(key)
            record["marker"] = ack_record.get("marker")
            marker_record = record.get("marker")
            marker_ack = (
                marker_record.get("ack_monotonic_s")
                if isinstance(marker_record, dict) else None)
            write_begin = record.get("write_begin_monotonic_s")
            record["marker_ack_to_ble_write_ms"] = (
                round((write_begin - marker_ack) * 1000, 3)
                if marker_ack is not None and write_begin is not None else None)

        def release_exchange(task):
            # Consume the result even after the caller timed out/cancelled.
            try:
                task.result()
            except BaseException:
                pass
            copy_evidence()
            record["cleanup_pending"] = False
            record["exchange_completed_monotonic_s"] = time.monotonic()
            if self._live_current_task is task:
                self._live_current_task = None
            self._configuration_lock.release()

        try:
            done, _pending = await asyncio.wait({exchange}, timeout=timeout_s)
            if not done:
                record["status"] = "TIMEOUT"
                ack_record["abort_requested"] = True
                exchange.cancel()
                raise LiveCurrentUpdateError(record)
            try:
                exchange.result()
            except ConfigurationAckError as exc:
                record["status"] = exc.record["status"]
                raise LiveCurrentUpdateError(record) from exc
            if generation != self._control_generation:
                record["status"] = "CONTROL_ABORTED"
                raise LiveCurrentUpdateError(record)
            if not self.connected:
                record["status"] = "BLE_DISCONNECTED"
                raise LiveCurrentUpdateError(record)
            if not self.running:
                record["status"] = "STOPPED_DURING_UPDATE"
                raise LiveCurrentUpdateError(record)
            if self.config != old_config:
                record["status"] = "CONFIG_CHANGED_DURING_UPDATE"
                raise LiveCurrentUpdateError(record)
            self.config = deepcopy(proposed)
            self.verified_config_snapshot = deepcopy(proposed)
            self.config_verified = True
            record["configuration_verified_after"] = True
            record["status"] = "OK"
            return record
        except asyncio.CancelledError:
            record["status"] = "CANCELLED"
            ack_record["abort_requested"] = True
            exchange.cancel()
            raise
        except LiveCurrentUpdateError:
            raise
        except Exception as exc:
            record["status"] = "TRANSPORT_ERROR"
            record["error"] = "{}: {}".format(type(exc).__name__, exc)
            raise LiveCurrentUpdateError(record) from exc
        finally:
            copy_evidence()
            record["completed_monotonic_s"] = time.monotonic()
            record["elapsed_ms"] = round(
                (record["completed_monotonic_s"] - requested) * 1000, 3)
            if exchange.done():
                release_exchange(exchange)
            else:
                record["cleanup_pending"] = True
                exchange.add_done_callback(release_exchange)

    async def apply_live_frequency(
            self, hz, *, timeout_s=2.0, timing_marker=None,
            min_ack_to_write_ms=LIVE_FREQUENCY_MIN_ACK_TO_WRITE_MS,
            transport="optimized_persistent"):
        """Apply one verified live SF update over the optimized persistent path.

        Consecutive calls reuse one indication subscription and send the short
        SF frame with ATT Write Command. Exactly one command may be outstanding
        and one unique Matrix32 application-level >OK< indication is mandatory
        before the call succeeds. The next SF write is held until at least
        7.5 ms after the previous SF ACK; callers may request a larger guard but
        cannot request a smaller one through this qualified API.

        The current configuration must already be ACK-verified and stimulation
        must be running. Same-value requests are transmitted. No STOP, START,
        retry, pipelining, or unrelated configuration write is sent.

        timing_marker, when supplied, is emitted after the post-ACK guard and
        before the SF write. It is diagnostic and therefore adds latency; the
        qualified ~44 Hz path does not use a per-command marker.

        timeout_s bounds the command exchange after any required guard/marker.
        transport="legacy_per_command_subscription" is retained only so closed
        S05/S06-F1 experiment runners can reproduce their original transport
        semantics; ordinary callers should use the default optimized path.

        If a transport does not respond promptly to cancellation, this method
        can return an error while the configuration lock remains held until the
        exchange cleanup finishes. An independent STOP supervisor remains
        required for bounded-output experiments.
        """
        if type(hz) is not int or not 1 <= hz <= 400:
            raise ValueError(
                "Live SF must be an exact integer in manufacturer range 1..400 Hz")
        if (type(timeout_s) not in (int, float) or not math.isfinite(timeout_s)
                or not 0 < timeout_s <= 10):
            raise ValueError(
                "Live update operation timeout must be >0 and <=10 seconds")
        if transport == "legacy_per_command_subscription":
            return await self._apply_live_frequency_legacy(
                hz, timeout_s=timeout_s, timing_marker=timing_marker)
        if transport != "optimized_persistent":
            raise ValueError(
                "Live SF transport must be 'optimized_persistent' or "
                "'legacy_per_command_subscription'")
        if (type(min_ack_to_write_ms) not in (int, float)
                or not math.isfinite(min_ack_to_write_ms)
                or not LIVE_FREQUENCY_MIN_ACK_TO_WRITE_MS
                <= min_ack_to_write_ms
                <= LIVE_FREQUENCY_MAX_ACK_TO_WRITE_MS):
            raise ValueError(
                "Live SF ACK-to-next-write guard must be 7.5..5000 ms")
        return await self._apply_live_persistent_config(
            command="SF", value=hz, timeout_s=timeout_s,
            timing_marker=timing_marker, min_ack_to_write_ms=min_ack_to_write_ms)

    async def _apply_live_persistent_config(
            self, *, command, value, timeout_s, timing_marker,
            min_ack_to_write_ms, channel=None):
        """One shared persistent exchange path; SC is opt-in and unqualified."""
        if command not in ("SF", "SC"):
            raise ValueError("Unsupported persistent live command")
        is_sf = command == "SF"
        error_type = LiveFrequencyUpdateError if is_sf else LiveCurrentUpdateError
        records = self.live_frequency_records if is_sf else self.live_current_records
        if self._configuration_lock.locked():
            raise RuntimeError(
                "Another Matrix32 configuration/indication transaction is pending")
        await self._configuration_lock.acquire()

        record = None
        session = None
        exchange = None
        defer_release = False
        failed = False

        try:
            self._require_connection()
            if not self.running:
                raise RuntimeError(
                    "Live frequency update requires running stimulation")
            if self.read_char_uuid is None:
                raise RuntimeError(
                    "Matrix32Device indication characteristic is unavailable")
            if (not self.config_verified
                    or self.config != self.verified_config_snapshot):
                raise RuntimeError(
                    "Live frequency update requires the exact "
                    "ACK-verified configuration")

            if is_sf:
                old_config = deepcopy(self.config)
                old_frequency = old_config["sf"]
                proposed = deepcopy(old_config)
                proposed["sf"] = value
                message = next(
                    item for item in build_config_messages(proposed)
                    if item.startswith(b">SF;"))
                details = {"old_frequency_hz": old_frequency,
                           "new_frequency_hz": value,
                           "same_value": old_frequency == value}
            else:
                old_config, proposed, message, old_current = self._propose_live_current(
                    channel, value)
                details = {"channel": channel, "old_current_command": old_current,
                           "new_current_command": value, "same_value": old_current == value}

            requested = time.monotonic()
            generation = self._control_generation
            previous_ack = self._live_frequency_last_ack_monotonic_s
            previous_write_begin = (
                self._live_frequency_last_write_begin_monotonic_s)
            if self.experimental_shared_sf_sc:
                # Apply the guard after ANY prior mixed command ACK.
                previous_ack = self._live_config_last_ack_monotonic_s
                previous_write_begin = self._live_config_last_write_begin_monotonic_s
            ack_record = {}
            record = {
                "sequence_id": len(records) + 1,
                "command": command,
                **details,
                "old_config": old_config,
                "requested_config": proposed,
                "request_hex": message.hex(" "),
                "response_hex": None,
                "status": "IN_FLIGHT",
                "timeout_s": timeout_s,
                "request_monotonic_s": requested,
                "write_begin_monotonic_s": None,
                "write_return_monotonic_s": None,
                "ack_monotonic_s": None,
                "completed_monotonic_s": None,
                "elapsed_ms": None,
                "configuration_verified_after": False,
                "cleanup_pending": False,
                "ack": ack_record,
                "marker": None,
                "marker_ack_to_ble_write_ms": None,
                "persistent_subscription": True,
                "persistent_session_reused": None,
                "transport": ("experimental_shared_sf_sc" if self.experimental_shared_sf_sc
                              else "optimized_persistent"),
                "subscribe_begin_monotonic_s": None,
                "subscribe_return_monotonic_s": None,
                "write_procedure": "ATT_WRITE_COMMAND",
                "min_ack_to_write_ms": float(min_ack_to_write_ms),
                "previous_ack_monotonic_s": previous_ack,
                "previous_write_begin_monotonic_s": previous_write_begin,
                "guard_sleep_requested_ms": 0.0,
                "previous_ack_to_write_begin_ms": None,
                "inter_write_begin_ms": None,
            }
            records.append(record)
            self._live_persistent_record = record
            self.config_verified = False
            self.verified_config_snapshot = None

            session, reused = await self._ensure_live_frequency_session()
            record["persistent_session_reused"] = reused
            record["subscribe_begin_monotonic_s"] = (
                session.subscribe_begin_monotonic_s)
            record["subscribe_return_monotonic_s"] = (
                session.subscribe_return_monotonic_s)

            if previous_ack is not None:
                guard_deadline = (
                    previous_ack + float(min_ack_to_write_ms) / 1000.0)
                remaining = max(0.0, guard_deadline - time.monotonic())
                record["guard_sleep_requested_ms"] = round(
                    remaining * 1000.0, 3)
                if remaining > 0:
                    await asyncio.sleep(remaining)

            self._check_control_generation(generation)
            if timing_marker is not None:
                try:
                    marker_record = timing_marker.mark()
                    if (not isinstance(marker_record, dict)
                            or marker_record.get("status") != "ACK"):
                        raise RuntimeError(
                            "Timing marker did not return an ACK record")
                    record["marker"] = marker_record
                except Exception as exc:
                    record["status"] = "MARKER_ERROR"
                    record["marker_error"] = "{}: {}".format(
                        type(exc).__name__, exc)
                    failed = True
                    raise error_type(record) from exc

            self._check_control_generation(generation)
            exchange = asyncio.create_task(session.send(
                message,
                timeout_s=timeout_s,
                record=ack_record,
                write_response=False,
            ))
            self._live_frequency_task = exchange

            def copy_evidence():
                for key in (
                    "response_hex", "write_begin_monotonic_s",
                    "write_return_monotonic_s", "ack_monotonic_s",
                ):
                    record[key] = ack_record.get(key)
                write_begin = record.get("write_begin_monotonic_s")
                if previous_ack is not None and write_begin is not None:
                    record["previous_ack_to_write_begin_ms"] = round(
                        (write_begin - previous_ack) * 1000.0, 3)
                if previous_write_begin is not None and write_begin is not None:
                    record["inter_write_begin_ms"] = round(
                        (write_begin - previous_write_begin) * 1000.0, 3)
                marker_record = record.get("marker")
                marker_ack = (
                    marker_record.get("ack_monotonic_s")
                    if isinstance(marker_record, dict) else None)
                record["marker_ack_to_ble_write_ms"] = (
                    round((write_begin - marker_ack) * 1000.0, 3)
                    if marker_ack is not None and write_begin is not None
                    else None)

            async def deferred_cleanup():
                try:
                    try:
                        await exchange
                    except BaseException:
                        pass
                    copy_evidence()
                    try:
                        if self._live_frequency_session is session:
                            await self._close_live_frequency_session()
                    except Exception as exc:
                        record["session_close_error"] = "{}: {}".format(
                            type(exc).__name__, exc)
                finally:
                    record["cleanup_pending"] = False
                    record["exchange_completed_monotonic_s"] = time.monotonic()
                    if self._live_frequency_task is exchange:
                        self._live_frequency_task = None
                    self._live_persistent_record = None
                    if self._live_frequency_cleanup_task is asyncio.current_task():
                        self._live_frequency_cleanup_task = None
                    if self._configuration_lock.locked():
                        self._configuration_lock.release()

            done, _pending = await asyncio.wait({exchange}, timeout=timeout_s)
            if not done:
                record["status"] = "TIMEOUT"
                ack_record["abort_requested"] = True
                exchange.cancel()
                record["cleanup_pending"] = True
                failed = True
                defer_release = True
                self._live_frequency_cleanup_task = asyncio.create_task(
                    deferred_cleanup())
                raise error_type(record)

            try:
                exchange.result()
            except ConfigurationAckError as exc:
                record["status"] = exc.record["status"]
                failed = True
                raise error_type(record) from exc

            copy_evidence()
            if generation != self._control_generation:
                record["status"] = "CONTROL_ABORTED"
                failed = True
                raise error_type(record)
            if not self.connected:
                record["status"] = "BLE_DISCONNECTED"
                failed = True
                raise error_type(record)
            if not self.running:
                record["status"] = "STOPPED_DURING_UPDATE"
                failed = True
                raise error_type(record)
            if self.config != old_config:
                record["status"] = "CONFIG_CHANGED_DURING_UPDATE"
                failed = True
                raise error_type(record)

            self.config = deepcopy(proposed)
            self.verified_config_snapshot = deepcopy(proposed)
            self.config_verified = True
            if is_sf:
                self._live_frequency_last_ack_monotonic_s = record["ack_monotonic_s"]
                self._live_frequency_last_write_begin_monotonic_s = record["write_begin_monotonic_s"]
            self._live_config_last_ack_monotonic_s = record["ack_monotonic_s"]
            self._live_config_last_write_begin_monotonic_s = record["write_begin_monotonic_s"]
            record["configuration_verified_after"] = True
            record["status"] = "OK"
            return record

        except asyncio.CancelledError:
            if record is not None:
                record["status"] = "CANCELLED"
                record["ack"]["abort_requested"] = True
            failed = True
            if exchange is not None and not exchange.done():
                exchange.cancel()
                if record is not None:
                    record["cleanup_pending"] = True
                defer_release = True
                self._live_frequency_cleanup_task = asyncio.create_task(
                    deferred_cleanup())
            raise
        except (LiveFrequencyUpdateError, LiveCurrentUpdateError):
            raise
        except Exception as exc:
            if record is None:
                raise
            record["status"] = "TRANSPORT_ERROR"
            record["error"] = "{}: {}".format(type(exc).__name__, exc)
            failed = True
            raise error_type(record) from exc
        finally:
            if record is not None:
                if exchange is not None and exchange.done():
                    for key in (
                        "response_hex", "write_begin_monotonic_s",
                        "write_return_monotonic_s", "ack_monotonic_s",
                    ):
                        record[key] = record["ack"].get(key)
                    previous_ack = record.get("previous_ack_monotonic_s")
                    write_begin = record.get("write_begin_monotonic_s")
                    if previous_ack is not None and write_begin is not None:
                        record["previous_ack_to_write_begin_ms"] = round(
                            (write_begin - previous_ack) * 1000.0, 3)
                    previous_write_begin = record.get(
                        "previous_write_begin_monotonic_s")
                    if (previous_write_begin is not None
                            and write_begin is not None):
                        record["inter_write_begin_ms"] = round(
                            (write_begin - previous_write_begin) * 1000.0, 3)
                record["completed_monotonic_s"] = time.monotonic()
                record["elapsed_ms"] = round(
                    (record["completed_monotonic_s"]
                     - record["request_monotonic_s"]) * 1000.0, 3)

            if not defer_release:
                if self._live_frequency_task is exchange:
                    self._live_frequency_task = None
                self._live_persistent_record = None
                if failed and self._live_frequency_session is not None:
                    try:
                        await self._close_live_frequency_session()
                    except Exception as exc:
                        if record is not None:
                            record["session_close_error"] = "{}: {}".format(
                                type(exc).__name__, exc)
                if self._configuration_lock.locked():
                    self._configuration_lock.release()

    async def _apply_live_frequency_legacy(
            self, hz, *, timeout_s=2.0, timing_marker=None):
        """Historical per-command subscribe/write/ACK/unsubscribe SF path.

        Kept for reproducibility of closed S05 and original S06-F1 protocols.
        New application code should use apply_live_frequency() with its default
        optimized persistent transport.
        """
        if self._configuration_lock.locked():
            raise RuntimeError(
                "Another Matrix32 configuration/indication transaction is pending")
        await self._configuration_lock.acquire()
        try:
            await self._close_live_frequency_session()
            self._require_connection()
            if not self.running:
                raise RuntimeError(
                    "Live frequency update requires running stimulation")
            if self.read_char_uuid is None:
                raise RuntimeError(
                    "Matrix32Device indication characteristic is unavailable")
            if (not self.config_verified
                    or self.config != self.verified_config_snapshot):
                raise RuntimeError(
                    "Live frequency update requires the exact "
                    "ACK-verified configuration")
            old_config = deepcopy(self.config)
            old_frequency = old_config["sf"]
            proposed = deepcopy(old_config)
            proposed["sf"] = hz
            message = next(
                item for item in build_config_messages(proposed)
                if item.startswith(b">SF;"))
        except BaseException:
            self._configuration_lock.release()
            raise

        requested = time.monotonic()
        generation = self._control_generation
        ack_record = {}
        record = {
            "sequence_id": len(self.live_frequency_records) + 1,
            "command": "SF",
            "old_frequency_hz": old_frequency,
            "new_frequency_hz": hz,
            "same_value": old_frequency == hz,
            "old_config": old_config,
            "requested_config": proposed,
            "request_hex": message.hex(" "),
            "response_hex": None,
            "status": "IN_FLIGHT",
            "timeout_s": timeout_s,
            "request_monotonic_s": requested,
            "write_begin_monotonic_s": None,
            "write_return_monotonic_s": None,
            "ack_monotonic_s": None,
            "completed_monotonic_s": None,
            "elapsed_ms": None,
            "configuration_verified_after": False,
            "cleanup_pending": False,
            "ack": ack_record,
            "marker": None,
            "marker_ack_to_ble_write_ms": None,
            "persistent_subscription": False,
            "persistent_session_reused": False,
            "write_procedure": "ATT_WRITE_COMMAND",
            "transport": "legacy_per_command_subscription",
        }
        self.live_frequency_records.append(record)
        self.config_verified = False
        self.verified_config_snapshot = None
        exchange = asyncio.create_task(write_config_with_ack(
            self.client, self.write_char_uuid, self.read_char_uuid, message,
            timeout_s=timeout_s, record=ack_record, marker=timing_marker))
        self._live_frequency_task = exchange

        def copy_evidence():
            for key in (
                "response_hex", "write_begin_monotonic_s",
                "write_return_monotonic_s", "ack_monotonic_s",
            ):
                record[key] = ack_record.get(key)
            record["marker"] = ack_record.get("marker")
            marker_record = record.get("marker")
            marker_ack = (
                marker_record.get("ack_monotonic_s")
                if isinstance(marker_record, dict) else None)
            write_begin = record.get("write_begin_monotonic_s")
            record["marker_ack_to_ble_write_ms"] = (
                round((write_begin - marker_ack) * 1000, 3)
                if marker_ack is not None and write_begin is not None else None)

        def release_exchange(task):
            try:
                task.result()
            except BaseException:
                pass
            copy_evidence()
            record["cleanup_pending"] = False
            record["exchange_completed_monotonic_s"] = time.monotonic()
            if self._live_frequency_task is task:
                self._live_frequency_task = None
            self._configuration_lock.release()

        try:
            done, _pending = await asyncio.wait({exchange}, timeout=timeout_s)
            if not done:
                record["status"] = "TIMEOUT"
                ack_record["abort_requested"] = True
                exchange.cancel()
                raise LiveFrequencyUpdateError(record)
            try:
                exchange.result()
            except ConfigurationAckError as exc:
                record["status"] = exc.record["status"]
                raise LiveFrequencyUpdateError(record) from exc
            if generation != self._control_generation:
                record["status"] = "CONTROL_ABORTED"
                raise LiveFrequencyUpdateError(record)
            if not self.connected:
                record["status"] = "BLE_DISCONNECTED"
                raise LiveFrequencyUpdateError(record)
            if not self.running:
                record["status"] = "STOPPED_DURING_UPDATE"
                raise LiveFrequencyUpdateError(record)
            if self.config != old_config:
                record["status"] = "CONFIG_CHANGED_DURING_UPDATE"
                raise LiveFrequencyUpdateError(record)
            self.config = deepcopy(proposed)
            self.verified_config_snapshot = deepcopy(proposed)
            self.config_verified = True
            record["configuration_verified_after"] = True
            record["status"] = "OK"
            return record
        except asyncio.CancelledError:
            record["status"] = "CANCELLED"
            ack_record["abort_requested"] = True
            exchange.cancel()
            raise
        except LiveFrequencyUpdateError:
            raise
        except Exception as exc:
            record["status"] = "TRANSPORT_ERROR"
            record["error"] = "{}: {}".format(type(exc).__name__, exc)
            raise LiveFrequencyUpdateError(record) from exc
        finally:
            copy_evidence()
            record["completed_monotonic_s"] = time.monotonic()
            record["elapsed_ms"] = round(
                (record["completed_monotonic_s"] - requested) * 1000, 3)
            if exchange.done():
                release_exchange(exchange)
            else:
                record["cleanup_pending"] = True
                exchange.add_done_callback(release_exchange)

    async def probe_frequency_ack(self, hz):
        """Send ONLY SF, never START; caller must verify device is physically idle."""
        async with self._configuration_lock:
            return await self._probe_frequency_ack(hz)

    async def _probe_frequency_ack(self, hz):
        await self._close_live_frequency_session()
        self._require_connection()
        if self.running:
            raise RuntimeError("Cannot probe SF while running")
        if not isinstance(hz, int) or not 1 <= hz <= 400:
            raise ValueError("Manufacturer SF range is integer 1..400 Hz")
        self.config_verified = False
        self.verified_config_snapshot = None
        self.config_ack_records = []
        message = b">SF;" + hz.to_bytes(2, "big") + b"<"
        try:
            record = await write_config_with_ack(
                self.client, self.write_char_uuid, self.read_char_uuid, message)
        except ConfigurationAckError as exc:
            self.config_ack_records.append(exc.record)
            raise
        self.config_ack_records.append(record)
        return record

    async def start(self):
        generation = self._control_generation
        async with self._configuration_lock:
            try:
                return await self._start(generation)
            except asyncio.CancelledError:
                self.abort_pending_commands()
                raise

    async def _start(self, generation):
        await self._close_live_frequency_session()
        self._check_control_generation(generation)
        self._require_connection()
        # S02 evidence-based safety restriction, separate from the vendor's
        # documented 1..400-Hz configuration range. Keep non-stimulating SF1
        # acknowledgement diagnostics possible; never enable output at SF1.
        if self.config.get("sf") == 1:
            raise RuntimeError(
                "START blocked: SF=1 Hz is experimentally anomalous. "
                "Three S02 runs show ~7.924751-Hz provisional pulse candidates "
                "despite positive firmware ACKs. See "
                "experiments/S02_1HZ_BOUNDARY_INVESTIGATION_2026-10-21.md."
            )
        if not self.config_verified or self.config != self.verified_config_snapshot:
            raise RuntimeError("START blocked: configuration ACKs not verified for the current settings; use apply(require_ack=True)")
        # Check between writes so a delayed ON completion cannot send a new S
        # after the independent STOP supervisor invalidated this operation.
        # Even a failed return from ON can mean output enable reached hardware.
        self._stop_cleanup_needed = True
        await self.client.write_gatt_char(self.write_char_uuid, b">ON<")
        self._check_control_generation(generation)
        self._require_connection()
        await self.client.write_gatt_char(self.write_char_uuid, b">S<")
        self._check_control_generation(generation)
        self._require_connection()
        self.running = True
        print("Stimulation is started.")

    async def stop(self):
        self.abort_pending_commands()
        self._require_connection()
        try:
            await stim_stop(self.client, self.write_char_uuid)
            self._stop_cleanup_needed = False
            self.running = False
        finally:
            # STOP must not wait for the configuration lock. If a persistent exchange
            # is still in flight, do not unsubscribe underneath it; generation
            # invalidation plus the exchange's own failure/timeout cleanup owns
            # that teardown. An idle persistent subscription can be released
            # immediately after the independent stop command path.
            live_sf_in_flight = (
                self._live_frequency_task is not None
                and not self._live_frequency_task.done())
            if not live_sf_in_flight:
                try:
                    await self._close_live_frequency_session()
                except Exception as exc:
                    print(
                        "Could not close persistent live-frequency indication "
                        "session after STOP: {}: {}".format(
                            type(exc).__name__, exc))

    async def set_and_apply_channel(self, channel, current=None, pulse_width=None, active=True):
        self.set_channel(
            channel,
            current=current,
            pulse_width=pulse_width,
            active=active,
        )
        await self.apply()

    @staticmethod
    def _validate_channel(channel):
        if not 1 <= channel <= 32:
            raise ValueError(f"Channel must be in range 1..32, got {channel}.")

    def _require_connection(self):
        if (
            self.client is None
            or not self.connected
            or self.write_char_uuid is None
        ):
            raise RuntimeError("Matrix32Device is not connected.")
