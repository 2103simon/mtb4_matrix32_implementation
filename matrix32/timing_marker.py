"""Fail-closed serial timing-marker client for the S07 Arduino Nano Every.

The marker protocol deliberately separates two facts:

* the Nano drives D7 HIGH before emitting a matching ACK;
* the host receives that ACK before it proceeds to the Matrix32 BLE write.

The physical D7 edge is therefore ordered before the BLE write, but its exact
time is established only by the oscilloscope CH4 waveform. Host monotonic
timestamps are retained as transport evidence, not as a substitute for CH4.

MARK is never retried automatically: a lost ACK after a real edge would make a
retry create an ambiguous duplicate marker. PING may be retried during startup
synchronization because it never changes D7.
"""
from __future__ import annotations

import time


DEFAULT_BAUDRATE = 115200
DEFAULT_ACK_TIMEOUT_S = 0.25
DEFAULT_STARTUP_TIMEOUT_S = 2.0
MAX_SEQUENCE_ID = 0x7FFFFFFF
EXPECTED_FIRMWARE_ID = "S07_MARKER_V2"


class TimingMarkerError(RuntimeError):
    """The Nano timing-marker command did not complete unambiguously."""

    def __init__(self, record):
        self.record = record
        super().__init__(
            "Timing marker {} {} (sequence {})".format(
                record.get("command", "UNKNOWN"),
                record.get("status", "UNKNOWN"),
                record.get("sequence_id"),
            )
        )


def discover_timing_marker_port():
    """Find a uniquely identifiable Nano Every without opening a serial port."""
    from communication.serial_ports import discover_port
    return discover_port("nano")


