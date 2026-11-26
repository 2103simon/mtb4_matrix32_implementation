"""Offline checks of subscription evidence and the existing generic ACK session.

These are transport simulations only: no real BLE connection or stimulation.
"""
import asyncio
import unittest

from experiment_analysis.analyze_command_chain import subscription_timings
from matrix32.ack import PersistentConfigAckSession, ConfigurationAckError
from matrix32.config import build_config_messages


class FakeClient:
    is_connected = True

    def __init__(self):
        self.opens = self.closes = 0
        self.callback = None
        self.writes = []
        self.replies = [b">OK<"]
        self.write_started = asyncio.Event()
        self.release_write = None

    async def start_notify(self, uuid, callback):
        self.opens += 1
        self.callback = callback

    async def stop_notify(self, uuid):
        self.closes += 1
        self.callback = None

    async def write_gatt_char(self, uuid, message, response=False):
        self.writes.append((bytes(message), response))
        self.write_started.set()
        if self.release_write is not None:
            await self.release_write.wait()
        for reply in self.replies:
            self.callback(uuid, reply)


def command_pair():
    config = {"sa": [1], "sc": [0], "sf": 81, "sw": [100],
              "pw": 100, "sv": 50, "st": 10}
    messages = build_config_messages(config)
    return tuple(next(m for m in messages if m.startswith(prefix))
                 for prefix in (b">SF;", b">SC;"))


class SessionTimingTests(unittest.TestCase):
    def test_legacy_sc_nested_timestamps(self):
        result = subscription_timings({
            "request_monotonic_s": 1.010,
            "write_begin_monotonic_s": 1.030,
            "ack": {
                "subscribe_begin_monotonic_s": 1.011,
                "subscribe_return_monotonic_s": 1.029,
                "unsubscribe_begin_monotonic_s": 1.050,
                "unsubscribe_return_monotonic_s": 1.060,
            },
        }, 1000000000, 1070000000)
        self.assertEqual(result["before_device_request_ms"], 10)
        self.assertEqual(result["subscribe_ms"], 18)
        self.assertEqual(result["unsubscribe_ms"], 10)

    def test_reused_session_not_counted_as_new_subscription(self):
        result = subscription_timings({
            "persistent_session_reused": True,
            "subscribe_begin_monotonic_s": .1,
            "subscribe_return_monotonic_s": .2,
        }, 1000000000, 1070000000)
        self.assertIsNone(result["subscribe_ms"])
        self.assertIsNone(result["unsubscribe_ms"])

    def test_out_of_transaction_timestamps_not_counted(self):
        result = subscription_timings({
            "subscribe_begin_monotonic_s": 1.01,
            "subscribe_return_monotonic_s": 1.08,
        }, 1000000000, 1070000000)
        self.assertIsNone(result["subscribe_ms"])


class MixedSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_command_sequence_one_subscription(self):
        sf, sc = command_pair()
        client = FakeClient()
        async with PersistentConfigAckSession(client, "write", "indicate") as session:
            for message, expected in ((sf, "SF"), (sc, "SC"), (sf, "SF")):
                result = await session.send(message)
                self.assertEqual(result["command"], expected)
                self.assertEqual(result["status"], "OK")
                self.assertEqual(len(result["indications"]), 1)
        self.assertEqual((client.opens, client.closes), (1, 1))
        self.assertEqual(client.writes, [(sf, False), (sc, False), (sf, False)])

    async def test_concurrent_request_rejected(self):
        sf, sc = command_pair()
        client = FakeClient()
        client.release_write = asyncio.Event()
        async with PersistentConfigAckSession(client, "write", "indicate") as session:
            task = asyncio.create_task(session.send(sf))
            await client.write_started.wait()
            try:
                with self.assertRaises(RuntimeError):
                    await session.send(sc)
            finally:
                client.release_write.set()
                await task
        self.assertEqual(len(client.writes), 1)

    async def test_duplicate_ack_fails_closed(self):
        sf, sc = command_pair()
        client = FakeClient()
        client.replies = [b">OK<", b">OK<"]
        async with PersistentConfigAckSession(client, "write", "indicate") as session:
            with self.assertRaises(ConfigurationAckError):
                await session.send(sf)
            with self.assertRaises(RuntimeError):
                await session.send(sc)
        self.assertEqual(len(client.writes), 1)

    async def test_unsolicited_ack_blocks_next_command(self):
        sf, sc = command_pair()
        client = FakeClient()
        async with PersistentConfigAckSession(client, "write", "indicate") as session:
            await session.send(sf)
            client.callback("indicate", b">OK<")
            with self.assertRaises(RuntimeError):
                await session.send(sc)
        self.assertEqual(len(client.writes), 1)

    async def test_negative_ack_is_not_success(self):
        _, sc = command_pair()
        client = FakeClient()
        client.replies = [b">ERR<"]
        with self.assertRaises(ConfigurationAckError):
            async with PersistentConfigAckSession(client, "write", "indicate") as session:
                await session.send(sc)
        self.assertEqual((client.opens, client.closes), (1, 1))


if __name__ == "__main__":
    unittest.main()
