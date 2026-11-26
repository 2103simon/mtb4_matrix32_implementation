"""No-hardware regression tests for the sole live SF/SC transport."""
import asyncio
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch

from matrix32.device import Matrix32Device, LiveCurrentUpdateError
from experiment_analysis.compare_ble_replays import shared_transport


class FakeClient:
    is_connected = True

    def __init__(self):
        self.callback = None
        self.opens = 0
        self.closes = 0
        self.writes = []
        self.reply = b">OK<"

    async def start_notify(self, uuid, callback):
        self.opens += 1
        self.callback = callback

    async def stop_notify(self, uuid):
        self.closes += 1
        self.callback = None

    async def write_gatt_char(self, uuid, message, response=False):
        self.writes.append((bytes(message), response))
        if self.callback is not None and self.reply is not None:
            self.callback(uuid, self.reply)


def running_device():
    d = Matrix32Device(config={"sa": [1], "sc": [50], "sw": [100],
                               "sf": 98, "pw": 100, "sv": 50, "st": 10})
    d.client = FakeClient()
    d.write_char_uuid = "write"
    d.read_char_uuid = "read"
    d.running = True
    d.config_verified = True
    d.verified_config_snapshot = deepcopy(d.config)
    return d


class SharedTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_commands_share_subscription_and_guard(self):
        d = running_device()
        original = deepcopy(d.config)
        records = [
            await d.apply_live_current(1, 0),
            await d.apply_live_frequency(400),
            await d.apply_live_frequency(98),
            await d.apply_live_current(1, 50),
        ]
        self.assertEqual(d.client.opens, 1)
        self.assertEqual(d.client.closes, 0)
        self.assertEqual([r["persistent_session_reused"] for r in records],
                         [False, True, True, True])
        for r in records:
            self.assertEqual(r["status"], "OK")
            self.assertEqual(r["transport"], "shared_persistent_sf_sc")
        for r in records[1:]:
            self.assertGreaterEqual(r["previous_ack_to_write_begin_ms"], 7.5)
        self.assertEqual([m[:4] for m, _ in d.client.writes],
                         [b">SC;", b">SF;", b">SF;", b">SC;"])
        self.assertTrue(all(response is False for _, response in d.client.writes))
        self.assertEqual(d.config, original)
        self.assertTrue(d.config_verified)
        await d.close_live_frequency_session()
        self.assertEqual(d.client.closes, 1)

    async def test_rejected_sc_invalidates_and_closes(self):
        d = running_device()
        await d.apply_live_frequency(100)
        d.client.reply = b">ERR<"
        with self.assertRaises(LiveCurrentUpdateError):
            await d.apply_live_current(1, 0)
        self.assertFalse(d.config_verified)
        self.assertEqual(d.config["sc"], [50])
        self.assertEqual(d.client.closes, 1)

    async def test_stop_during_mixed_guard_blocks_next_write(self):
        d = running_device()
        await d.apply_live_current(1, 0)
        task = asyncio.create_task(d.apply_live_frequency(400, min_ack_to_write_ms=100))
        for _ in range(100):
            if d._live_persistent_record is not None:
                break
            await asyncio.sleep(.001)
        self.assertIsNotNone(d._live_persistent_record)
        with patch("matrix32.device.stim_stop", new_callable=AsyncMock) as stop:
            await d.stop()
            stop.assert_awaited_once()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(d.client.writes), 1)
        self.assertFalse(d.config_verified)
        self.assertFalse(d.running)
        self.assertFalse(d._configuration_lock.locked())
        self.assertEqual(d.client.closes, 1)


class MetadataTests(unittest.TestCase):
    def test_new_and_historical_transport_identity(self):
        self.assertTrue(shared_transport({"live_transport": "shared_persistent_sf_sc"}))
        self.assertTrue(shared_transport({"experimental_shared_sf_sc": True}))
        self.assertFalse(shared_transport({"experimental_shared_sf_sc": False}))
        with self.assertRaises(ValueError):
            shared_transport({"live_transport": "unknown"})
        with self.assertRaises(ValueError):
            shared_transport({"live_transport": "shared_persistent_sf_sc",
                              "experimental_shared_sf_sc": False})


if __name__ == "__main__":
    unittest.main()
