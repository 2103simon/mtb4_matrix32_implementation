"""Acknowledged MCU encoding configuration before beginning data recording."""

import time


class McuConfigurationError(RuntimeError):
    """MCU configuration could not be verified."""


class McuConfigurationHandshake:
    """Send fixed encoding settings and require matching successful MCU ACKs.

    Only a matching ACK (command id, success status, applied value) counts.
    Samples before the second ACK must be ignored by the recording application.
    """

    ACK_TIMEOUT_SECONDS = 5.0

    def __init__(self, ra_threshold, sa_max_frequency_hz, timeout_seconds=None):
        self.expected = {0x01: int(ra_threshold), 0x03: int(sa_max_frequency_hz)}
        self.pending = set(self.expected)
        self.started = False
        self.deadline = None
        self.timeout_seconds = (
            self.ACK_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
        )

    @property
    def ready(self):
        return self.started and not self.pending

    def start(self, serial_manager):
        """Transmit both commands; raise immediately on an unsuccessful write."""
        if self.started:
            raise McuConfigurationError("MCU configuration already started")
        self.started = True
        # Start the ACK deadline before writing; slow serial writes must also
        # count against the handshake time budget.
        self.deadline = time.monotonic() + self.timeout_seconds
        for command in (
            f"RA_THR={self.expected[0x01]}",
            f"SA_FREQ={self.expected[0x03]}",
        ):
            if not serial_manager.send_command(command):
                raise McuConfigurationError(f"Failed to send MCU command: {command}")

    def accept_ack(self, command_id, status, applied_value):
        """Return True when this ACK completes the handshake."""
        if not self.started:
            raise McuConfigurationError("MCU ACK received before configuration request")
        if command_id not in self.expected:
            raise McuConfigurationError(f"Unexpected MCU ACK for command 0x{command_id:02X}")
        if command_id not in self.pending:
            # Duplicate ACK is not a new successful configuration.
            return self.ready
        if status != 1:
            raise McuConfigurationError(
                f"MCU rejected command 0x{command_id:02X} (status={status})"
            )
        expected = self.expected[command_id]
        if applied_value != expected:
            raise McuConfigurationError(
                f"MCU command 0x{command_id:02X} applied {applied_value}, "
                f"expected {expected}"
            )
        self.pending.remove(command_id)
        return self.ready

    def check_timeout(self):
        if self.started and not self.ready and time.monotonic() >= self.deadline:
            missing = ", ".join(f"0x{item:02X}" for item in sorted(self.pending))
            raise McuConfigurationError(
                f"Timed out waiting for MCU configuration ACK(s): {missing}"
            )
