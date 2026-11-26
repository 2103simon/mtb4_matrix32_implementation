"""Replay qualification checks without serial ports, BLE, or a scope."""
import asyncio
import csv
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from communication.rate_replay import (
    ReplayPlan, ReplaySnapshots, load_replay, replay_batches,
    replay_initialization, await_replay_trigger,
)
from experiment_analysis.paths import sha256


def fixture(root):
    root.mkdir()
    (root/"metadata.json").write_text("{}")
    folder = root/"mcu_matrix32"
    folder.mkdir()
    rid = "test"
    cfg = {"tactile_encoding": {"raThreshold": 3, "saMaxFrequencyHz": 150},
           "stimulation_source": {"taxel": 0, "encoding": "sa_ii"},
           "matrix32_stimulation": {}}
    (folder/f"{rid}_settings.json").write_text(json.dumps({
        "recording_id": rid, "configuration": cfg,
        "mcu_configuration_confirmed": {"acknowledged": True}}))
    path = folder/f"{rid}_tactile_rates.csv"
    times = [0, 10, 20, 20, 30, 40, 50, 60]
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["corrected_timestamp_ms", "raw_timestamp_ms", "pc_arrival_monotonic_ns"]
                   + [f"{prefix}{i}" for prefix in ("taxel_", "ra_rate_hz_", "sa_rate_hz_")
                      for i in range(12)])
        for index, t in enumerate(times):
            w.writerow([t, t, 1000000000+t*1000000] + [index]*36)
    (root/"epoch_clock_analysis.json").write_text(json.dumps({
        "records": [{"first_sample_host_monotonic_ns_est": 1010000000}]}))
    return path


class ReplaySourceTests(unittest.TestCase):
    def test_half_open_window_batches_and_immutable_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/"run"
            path = fixture(root)
            digest = sha256(path)
            plan = load_replay(root, duration_s=.04)
            self.assertEqual([offset for offset, _ in plan.batches],
                             [0, 10000000, 20000000, 30000000])
            self.assertEqual(plan.provenance["sample_count"], 5)
            self.assertEqual(len(plan.batches[1][1]), 2)
            self.assertEqual(plan.provenance["source_rates_sha256"], digest)
            self.assertEqual(sha256(path), digest)
            explicit = load_replay(root, duration_s=.04, start_s=.01)
            self.assertEqual(plan.batches, explicit.batches)

    def test_rejects_uncovered_window_or_stale_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/"run"
            fixture(root)
            with self.assertRaises(ValueError):
                load_replay(root, duration_s=.1)
            with self.assertRaises(ValueError):
                load_replay(root, duration_s=.04, stale_s=.01)

    def test_requires_original_ack_configured_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/"run"
            fixture(root)
            path = root/"mcu_matrix32"/"test_settings.json"
            settings = json.loads(path.read_text())
            settings["mcu_configuration_confirmed"]["acknowledged"] = False
            path.write_text(json.dumps(settings))
            with self.assertRaises(ValueError):
                load_replay(root, duration_s=.04)


class ReplayInitializationTests(unittest.IsolatedAsyncioTestCase):
    def test_positive_first_target_and_reject_mute_start(self):
        from run_tactile_stimulation import requested_frequency
        positive = {"sa_rates_hz": [81]*12}
        mute = {"sa_rates_hz": [0]*12}
        plan = ReplayPlan({}, {}, [(0, [positive]), (10, [mute])])
        initial, info = replay_initialization(
            plan, requested_frequency, taxel=0, receptor="sa_ii")
        self.assertIs(initial, positive)
        self.assertEqual(info["initial_target_hz"], 81)
        self.assertEqual(info["positive_source_samples"], 1)
        self.assertEqual(info["mute_source_samples"], 1)
        plan.batches.reverse()
        with self.assertRaisesRegex(ValueError, "mute target"):
            replay_initialization(plan, requested_frequency, taxel=0, receptor="sa_ii")

    async def test_gate_waits_for_verified_start(self):
        from types import SimpleNamespace
        plan = ReplayPlan({"source": "test"}, {}, [(0, [{"value": 81}])])
        snapshots, capture = ReplaySnapshots(), Capture()
        device = SimpleNamespace(connected=True, running=False,
                                 config_verified=False, config={"sf": 81})
        info = {"initial_target_hz": 81, "max_preroll_s": 1,
                "post_start_settle_s": .01}
        with tempfile.TemporaryDirectory() as tmp:
            trigger = Path(tmp)/"trigger.json"
            task = asyncio.create_task(await_replay_trigger(
                plan, snapshots, capture, trigger, device=device, arm=True,
                initial={"value": 81}, initialization=info, stale_seconds=.25))
            try:
                await asyncio.sleep(.02)
                self.assertTrue(capture.samples)
                self.assertFalse(any(n == "replay_preroll_ready" for n, _ in capture.events))
                device.running = device.config_verified = True
                async def wait_ready():
                    while not any(n == "replay_preroll_ready" for n, _ in capture.events):
                        await asyncio.sleep(.001)
                await asyncio.wait_for(wait_ready(), .5)
                now = time.monotonic_ns()
                trigger.write_text(json.dumps({"source": plan.provenance, "start_ns": now}))
                self.assertEqual(await asyncio.wait_for(task, .5), now)
                self.assertTrue(all(s["value"] == 81 for s in capture.samples))
            finally:
                if not task.done():
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task

    async def test_early_trigger_rejected(self):
        from types import SimpleNamespace
        plan = ReplayPlan({}, {}, [(0, [{"value": 81}])])
        with tempfile.TemporaryDirectory() as tmp:
            trigger = Path(tmp)/"trigger.json"
            trigger.write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "before verified readiness"):
                await await_replay_trigger(
                    plan, ReplaySnapshots(), Capture(), trigger,
                    device=SimpleNamespace(connected=True), arm=True,
                    initial={"value": 81}, initialization={"max_preroll_s": 1},
                    stale_seconds=.25)


