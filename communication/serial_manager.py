"""
Serial communication manager for MCU data reception.

Handles binary protocol communication with the microcontroller over UART,
parsing taxel pressure values, neuromorphic events, and rate snapshots.

Protocol format (framed, CRC-protected):
- length(2, BE) + type(1) + payload(N) + crc32(4, BE)
- type=0x02: Taxel payload = timestamp(4, BE) + 12 pressure bytes
- type=0x04: Events payload = reference_timestamp(4, BE) + repeated [ID0(1), ID1(1), offset_ms(2, BE)]
- type=0x05: Rates payload = timestamp(4, BE) + taxel[12](u8) + RA[12](u32 BE, centi-Hz) + SA[12](u32 BE, centi-Hz)
- type=0x10: Command payload = command_id(1) [+ value_u16(2, BE)]
- type=0x11: ACK payload = command_id(1) + status(1) + applied_value_u16(2, BE)

The rest of the GUI uses logical command strings, but SerialManager encodes
those commands into framed binary packets before transmission.

Author: Simon F. Muller-Cleve
Date: 2026-03-30
"""


import logging
import struct
import time
import zlib
import threading
import serial
import queue

from typing import Optional

from communication.clock_compensator import (
    ClockCompensationSettings,
    ClockCompensator
)


class SerialManager():
    """Manages serial communication with MCU in a separate thread.

    Continuously reads and parses binary data packets from the MCU,
    emitting Qt signals when new data is received.

    Signals:
        dataInSkin (list, int): Emitted when taxel data received.
            Args: List of 12 pressure values (0-255), timestamp in ms.
        dataInSkinWithPcTime (list, int, object): Optional raw taxel timing sample.
        dataInEvents (list): Emitted when event data received.
            Args: List of tuples (time_ms, ID0, ID1).
        commandAckReceived (int, int, int): Emitted when MCU acknowledges a command.
            Args: command id, status byte, applied value.
        linkReadyForCommands (): Emitted after the first valid telemetry frame is parsed.

    Attributes:
        ser (serial.Serial): Serial port connection object.
    """

    SHUTDOWN_TIMEOUT_MS = 2000
    FRAME_TYPE_TAXEL = 0x02
    FRAME_TYPE_EVENTS = 0x04
    FRAME_TYPE_TACTILE_RATES = 0x05
    TACTILE_RATES_PAYLOAD_SIZE = 112
    FRAME_TYPE_COMMAND = 0x10
    FRAME_TYPE_ACK = 0x11

    CMD_RA_THR = 0x01
    CMD_REF = 0x02
    CMD_SA_FREQ = 0x03
    CMD_GET_CONFIG = 0x04

    MAX_FRAME_LENGTH = 257  # type(1) + payload(0..256)

    def __init__(self, config: Optional[dict] = None, logger=None) -> None:
        """
        Initialize the SerialManager with default settings and optional config.

        Args:
            config (dict, optional): Configuration dictionary for serial settings.
            logger (logging.Logger, optional): Logger instance. Defaults to None.
        """
        
        self._stop_event = threading.Event()
        self._thread = None

        # Data callbacks
        # Thread-safe communication queue
        self.data_queue = queue.Queue()
        self._latest_rates_lock = threading.Lock()
        self._latest_rates_snapshot = None

        self._is_running: bool = False
        self._safe_close: bool = True
        self.ser: serial.Serial = serial.Serial()
        self.logger = logger or logging.getLogger(__name__)
        self.config = config or {}
        self.baudrate = self._get_baudrate_from_config()
        self._rx_buffer = bytearray()
        self._resynchronizing = False
        self._link_ready_emitted = False
        self._taxel_data_received = False
        self._emit_pc_receive_time = self._clock_drift_monitor_enabled_from_config()
        self._clock_compensator = ClockCompensator(logger=self.logger)
        self._clock_compensation_sample_source = "taxel"
        self._capture_pc_receive_time_for_compensation = False
        self._update_clock_time_features_from_config()

    def set_config(self, config: dict) -> None:
        """
        Set the configuration dictionary and update baudrate.

        Args:
            config (dict): Configuration dictionary. Should contain serial settings.
        """
        self.config = config
        self.baudrate = self._get_baudrate_from_config()
        self._emit_pc_receive_time = self._clock_drift_monitor_enabled_from_config()
        self._update_clock_time_features_from_config()

    def _update_clock_time_features_from_config(self) -> None:
        comp_config = self.config.get("clock_compensation", {}) if isinstance(self.config, dict) else {}
        if not isinstance(comp_config, dict):
            comp_config = {}

        settings = ClockCompensationSettings(
            enabled=bool(comp_config.get("enabled", False)),
            initial_ppm=float(comp_config.get("initial_ppm", 3625.0)),
            adaptive_enabled=bool(comp_config.get("adaptive_enabled", True)),
            sample_source=str(comp_config.get("sample_source", "taxel")).strip().lower(),
            update_interval_samples=max(1, int(comp_config.get("update_interval_samples", 2000))),
            min_fit_duration_ms=max(1, int(comp_config.get("min_fit_duration_ms", 30000))),
            min_fit_samples=max(2, int(comp_config.get("min_fit_samples", 5000))),
            min_fit_r2=max(0.0, min(1.0, float(comp_config.get("min_fit_r2", 0.995)))),
            ema_alpha=max(0.0, min(1.0, float(comp_config.get("ema_alpha", 0.15)))),
            max_step_ppm_per_update=float(comp_config.get("max_step_ppm_per_update", 100.0)),
            log_updates=bool(comp_config.get("log_updates", True)),
        )
        self._clock_compensator.configure(settings)
        self._clock_compensation_sample_source = settings.sample_source
        self._capture_pc_receive_time_for_compensation = settings.enabled and settings.adaptive_enabled and settings.sample_source == "taxel"

    def _clock_drift_monitor_enabled_from_config(self) -> bool:
        monitor_config = self.config.get("clock_drift_monitor", {}) if isinstance(self.config, dict) else {}
        if not isinstance(monitor_config, dict):
            return False
        mode = str(monitor_config.get("mode", "off")).strip().lower()
        return mode == "monitor"

    def _get_baudrate_from_config(self) -> int:
        """
        Retrieve baudrate from configuration dictionary.

        Returns:
            int: Baudrate value from config, or 115200 if not found or invalid.
        """
        try:
            return int(self.config.get('serial', {}).get('input_baud_rate', 115200))
        except (TypeError, ValueError):
            return 115200

    def is_running(self) -> bool:
        """Check if serial communication thread is active.

        Returns:
            bool: True if thread is running, False otherwise.
        """
        return self._is_running

    def get_latest_rates_snapshot(self):
        """Return the newest complete rate snapshot, or None (thread-safe).

        The recording queue remains lossless; this overwriting slot is for
        future stimulation consumers that must not replay stale samples.
        """
        with self._latest_rates_lock:
            if self._latest_rates_snapshot is None:
                return None
            snapshot = self._latest_rates_snapshot
            return {**snapshot,
                    "taxels": snapshot["taxels"].copy(),
                    "ra_rates_hz": snapshot["ra_rates_hz"].copy(),
                    "sa_rates_hz": snapshot["sa_rates_hz"].copy()}

    def has_received_telemetry(self) -> bool:
        """Return whether a valid taxel or event frame arrived this session."""
        return self._link_ready_emitted

    def has_received_taxel_data(self) -> bool:
        """Return whether a valid taxel frame arrived this session."""
        return self._taxel_data_received

    def stop_serial(self):
        self._stop_event.set()

        if self._thread and self._thread.is_alive():

            self._thread.join(timeout=2.0)

            if self._thread.is_alive():
                self.logger.error(
                    "Serial thread did not stop within timeout"
                )

        self._is_running = False

        self._link_ready_emitted = False
        self._taxel_data_received = False
        with self._latest_rates_lock:
            self._latest_rates_snapshot = None

    def start_comm(self, port: str):
        if self._thread and self._thread.is_alive():
            self.logger.warning("Serial communication already running")
            return

        try:
            self.ser = serial.Serial(
                port,
                self.baudrate,
                timeout=0.1
            )

            self.logger.info(
                f"Opened serial port {port} at {self.baudrate}"
            )

        except (ValueError, OSError, serial.SerialException) as e:
            self.logger.error(f"Cannot open serial port: {e}")
            return

        self._stop_event.clear()

        self._rx_buffer.clear()
        self._resynchronizing = False
        self._link_ready_emitted = False
        self._taxel_data_received = False
        with self._latest_rates_lock:
            self._latest_rates_snapshot = None

        self._clock_compensator.reset_session()

        self._is_running = True

        self._thread = threading.Thread(
            target=self.run,
            daemon=True
        )

        self._thread.start()

    def run(self):
        try:

            while not self._stop_event.is_set():

                try:

                    pending_bytes = getattr(
                        self.ser,
                        "in_waiting",
                        0
                    )

                    read_size = max(1, int(pending_bytes or 0))

                    chunk = self.ser.read(read_size)

                    if not chunk:
                        continue

                    self._rx_buffer.extend(chunk)

                    self._process_rx_buffer(time.monotonic_ns())

                except (
                    OSError,
                    serial.SerialException,
                    struct.error,
                    ValueError,
                    TypeError
                ) as e:

                    if self._stop_event.is_set():
                        break

                    self.logger.exception(
                        "Exception in serial read loop: %s", e
                    )

        finally:

            self._is_running = False

            try:
                self.ser.close()
                self.logger.info("Serial port closed.")

            except (AttributeError, OSError, serial.SerialException) as e:
                self.logger.error(f"Error closing serial port: {e}")

    def _find_valid_frame_offset(self):
        """Locate a complete CRC-valid frame after losing frame alignment.

        An invalid packet can contain plausible length headers. Returning
        early for one such *incomplete* candidate would leave subsequent valid
        frames stuck in the buffer. A CRC-verified candidate provides a safe
        point at which to resume processing.
        """
        buffer = self._rx_buffer
        for offset in range(1, len(buffer) - 6):
            length = (buffer[offset] << 8) | buffer[offset + 1]
            if not 1 <= length <= self.MAX_FRAME_LENGTH:
                continue
            end = offset + 2 + length + 4
            if end > len(buffer):
                continue
            received_crc = struct.unpack_from('>I', buffer, offset + 2 + length)[0]
            computed_crc = zlib.crc32(buffer[offset + 2:offset + 2 + length]) & 0xFFFFFFFF
            if received_crc == computed_crc:
                return offset
        return None

    def _process_rx_buffer(self, pc_arrival_monotonic_ns: int) -> None:
        """Parse as many valid frames as possible from the receive buffer."""
        while len(self._rx_buffer) >= 7:
            length = (self._rx_buffer[0] << 8) | self._rx_buffer[1]
            if length < 1 or length > self.MAX_FRAME_LENGTH:
                # Invalid length header; discard one byte and resync.
                self._resynchronizing = True
                del self._rx_buffer[0]
                continue

            total_size = 2 + length + 4
            if len(self._rx_buffer) < total_size:
                if self._resynchronizing:
                    offset = self._find_valid_frame_offset()
                    if offset is not None:
                        del self._rx_buffer[:offset]
                        continue
                return

            frame_type = self._rx_buffer[2]
            payload = bytes(self._rx_buffer[3:2 + length])
            received_crc = struct.unpack('>I', self._rx_buffer[2 + length:2 + length + 4])[0]
            computed_crc = zlib.crc32(bytes([frame_type]) + payload) & 0xFFFFFFFF

            if received_crc != computed_crc:
                if not self._resynchronizing:
                    self.logger.warning(
                        "CRC mismatch in serial frame; searching for next valid frame")
                self._resynchronizing = True
                del self._rx_buffer[0]
                continue

            # Valid CRC establishes an actual frame boundary, including for
            # legacy or currently unsupported frame types.
            self._resynchronizing = False

            if frame_type == self.FRAME_TYPE_TAXEL:
                if len(payload) != 16:
                    self.logger.warning(
                        f"Invalid taxel payload length: {len(payload)}")
                    del self._rx_buffer[0]
                    continue
                raw_timestamp_ms = struct.unpack('>I', payload[0:4])[0]
                taxel_values = list(payload[4:16])
                needs_pc_time = self._emit_pc_receive_time or self._capture_pc_receive_time_for_compensation
                pc_receive_ns = time.time_ns() if needs_pc_time else None
                corrected_timestamp_ms = self._clock_compensator.compensate(
                    raw_timestamp_ms,
                    pc_receive_ns=pc_receive_ns,
                    allow_adaptive_update=(self._clock_compensation_sample_source == "taxel"),
                )
                if not self._taxel_data_received:
                    self._taxel_data_received = True
                    self.logger.info("First valid taxel telemetry frame received")
                self.data_queue.put((
                    "taxel",
                    taxel_values,
                    corrected_timestamp_ms,
                    raw_timestamp_ms,
                    pc_arrival_monotonic_ns
                ))
                self.data_queue.put((
                    "taxel_pc_time",
                    taxel_values,
                    raw_timestamp_ms,
                    pc_receive_ns
                ))

            elif frame_type == self.FRAME_TYPE_TACTILE_RATES:
                if len(payload) != self.TACTILE_RATES_PAYLOAD_SIZE:
                    self.logger.warning(
                        "Invalid tactile rates payload length: %d", len(payload))
                    del self._rx_buffer[:total_size]
                    continue

                raw_timestamp_ms = struct.unpack_from('>I', payload, 0)[0]
                taxels = list(payload[4:16])
                ra_rates_hz = [v / 100.0 for v in struct.unpack_from('>12I', payload, 16)]
                sa_rates_hz = [v / 100.0 for v in struct.unpack_from('>12I', payload, 64)]
                needs_pc_time = (self._emit_pc_receive_time
                                 or self._capture_pc_receive_time_for_compensation)
                pc_receive_ns = time.time_ns() if needs_pc_time else None
                corrected_timestamp_ms = self._clock_compensator.compensate(
                    raw_timestamp_ms,
                    pc_receive_ns=pc_receive_ns,
                    allow_adaptive_update=(self._clock_compensation_sample_source == "taxel"),
                )
                snapshot = {
                    "raw_timestamp_ms": raw_timestamp_ms,
                    "corrected_timestamp_ms": corrected_timestamp_ms,
                    "pc_arrival_monotonic_ns": pc_arrival_monotonic_ns,
                    "taxels": taxels,
                    "ra_rates_hz": ra_rates_hz,
                    "sa_rates_hz": sa_rates_hz,
                }
                with self._latest_rates_lock:
                    self._latest_rates_snapshot = snapshot
                if not self._taxel_data_received:
                    self._taxel_data_received = True
                    self.logger.info("First valid tactile rate snapshot received")
                self.data_queue.put(("tactile_rates", snapshot))

            elif frame_type == self.FRAME_TYPE_EVENTS:
                if len(payload) < 4 or ((len(payload) - 4) % 4) != 0:
                    self.logger.warning(
                        f"Invalid events payload length: {len(payload)}")
                    del self._rx_buffer[0]
                    continue

                reference_time_ms = struct.unpack('>I', payload[0:4])[0]
                ev_data = []
                for offset in range(4, len(payload), 4):
                    id0 = payload[offset]
                    id1 = payload[offset + 1]
                    delta_ms = struct.unpack('>H', payload[offset + 2:offset + 4])[0]
                    raw_event_time_ms = reference_time_ms + delta_ms
                    corrected_event_time_ms = self._clock_compensator.compensate(raw_event_time_ms)
                    ev_data.append((raw_event_time_ms, corrected_event_time_ms, id0, id1))
                self.data_queue.put((
                    "events",
                    ev_data,
                    pc_arrival_monotonic_ns
                ))

            elif frame_type == self.FRAME_TYPE_ACK:
                if len(payload) != 4:
                    self.logger.warning(
                        f"Invalid command ACK payload length: {len(payload)}")
                    del self._rx_buffer[0]
                    continue
                command_id = payload[0]
                status = payload[1]
                applied_value = struct.unpack('>H', payload[2:4])[0]
                self.data_queue.put((
                    "ack",
                    command_id,
                    status,
                    applied_value
                ))
                self.logger.info(
                    "Received command ACK: cmd=0x%02X status=%d value=%d",
                    command_id,
                    status,
                    applied_value,
                )

            else:
                # The CRC is valid, so the frame boundary is trustworthy even
                # when this GUI version no longer supports the frame type.
                # Consume the complete frame to stay synchronized (notably
                # with older firmware that still emits legacy IMU frames).
                self.logger.debug(
                    "Ignoring unsupported frame type: 0x%02X", frame_type)

            if not self._link_ready_emitted and frame_type in {
                self.FRAME_TYPE_TAXEL,
                self.FRAME_TYPE_EVENTS,
                self.FRAME_TYPE_TACTILE_RATES,
            }:
                self._link_ready_emitted = True
                self.data_queue.put(("link_ready",))

            # Valid frame consumed.
            del self._rx_buffer[:total_size]

    def _build_frame(self, frame_type: int, payload: bytes) -> bytes:
        """Build a framed packet for transmission."""
        length = 1 + len(payload)  # type + payload
        header_and_payload = bytes([frame_type]) + payload
        crc = zlib.crc32(header_and_payload) & 0xFFFFFFFF
        return (
            struct.pack('>H', length)
            + header_and_payload
            + struct.pack('>I', crc)
        )

    def _encode_command_payload(self, command: str) -> bytes:
        """Convert high-level command text into binary command payload.

        Supported user-level commands:
        - RA_THR=<int>
        - REF=<int>
        - SA_FREQ=<int>
        - GET_CONFIG
        """
        clean = command.strip()

        if clean == "GET_CONFIG":
            return bytes([self.CMD_GET_CONFIG])

        if "=" not in clean:
            raise ValueError(f"Unsupported command format: {clean}")

        key, raw_value = clean.split("=", 1)
        key = key.strip().upper()
        value = int(raw_value.strip())

        if key == "RA_THR":
            cmd_id = self.CMD_RA_THR
        elif key == "REF":
            cmd_id = self.CMD_REF
        elif key == "SA_FREQ":
            cmd_id = self.CMD_SA_FREQ
        else:
            raise ValueError(f"Unsupported command key: {key}")

        if value < 0 or value > 0xFFFF:
            raise ValueError(f"Command value out of range for uint16: {value}")

        return bytes([cmd_id]) + struct.pack('>H', value)

    def send_command(self, command: str) -> bool:
        """
        Send framed binary command to MCU for parameter configuration.

        Args:
            command (str): Logical command string (e.g., "RA_THR=5\n").

        Supported commands:
            - RA_THR=<value>: Set RA-I threshold (1-255)
            - REF=<value>: Set legacy refractory period (1-100 ms)
            - SA_FREQ=<value>: Set SA-II maximum frequency (10-1000 Hz)
            - GET_CONFIG: Request current parameter values

        Returns True only if the full frame was written successfully.
        """
        if not isinstance(command, str):
            raise TypeError("command must be a string")
        if not getattr(self.ser, "is_open", False):
            self.logger.error("Cannot send MCU command: serial port is closed")
            return False
        try:
            payload = self._encode_command_payload(command)
            frame = self._build_frame(self.FRAME_TYPE_COMMAND, payload)
            bytes_written = self.ser.write(frame)
            if bytes_written != len(frame):
                raise OSError(f"Short serial write: {bytes_written}/{len(frame)} bytes")
            self.ser.flush()
            self.logger.info("Sent framed command: %s", command.strip())
            return True
        except (AttributeError, OSError, ValueError, TypeError, serial.SerialTimeoutException, serial.SerialException) as e:
            self.logger.error("Failed to send command '%s': %s", command.strip(), e)
            return False
