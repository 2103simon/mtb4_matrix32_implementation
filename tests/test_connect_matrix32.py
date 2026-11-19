"""Offline checks: connection-only Matrix32 script never enables stimulation."""

import asyncio
import unittest

from connect_matrix32 import run_connection


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


class Matrix32ConnectionHoldTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_disconnects_without_stimulating(self):
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

    async def test_unexpected_disconnect_reports_error_and_cleans_up(self):
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

    async def test_readiness_failure_disconnects_without_stimulating(self):
        device = FakeMatrix32(battery=None)
        with self.assertRaisesRegex(RuntimeError, "readiness"):
            await run_connection(device)
        self.assertEqual(device.disconnect_calls, 1)
        self.assertEqual(device.stimulation_calls, [])


if __name__ == "__main__":
    unittest.main()
