"""Offline checks: connection-only Matrix32 script never enables stimulation."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from connect_matrix32 import check_bleak_version, check_pc_bluetooth, run_connection


class FakeMatrix32:
    def __init__(self, battery=80):
        self.client = None
        self.connected = False
        self.last_battery_level = None
        self.battery = battery
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.stimulation_calls = []

    async def connect(self):
        self.connect_calls += 1
        self.client = object()
        self.connected = True
        self.last_battery_level = self.battery
        return self

    async def disconnect(self):
        self.disconnect_calls += 1
        self.connected = False
        self.client = None

    async def apply(self, *args, **kwargs):
        self.stimulation_calls.append("apply")
        raise AssertionError("Connection-only mode must not configure output")

    async def start(self):
        self.stimulation_calls.append("start")
        raise AssertionError("Connection-only mode must not start stimulation")


class BluetoothPreflightTest(unittest.TestCase):
    @patch("connect_matrix32.subprocess.run")
    @patch("connect_matrix32.check_bluetooth_service", return_value=False)
    def test_service_inactive_aborts_before_adapter_check(self, service, run):
        with self.assertRaisesRegex(RuntimeError, "service is unavailable"):
            check_pc_bluetooth()
        run.assert_not_called()

    @patch("connect_matrix32.subprocess.run", return_value=SimpleNamespace(
        returncode=0, stdout="Controller 12:34:56\n  Powered: no\n"
    ))
    @patch("connect_matrix32.check_bluetooth_service", return_value=True)
    def test_adapter_powered_off(self, service, run):
        with self.assertRaisesRegex(RuntimeError, "adapter is OFF"):
            check_pc_bluetooth()

    @patch("connect_matrix32.subprocess.run", return_value=SimpleNamespace(
        returncode=0, stdout="Controller 12:34:56\n  Powered: yes\n"
    ))
    @patch("connect_matrix32.check_bluetooth_service", return_value=True)
    def test_adapter_powered_on(self, service, run):
        self.assertIsNone(check_pc_bluetooth())


class BleakCompatibilityTest(unittest.TestCase):
    @patch("connect_matrix32.version", return_value="3.0.2")
    def test_incompatible_version_rejected(self, get_version):
        with self.assertRaisesRegex(RuntimeError, "bleak==0.22.3"):
            check_bleak_version()

    @patch("connect_matrix32.version", return_value="0.22.3")
    def test_validated_version_accepted(self, get_version):
        self.assertIsNone(check_bleak_version())


@patch("connect_matrix32.check_bleak_version", return_value=None)
@patch("connect_matrix32.check_pc_bluetooth", return_value=None)
class Matrix32ConnectionHoldTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_disconnects_without_stimulating(self, bluetooth, bleak):
        device = FakeMatrix32()
        task = asyncio.create_task(run_connection(device, heartbeat_seconds=0.01))
        for _ in range(100):
            if device.connect_calls and device.connected:
                break
            await asyncio.sleep(0.001)
        self.assertTrue(device.connected)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(device.disconnect_calls, 1)
        self.assertEqual(device.stimulation_calls, [])

    async def test_unexpected_disconnect_reports_error_and_cleans_up(self, bluetooth, bleak):
        device = FakeMatrix32()
        task = asyncio.create_task(run_connection(device, heartbeat_seconds=0.01))
        for _ in range(100):
            if device.connected:
                break
            await asyncio.sleep(0.001)
        self.assertTrue(device.connected)
        device.connected = False
        with self.assertRaisesRegex(ConnectionError, "BLE link lost"):
            await task
        self.assertEqual(device.disconnect_calls, 1)
        self.assertEqual(device.stimulation_calls, [])

    async def test_readiness_failure_disconnects_without_stimulating(self, bluetooth, bleak):
        device = FakeMatrix32(battery=None)
        with self.assertRaisesRegex(RuntimeError, "readiness"):
            await run_connection(device)
        self.assertEqual(device.disconnect_calls, 1)
        self.assertEqual(device.stimulation_calls, [])


if __name__ == "__main__":
    unittest.main()
