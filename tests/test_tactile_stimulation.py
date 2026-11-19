"""Offline tests for the single-channel latest-rate Matrix32 worker."""
import asyncio
import time
import unittest
from copy import deepcopy

from run_tactile_stimulation import (
    build_stim_config, frequency_worker, requested_frequency, validate_settings,
)


CONFIG = {
    "tactile_encoding": {"raThreshold": 3, "saMaxFrequencyHz": 150},
    "stimulation_source": {"taxel": 4, "encoding": "ra_i"},
    "matrix32_stimulation": {
        "channel": 1,
        "currentCommand": 50,
        "phaseWidthUs": 100,
        "outputVoltageCommand": 50,
        "interPulseTimingCommand": 10,
    },
}
STIM = {
    "sa": [1], "sc": [20], "sw": [100], "sf": 20,
    "pw": 50, "st": 10, "sv": 50,
}


def sample(ra, sa=100, age=0):
    return {
        "pc_arrival_monotonic_ns": time.monotonic_ns() - int(age * 1e9),
        "ra_rates_hz": [ra] * 12,
        "sa_rates_hz": [sa] * 12,
    }


class FakeSerial:
    def __init__(self, snapshot=None):
        self.snapshot = snapshot
        self.running = True

    def is_running(self):
        return self.running

    def get_latest_rates_snapshot(self):
        return self.snapshot


class FakeMatrix32:
    def __init__(self):
        self.connected = True
        self.client = object()
        self.running = False
        self.config_verified = False
        self.records = []
        self.config = {"sf": 20}

    def set_global_frequency(self, hz):
        self.config["sf"] = hz
        self.records.append(("set", hz))

    async def apply(self, **kwargs):
        self.config_verified = True
        self.records.append(("apply", self.config["sf"]))
        return [object()] * 7

    async def start(self):
        self.running = True
        self.records.append(("start", self.config["sf"]))

    async def apply_live_frequency(self, hz):
        self.records.append(("live", hz))

    async def stop(self):
        self.running = False
        self.records.append(("stop",))


class FixedConfigTests(unittest.TestCase):
    def test_reference_settings_and_ch1_only(self):
        settings = build_stim_config(CONFIG)
        self.assertEqual(settings, {
            "sa": [1], "sc": [50], "sw": [100], "pw": 100,
            "sf": 20, "sv": 50, "st": 10,
        })
        # SF is merely a bootstrap placeholder: the live MCU request sets it.
        self.assertEqual(validate_settings(CONFIG, settings, arm=True), (4, "ra_i"))

    def test_200us_is_within_full_cycle_but_not_verified_at_400hz(self):
        cfg = deepcopy(CONFIG)
        cfg["matrix32_stimulation"]["phaseWidthUs"] = 200
        settings = build_stim_config(cfg)
        self.assertEqual(settings["sw"], [200])

    def test_reject_physically_impossible_full_cycle_and_other_channels(self):
        cfg = deepcopy(CONFIG)
        cfg["matrix32_stimulation"]["phaseWidthUs"] = 1300
        with self.assertRaisesRegex(ValueError, "Biphasic"):
            build_stim_config(cfg)
        cfg["matrix32_stimulation"]["phaseWidthUs"] = 100
        cfg["matrix32_stimulation"]["channel"] = 2
        with self.assertRaisesRegex(ValueError, "channel 1"):
            build_stim_config(cfg)

    def test_reject_bad_fixed_configuration_types(self):
        for key, invalid in (
            ("currentCommand", 0),
            ("phaseWidthUs", True),
            ("outputVoltageCommand", 300),
            ("interPulseTimingCommand", 0),
        ):
            with self.subTest(key=key):
                cfg = deepcopy(CONFIG)
                cfg["matrix32_stimulation"][key] = invalid
                with self.assertRaises(ValueError):
                    build_stim_config(cfg)


class SelectionTests(unittest.TestCase):
    def test_select_ra_sa_rounding_and_max_frequency(self):
        snap = sample(72.6, 300.3)
        self.assertEqual(requested_frequency(snap, 4, "ra_i"), 73)
        self.assertEqual(requested_frequency(snap, 4, "sa_ii"), 300)
        # Matrix32 upper command clamp: 400 is the maximum sent, even
        # though oscilloscope measurements indicate ~401 pulses/s at SF=400.
        for requested in (400, 400.01, 401, 800, 10_000):
            with self.subTest(requested=requested):
                self.assertEqual(requested_frequency(sample(requested), 4, "ra_i"), 400)
        self.assertEqual(requested_frequency(sample(399.6), 4, "ra_i"), 400)
        self.assertEqual(requested_frequency(sample(399.4), 4, "ra_i"), 399)
        self.assertEqual(requested_frequency(sample(1.99), 4, "ra_i"), 0)
        self.assertEqual(requested_frequency(sample(0), 4, "ra_i"), 0)

    def test_validate_selected_channel_and_required_output_settings(self):
        self.assertEqual(validate_settings(CONFIG, STIM, arm=True), (4, "ra_i"))
        multi = deepcopy(STIM)
        multi["sa"] = [1, 2]
        with self.assertRaisesRegex(ValueError, "channel 1"):
            validate_settings(CONFIG, multi, arm=True)
        zero = deepcopy(STIM)
        zero["sc"] = [0]
        with self.assertRaisesRegex(ValueError, "positive CH1 current"):
            validate_settings(CONFIG, zero, arm=True)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_newest_sample_only_and_zero_stops(self):
        serial = FakeSerial(sample(40))
        device = FakeMatrix32()
        ready = asyncio.Event()
        ready.configured_ns = time.monotonic_ns() - 1000000000
        ready.set()
        task = asyncio.create_task(frequency_worker(
            serial, device, ready, 4, "ra_i", arm=True
        ))
        try:
            await asyncio.sleep(0.05)
            self.assertIn(("start", 40), device.records)
            serial.snapshot = sample(60)
            await asyncio.sleep(0.05)
            self.assertIn(("live", 60), device.records)
            serial.snapshot = sample(0)
            await asyncio.sleep(0.05)
            self.assertIn(("stop",), device.records)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_pre_ack_sample_not_used(self):
        serial = FakeSerial(sample(55))
        device = FakeMatrix32()
        ready = asyncio.Event()
        ready.configured_ns = time.monotonic_ns()
        ready.set()
        task = asyncio.create_task(frequency_worker(
            serial, device, ready, 4, "ra_i", arm=True
        ))
        try:
            await asyncio.sleep(0.025)
            self.assertFalse(device.records)
            serial.snapshot = sample(55)
            await asyncio.sleep(0.035)
            self.assertIn(("start", 55), device.records)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_dry_run_never_writes_matrix32(self):
        serial = FakeSerial(sample(75))
        device = FakeMatrix32()
        ready = asyncio.Event()
        ready.configured_ns = 0
        ready.set()
        task = asyncio.create_task(frequency_worker(
            serial, device, ready, 4, "ra_i", arm=False
        ))
        try:
            await asyncio.sleep(0.025)
            self.assertEqual(device.records, [])
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


if __name__ == "__main__":
    unittest.main()
