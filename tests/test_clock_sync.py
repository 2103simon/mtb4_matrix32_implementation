"""Device-free regression tests for Nano/PC clock synchronization."""
import json
from pathlib import Path
import tempfile
import unittest

from communication.clock_sync import (
    active_clock_fit, nano_us_to_host_ns, passive_diagnostics,
    read_active_exchanges,
)


def synthetic_exchanges(n=24, *, slope=1.00042, offset_ns=123_000_000_000):
    rows = []
    for k in range(n):
        nano_rx = 1_000_000 + k*1_000_000
        nano_tx = nano_rx + 150
        # True mapping host = slope*nano_ns + offset_ns.
        # Asymmetric USB delays oscillate but are always nonnegative.
        up_ns = 120000 + (k % 4)*3000
        down_ns = 140000 + (k % 3)*4000
        t2_host = offset_ns + 1000*slope*nano_rx
        t3_host = offset_ns + 1000*slope*nano_tx
        rows.append({
            "seq": k+1,
            "t1_host_ns": round(t2_host-up_ns),
            "t4_host_ns": round(t3_host+down_ns),
            "t2_nano_us": nano_rx,
            "t3_nano_us": nano_tx,
        })
    return rows


class ClockSyncTests(unittest.TestCase):
    def test_active_clock_ratio_offset_and_bounds(self):
        observations = synthetic_exchanges()
        fit = active_clock_fit(observations)
        self.assertTrue(fit["host_clock_mapping_estimated"])
        self.assertFalse(fit["host_clock_synchronized"])
        self.assertAlmostEqual(fit["slope_host_ns_per_nano_us"], 1000.42, delta=0.25)
        inferred = nano_us_to_host_ns(13_000_000, fit)
        expected = 123_000_000_000 + 13_000_000*1000*1.00042
        self.assertAlmostEqual(inferred, expected, delta=100_000)
        self.assertGreater(fit["best_exchange_offset_halfwidth_us"], 10)
        self.assertLess(fit["best_exchange_offset_halfwidth_us"], 300)

    def test_insufficient_span_and_samples_fail(self):
        with self.assertRaisesRegex(ValueError, "least|Need at least"):
            active_clock_fit(synthetic_exchanges(4))
        with self.assertRaisesRegex(ValueError, "span"):
            active_clock_fit(synthetic_exchanges(9), min_span_s=20)

    def test_passive_only_cannot_calibrate_offset(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nano_serial.jsonl"
            lines = []
            for k in range(30):
                t = 100_000 + 100_000*k
                host_ns = 7_000_000_000 + round(1000.2 * t) + (k%3)*1000
                n = k
                gray = n ^ (n >> 1)
                lines.append(json.dumps({
                    "line": f"FRAME {n} GRAY {gray:X} EDGE_US {t} OVERRUNS 0",
                    "received_monotonic_ns": host_ns,
                }))
            path.write_text("\n".join(lines)+"\n", encoding="utf-8")
            fit = passive_diagnostics(path)
            self.assertFalse(fit["host_clock_synchronized"])
            self.assertFalse(fit["offset_calibrated"])
            with self.assertRaisesRegex(ValueError, "Passive"):
                nano_us_to_host_ns(100_000, fit)

    def test_parse_sync_and_micros_rollover(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nano_serial.jsonl"
            lines = []
            for k, (t2,t3) in enumerate([
                (4294967000, 4294967100),
                (4294967200, 4),
                (200, 300),
            ]):
                lines.append(json.dumps({
                    "type": "sync_exchange", "sequence_id": k+1,
                    "nano_receive_us": t2, "nano_transmit_us": t3,
                    "host_send_monotonic_ns": 10**12+k*1_000_000,
                    "host_receive_monotonic_ns": 10**12+k*1_000_000+200_000,
                }))
            path.write_text("\n".join(lines)+"\n", encoding="utf-8")
            rows = read_active_exchanges(path)
            self.assertEqual(len(rows),3)
            self.assertEqual(rows[1]["t3_nano_us"], 2**32+4)
            self.assertEqual(rows[2]["t2_nano_us"], 2**32+200)

    def test_negative_roundtrip_rejected(self):
        rows=synthetic_exchanges()
        rows[2]["t4_host_ns"]=rows[2]["t1_host_ns"]+1000
        with self.assertRaisesRegex(ValueError, "Negative roundtrip"):
            active_clock_fit(rows)


if __name__ == "__main__":
    unittest.main()
