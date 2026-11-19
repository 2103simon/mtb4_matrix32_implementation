"""Tests for the ACK gate that protects MCU recording startup."""
import unittest
from unittest.mock import patch

from communication.mcu_configuration import (
    McuConfigurationError,
    McuConfigurationHandshake,
)


class FakeSerialManager:
    def __init__(self, failed_command=None):
        self.commands = []
        self.failed_command = failed_command

    def send_command(self, command):
        self.commands.append(command)
        return command != self.failed_command


class McuConfigurationTest(unittest.TestCase):
    def test_both_matching_acks_required_before_ready(self):
        serial = FakeSerialManager()
        handshake = McuConfigurationHandshake(3, 150)
        self.assertFalse(handshake.ready)
        handshake.start(serial)
        self.assertEqual(serial.commands, ["RA_THR=3", "SA_FREQ=150"])
        self.assertFalse(handshake.accept_ack(0x03, 1, 150))
        self.assertFalse(handshake.ready)
        self.assertTrue(handshake.accept_ack(0x01, 1, 3))
        self.assertTrue(handshake.ready)

    def test_rejected_ack_raises(self):
        handshake = McuConfigurationHandshake(3, 150)
        handshake.start(FakeSerialManager())
        with self.assertRaisesRegex(McuConfigurationError, "rejected"):
            handshake.accept_ack(0x01, 0, 3)
        self.assertFalse(handshake.ready)

    def test_applied_value_mismatch_raises(self):
        handshake = McuConfigurationHandshake(3, 150)
        handshake.start(FakeSerialManager())
        with self.assertRaisesRegex(McuConfigurationError, "expected 150"):
            handshake.accept_ack(0x03, 1, 300)
        self.assertFalse(handshake.ready)

    def test_command_write_error_raises(self):
        handshake = McuConfigurationHandshake(3, 150)
        with self.assertRaisesRegex(McuConfigurationError, "SA_FREQ"):
            handshake.start(FakeSerialManager(failed_command="SA_FREQ=150"))
        self.assertFalse(handshake.ready)

    def test_timeout_raises_if_ack_missing(self):
        handshake = McuConfigurationHandshake(3, 150, timeout_seconds=0)
        handshake.start(FakeSerialManager())
        handshake.accept_ack(0x01, 1, 3)
        with self.assertRaisesRegex(McuConfigurationError, "0x03"):
            handshake.check_timeout()

    def test_duplicate_ack_does_not_complete_early(self):
        handshake = McuConfigurationHandshake(3, 150)
        handshake.start(FakeSerialManager())
        self.assertFalse(handshake.accept_ack(0x01, 1, 3))
        self.assertFalse(handshake.accept_ack(0x01, 1, 3))
        self.assertFalse(handshake.ready)

    def test_ack_before_start_and_unexpected_command_rejected(self):
        handshake = McuConfigurationHandshake(3, 150)
        with self.assertRaisesRegex(McuConfigurationError, "before"):
            handshake.accept_ack(0x01, 1, 3)
        handshake.start(FakeSerialManager())
        with self.assertRaisesRegex(McuConfigurationError, "Unexpected"):
            handshake.accept_ack(0x02, 1, 3)


if __name__ == "__main__":
    unittest.main()