class Capture:
    def __init__(self):
        self.recorder = self
        self.samples = []
        self.events = []

    def record_tactile_rates(self, snapshot):
        self.samples.append(snapshot)

    def emit(self, name, **details):
        self.events.append((name, details))


class ReplayDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_arrival_and_last_sample_in_batch(self):
        snapshots, capture = ReplaySnapshots(), Capture()
        plan = ReplayPlan({"duration_s": .025, "sample_count": 3}, {},
                          [(0, [{"value": 1}, {"value": 2}]),
                           (10000000, [{"value": 3}])])
        start = time.monotonic_ns()+10000000
        await replay_batches(plan, snapshots, capture, start_ns=start)
        self.assertEqual([s["value"] for s in capture.samples], [1, 2, 3])
        self.assertEqual(capture.samples[0]["pc_arrival_monotonic_ns"],
                         capture.samples[1]["pc_arrival_monotonic_ns"])
        self.assertEqual(snapshots.get_latest_rates_snapshot()["value"], 3)
        self.assertTrue(all(s["pc_arrival_monotonic_ns"] >= start for s in capture.samples))
        self.assertEqual([name for name, _ in capture.events],
                         ["replay_batch", "replay_batch", "replay_completed"])

    async def test_excess_lateness_aborts_before_publishing(self):
        snapshots, capture = ReplaySnapshots(), Capture()
        plan = ReplayPlan({"duration_s": .025, "sample_count": 1}, {},
                          [(0, [{"value": 1}])])
        with self.assertRaises(TimeoutError):
            await replay_batches(plan, snapshots, capture,
                                 start_ns=time.monotonic_ns()-100000000)
        self.assertEqual(capture.samples, [])
        self.assertIsNone(snapshots.get_latest_rates_snapshot())

    async def test_finite_replay_stops_and_does_not_restart_worker(self):
        from run_tactile_stimulation import run_replay_tasks
        from types import SimpleNamespace
        capture = Capture()
        capture.start = lambda **kw: None
        plan = ReplayPlan({"duration_s": .025, "sample_count": 1,
                           "source_settings_sha256": "test"}, {},
                          [(0, [{"sa_rates_hz": [81]*12}])])
        aborts = []
        device = SimpleNamespace(connected=True,
                                 abort_pending_commands=lambda: aborts.append(True))
        ready = asyncio.Event()
        tasks = []
        async def blocked(*args, **kwargs):
            tasks.append(asyncio.current_task())
            await asyncio.Event().wait()
        with tempfile.TemporaryDirectory() as tmp:
            trigger = Path(tmp)/"trigger.json"
            trigger.write_text(json.dumps({"source": plan.provenance,
                                           "start_ns": time.monotonic_ns()}))
            with patch("run_tactile_stimulation.frequency_worker", blocked), \
                 patch("run_tactile_stimulation.stale_supervisor", blocked), \
                 patch("run_tactile_stimulation.await_replay_trigger",
                       new_callable=AsyncMock) as gate, \
                 patch("run_tactile_stimulation.stop_output", new_callable=AsyncMock) as stop:
                gate.return_value = time.monotonic_ns()
                stop.return_value = True
                task = asyncio.create_task(run_replay_tasks(
                    plan, trigger, device, ready, capture, arm=True,
                    taxel=0, receptor="sa_ii", stale_seconds=.25))
                try:
                    async def wait_finished():
                        while not any(name == "replay_finished" for name, _ in capture.events):
                            if task.done():
                                await task
                            await asyncio.sleep(.001)
                    await asyncio.wait_for(wait_finished(), 1)
                    stop.assert_awaited_once()
                    self.assertEqual(aborts, [True])
                    self.assertEqual(len(tasks), 2)
                    self.assertTrue(all(t.cancelled() for t in tasks))
                    self.assertFalse(task.done())
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task


if __name__ == "__main__":
    unittest.main()
