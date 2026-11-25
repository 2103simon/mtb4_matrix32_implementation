"""Focused controller regressions without importing optional hardware libraries.

These tests compile the named, unchanged function definitions from the actual
controller. Fake MCU/device objects exercise command decisions and provenance;
they do not test serial/BLE transport, electrical output, or the full run loop.
No dependency modules are replaced in sys.modules.
"""
import ast
import asyncio
import copy
import math
from pathlib import Path
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "run_tactile_stimulation.py"
FUNCTIONS = {
    "requested_frequency", "stop_output", "send_live_current", "frequency_worker",
}
CONSTANTS = {
    "STALE_SECONDS", "POLL_SECONDS", "STOP_TIMEOUT_SECONDS", "ZERO_CURRENT_FAST_SF_HZ",
}


class ScenarioComplete(Exception):
    """Stop when the worker reaches its ordinary wait for another sample."""


class FakeClock:
    def __init__(self):
        self.now_ns = 1_000_000_000

    def monotonic_ns(self):
        return self.now_ns

    async def sleep(self, _delay):
        raise ScenarioComplete()


def load_worker(clock):
    tree = ast.parse(SOURCE.read_text(), filename=str(SOURCE))
    definitions = [node for node in tree.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name in FUNCTIONS]
    if {node.name for node in definitions} != FUNCTIONS:
        raise AssertionError("The controller no longer defines the scoped test functions")
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in CONSTANTS:
                constants[target.id] = ast.literal_eval(node.value)
    if set(constants) != CONSTANTS:
        raise AssertionError("The controller no longer defines the scoped test constants")
    namespace = {
        "asyncio": SimpleNamespace(sleep=clock.sleep, wait_for=asyncio.wait_for),
        "time": clock, "math": math, "print": lambda *_args, **_kwargs: None,
        **constants,
    }
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace["frequency_worker"]


class FakeSerial:
    def __init__(self, clock):
        self.clock = clock
        self.snapshot = None
        self.publish(50)

    def publish(self, hz, *, same_arrival_timestamp=False):
        old_ns = self.snapshot["pc_arrival_monotonic_ns"] if self.snapshot else None
        self.clock.now_ns += 1_000_000
        self.snapshot = {
            "pc_arrival_monotonic_ns": old_ns if same_arrival_timestamp else self.clock.now_ns,
            "sa_rates_hz": [hz] * 12,
        }

    def is_running(self):
        return True

    def get_latest_rates_snapshot(self):
        return self.snapshot


class FakeCapture:
    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        self.events.append({"event": event, **copy.deepcopy(fields)})


class FakeDevice:
    connected = True
    client = object()

    def __init__(self, serial, replacement):
        self.serial, self.replacement = serial, replacement
        self.config = {"sf": 20, "sc": [50]}
        self.config_verified = False
        self.running = False
        self.actions = []
        self.fast_sf_count = 0
        self.resume_source_ns = None
        self.after_sf_source_ns = None

    def set_global_frequency(self, hz):
        self.config["sf"] = hz

    async def apply(self, **_kwargs):
        self.config_verified = True
        return [{"status": "OK"} for _ in range(7)]

    async def start(self):
        self.actions.append(("START", self.config["sf"]))
        self.running = True
        self.serial.publish(0)

    async def stop(self):
        self.actions.append(("STOP", None))
        self.running = False

    async def apply_live_current(self, channel, current):
        if channel != 1:
            raise AssertionError("Unexpected stimulation channel")
        self.actions.append(("SC", current))
        await asyncio.sleep(0)
        self.config["sc"] = [current]
        return {"status": "OK"}

    async def apply_live_frequency(self, hz):
        self.actions.append(("SF", hz))
        await asyncio.sleep(0)
        self.config["sf"] = hz
        if hz == 400:
            self.fast_sf_count += 1
            if self.fast_sf_count == 1:
                self.serial.publish(3)
                self.resume_source_ns = self.serial.snapshot["pc_arrival_monotonic_ns"]
            elif self.replacement == "zero_then_positive":
                self.serial.publish(9)
        elif hz == 3:
            # Deliver the changed input before this awaited SF ACK returns.
            if self.replacement in ("zero", "zero_then_positive"):
                self.serial.publish(0)
            elif self.replacement == "subthreshold":
                self.serial.publish(1.18)
            elif self.replacement == "zero_same_timestamp":
                self.serial.publish(0, same_arrival_timestamp=True)
            elif self.replacement == "missing":
                self.serial.snapshot = None
            elif self.replacement == "stale":
                self.serial.clock.now_ns += 251_000_000
            elif self.replacement == "newer_positive":
                self.serial.publish(9)
            elif self.replacement != "unchanged_positive":
                raise AssertionError(f"Unknown scenario: {self.replacement}")
            if self.serial.snapshot is not None:
                self.after_sf_source_ns = self.serial.snapshot["pc_arrival_monotonic_ns"]
        return {"status": "OK"}


class ResumeCurrentGuardTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, replacement):
        clock = FakeClock()
        serial = FakeSerial(clock)
        device = FakeDevice(serial, replacement)
        capture = FakeCapture()
        self.case = SimpleNamespace(clock=clock, serial=serial, device=device, capture=capture)
        ready = asyncio.Event()
        ready.configured_ns = 0
        ready.set()
        worker = load_worker(clock)
        try:
            await asyncio.wait_for(
                worker(serial, device, ready, 11, "sa_ii", arm=True,
                       stale_seconds=0.25, capture=capture), timeout=1.0)
        except ScenarioComplete:
            pass
        return self.case

    async def test_fresh_zero_during_sf_ack_never_restores_current(self):
        case = await self.run_case("zero")
        self.assertNotIn(("SC", 50), case.device.actions)
        self.assertEqual(case.device.config["sc"], [0])

    async def test_fresh_subthreshold_request_keeps_current_muted(self):
        case = await self.run_case("subthreshold")
        self.assertNotIn(("SC", 50), case.device.actions)
        self.assertEqual(case.device.config, {"sf": 400, "sc": [0]})

    async def test_aborted_resume_restores_sf400_without_repeating_sc0(self):
        case = await self.run_case("zero")
        self.assertEqual(case.device.actions,
                         [("START", 50), ("SC", 0), ("SF", 400), ("SF", 3), ("SF", 400)])
        fast_acks = [e for e in case.capture.events
                     if e["event"] == "sf_ack" and e["frequency_hz"] == 400]
        self.assertEqual(len(fast_acks), 2)
        self.assertTrue(all(e["status"] == "OK" for e in fast_acks))
        fast_requests = [e for e in case.capture.events
                         if e["event"] == "sf_requested" and e["frequency_hz"] == 400]
        self.assertEqual(fast_requests[-1]["source_sample_ns"], case.device.after_sf_source_ns)

    async def test_changed_snapshot_sharing_arrival_timestamp_restores_muted_sf400(self):
        case = await self.run_case("zero_same_timestamp")
        self.assertEqual(case.device.after_sf_source_ns, case.device.resume_source_ns)
        self.assertNotIn(("SC", 50), case.device.actions)
        self.assertEqual([value for command, value in case.device.actions if command == "SF"],
                         [400, 3, 400])
        self.assertEqual(case.device.config, {"sf": 400, "sc": [0]})

    async def test_later_positive_input_can_resume_after_an_aborted_restore(self):
        case = await self.run_case("zero_then_positive")
        self.assertEqual(case.device.actions,
                         [("START", 50), ("SC", 0), ("SF", 400), ("SF", 3),
                          ("SF", 400), ("SF", 9), ("SC", 50)])
        self.assertEqual(case.device.config, {"sf": 9, "sc": [50]})

    async def test_missing_input_after_sf_ack_propagates_error_without_sc_restore(self):
        with self.assertRaisesRegex(TimeoutError, "before current restore"):
            await self.run_case("missing")
        self.assertNotIn(("SC", 50), self.case.device.actions)
        self.assertEqual(self.case.device.config["sc"], [0])

    async def test_stale_input_after_sf_ack_propagates_error_without_sc_restore(self):
        with self.assertRaisesRegex(TimeoutError, "before current restore"):
            await self.run_case("stale")
        self.assertNotIn(("SC", 50), self.case.device.actions)
        self.assertEqual(self.case.device.config["sc"], [0])

    async def test_normal_positive_resume_preserves_sf_then_sc_sequence(self):
        case = await self.run_case("unchanged_positive")
        self.assertEqual(case.device.actions,
                         [("START", 50), ("SC", 0), ("SF", 400), ("SF", 3), ("SC", 50)])
        self.assertEqual(case.device.config, {"sf": 3, "sc": [50]})
        restores = [e for e in case.capture.events
                    if e["event"] == "sc_ack" and e["current_command"] == 50]
        self.assertEqual(len(restores), 1)
        self.assertEqual(restores[0]["status"], "OK")

    async def test_current_restore_uses_fresh_sample_without_rewriting_sf_provenance(self):
        case = await self.run_case("newer_positive")
        restore = next(e for e in case.capture.events
                       if e["event"] == "sc_requested" and e["current_command"] == 50)
        sf3 = next(e for e in case.capture.events
                   if e["event"] == "sf_requested" and e["frequency_hz"] == 3)
        self.assertEqual(restore["source_sample_ns"], case.device.after_sf_source_ns)
        self.assertEqual(sf3["source_sample_ns"], case.device.resume_source_ns)
        self.assertEqual(case.device.actions[-2:], [("SC", 50), ("SF", 9)])
        self.assertEqual(case.device.config, {"sf": 9, "sc": [50]})


if __name__ == "__main__":
    unittest.main()