class SerialTimingMarker:
    """Sequence-matched command/ACK client for the Nano marker firmware."""

    def __init__(
        self,
        port,
        *,
        baudrate=DEFAULT_BAUDRATE,
        ack_timeout_s=DEFAULT_ACK_TIMEOUT_S,
        serial_factory=None,
    ):
        if not port:
            raise ValueError("Timing-marker serial port is required")
        if not 0 < float(ack_timeout_s) <= 2.0:
            raise ValueError("Timing-marker ACK timeout must be >0 and <=2 seconds")
        self.port = str(port)
        self.baudrate = int(baudrate)
        self.ack_timeout_s = float(ack_timeout_s)
        self._serial_factory = serial_factory
        self.serial = None
        self._sequence_id = 0

    def open(self):
        if self.serial is not None:
            return self
        factory = self._serial_factory
        if factory is None:
            try:
                import serial
            except ImportError as exc:  # pragma: no cover - environment specific
                raise RuntimeError(
                    "pyserial is required for the S07 timing marker"
                ) from exc
            factory = serial.Serial

        self.serial = factory(
            self.port,
            self.baudrate,
            timeout=min(0.02, self.ack_timeout_s),
            write_timeout=self.ack_timeout_s,
        )
        return self

    def close(self):
        if self.serial is None:
            return
        try:
            self.serial.close()
        finally:
            self.serial = None

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _next_sequence_id(self):
        self._sequence_id += 1
        if self._sequence_id > MAX_SEQUENCE_ID:
            self._sequence_id = 1
        return self._sequence_id

    def _require_open(self):
        if self.serial is None:
            raise RuntimeError("Timing-marker serial port is not open")

    def _exchange(self, command, *, timeout_s=None):
        self._require_open()
        timeout = self.ack_timeout_s if timeout_s is None else float(timeout_s)
        if not 0 < timeout <= 2.0:
            raise ValueError("Timing-marker command timeout must be >0 and <=2 seconds")

        sequence_id = self._next_sequence_id()
        wire = "{} {}\n".format(command, sequence_id).encode("ascii")
        start = time.monotonic()
        record = {
            "command": command,
            "sequence_id": sequence_id,
            "status": "IN_FLIGHT",
            "request_hex": wire.hex(" "),
            "request_monotonic_s": start,
            "write_return_monotonic_s": None,
            "ack_monotonic_s": None,
            "completed_monotonic_s": None,
            "elapsed_ms": None,
            "unexpected_lines": [],
        }

        try:
            self.serial.write(wire)
            self.serial.flush()
            record["write_return_monotonic_s"] = time.monotonic()
        except Exception as exc:
            record["status"] = "SERIAL_WRITE_ERROR"
            record["error"] = "{}: {}".format(type(exc).__name__, exc)
            record["completed_monotonic_s"] = time.monotonic()
            record["elapsed_ms"] = round(
                (record["completed_monotonic_s"] - start) * 1000, 3
            )
            raise TimingMarkerError(record) from exc

        expected = "ACK {} {}".format(command, sequence_id)
        error_prefix = "ERR {} {}".format(command, sequence_id)
        deadline = start + timeout

        while time.monotonic() < deadline:
            try:
                raw = self.serial.readline()
            except Exception as exc:
                record["status"] = "SERIAL_READ_ERROR"
                record["error"] = "{}: {}".format(type(exc).__name__, exc)
                record["completed_monotonic_s"] = time.monotonic()
                record["elapsed_ms"] = round(
                    (record["completed_monotonic_s"] - start) * 1000, 3
                )
                raise TimingMarkerError(record) from exc

            if not raw:
                continue
            received = time.monotonic()
            try:
                line = bytes(raw).decode("ascii", errors="strict").strip()
            except UnicodeDecodeError:
                record["unexpected_lines"].append(bytes(raw).hex(" "))
                continue

            if line == expected or line.startswith(expected + " "):
                record["status"] = "ACK"
                record["ack_monotonic_s"] = received
                record["response"] = line
                suffix = line[len(expected):].strip().split()
                if suffix:
                    if len(suffix) % 2:
                        record["status"] = "MALFORMED_ACK"
                        record["completed_monotonic_s"] = received
                        record["elapsed_ms"] = round((received - start) * 1000, 3)
                        raise TimingMarkerError(record)
                    record["ack_fields"] = {
                        suffix[i]: suffix[i + 1]
                        for i in range(0, len(suffix), 2)
                    }
                record["completed_monotonic_s"] = received
                record["elapsed_ms"] = round((received - start) * 1000, 3)
                return record

            if line == error_prefix or line.startswith(error_prefix + " "):
                record["status"] = "MCU_ERROR"
                record["ack_monotonic_s"] = received
                record["response"] = line
                record["completed_monotonic_s"] = received
                record["elapsed_ms"] = round((received - start) * 1000, 3)
                raise TimingMarkerError(record)

            record["unexpected_lines"].append(line)

        record["status"] = "TIMEOUT"
        record["completed_monotonic_s"] = time.monotonic()
        record["elapsed_ms"] = round(
            (record["completed_monotonic_s"] - start) * 1000, 3
        )
        raise TimingMarkerError(record)

    def ping(self, *, timeout_s=None):
        """Verify the serial protocol without changing D7."""
        return self._exchange("PING", timeout_s=timeout_s)

    def info(self, *, timeout_s=None):
        """Read firmware identity and marker diagnostics without changing D7."""
        record = self._exchange("INFO", timeout_s=timeout_s)
        fields = record.get("ack_fields", {})
        required = {"FW", "PULSE_US", "COUNT", "PIN", "UPTIME_MS"}
        missing = required.difference(fields)
        if missing:
            record["status"] = "MALFORMED_INFO"
            record["error"] = "Missing INFO fields: " + ", ".join(sorted(missing))
            raise TimingMarkerError(record)
        try:
            record["firmware_id"] = fields["FW"]
            record["pulse_us"] = int(fields["PULSE_US"])
            record["accepted_mark_count"] = int(fields["COUNT"])
            record["pin_state"] = int(fields["PIN"])
            record["uptime_ms"] = int(fields["UPTIME_MS"])
        except ValueError as exc:
            record["status"] = "MALFORMED_INFO"
            record["error"] = "Non-integer INFO diagnostic field"
            raise TimingMarkerError(record) from exc
        return record

    def synchronize(self, *, startup_timeout_s=DEFAULT_STARTUP_TIMEOUT_S):
        """Wait for a freshly opened Nano to answer PING.

        Serial-port opening may reset an Arduino. Only PING is retried here;
        MARK is intentionally never retried.
        """
        deadline = time.monotonic() + float(startup_timeout_s)
        last_error = None
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                return self.ping(timeout_s=min(self.ack_timeout_s, remaining))
            except TimingMarkerError as exc:
                last_error = exc
                if exc.record.get("status") not in {"TIMEOUT", "SERIAL_READ_ERROR"}:
                    raise
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        raise RuntimeError(
            "Nano timing marker did not become serial-ready within {:.3f}s".format(
                startup_timeout_s
            )
        ) from last_error

    def mark(self):
        """Emit exactly one marker pulse and wait for its matching ACK.

        Firmware drives D7 HIGH before it starts transmitting ACK MARK <seq>.
        This method never retries a MARK command.
        """
        record = self._exchange("MARK")
        fields = record.get("ack_fields", {})
        if "COUNT" not in fields or "PIN" not in fields:
            record["status"] = "MALFORMED_MARK_ACK"
            record["error"] = "MARK ACK lacks COUNT/PIN diagnostics"
            raise TimingMarkerError(record)
        try:
            record["accepted_mark_count"] = int(fields["COUNT"])
            record["pin_state_after_high"] = int(fields["PIN"])
        except ValueError as exc:
            record["status"] = "MALFORMED_MARK_ACK"
            record["error"] = "Non-integer MARK diagnostic field"
            raise TimingMarkerError(record) from exc
        return record
