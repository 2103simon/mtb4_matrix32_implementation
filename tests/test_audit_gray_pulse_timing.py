"""Hardware-free checks for CH2 physical Gray geometry audit."""
import unittest

from oscilloscope.audit_gray_pulse_timing import geometry, summarize


class GrayPulseGeometryTests(unittest.TestCase):
    def test_statistic_empty(self):
        self.assertEqual(summarize([])["n"], 0)
        self.assertIsNone(summarize([])["std"])

    def test_known_physical_pulse_widths_and_frame_timing(self):
        # Three complete Gray=0 frames: START at 0 ms, END at 90 ms,
        # then a 100-ms frame repetition. No Gray data bits are high.
        frames = []
        pulses = []
        log = {}
        for n in range(3):
            t = n*.1
            frames.append({
                "scope_start_s": t,
                "counter_mod_65536": n,
                "gray_hex": f"{n^(n>>1):04X}",
                "bits_msb_first": format(n^(n>>1), "016b"),
            })
            log[n] = {
                "counter": n,
                "gray_hex": f"{n^(n>>1):04X}",
                "nano_unwrapped_us": 200000+n*100000,
                "overruns": 0,
            }
            pulses.append((t, .004))
            gray = n^(n>>1)
            for bit in range(16):
                if (gray >> (15-bit)) & 1:
                    pulses.append((t+.007+bit*.005, .001))
            pulses.append((t+.090, .007))
        record = {"decoded_gray": {"frames": frames}}
        widths, spaces, report = geometry(record, log, pulses)
        self.assertEqual(report["decoded_frames"], 3)
        self.assertEqual(report["unassigned_complete_high_pulses"], 0)
        self.assertEqual(report["unmatched_roles"], {})
        self.assertEqual(report["width_start_us"]["n"], 3)
        self.assertEqual(report["width_end_us"]["n"], 3)
        self.assertAlmostEqual(report["width_start_us"]["median"], 4000)
        self.assertAlmostEqual(report["width_end_us"]["median"], 7000)
        self.assertEqual(len(spaces), 2)
        self.assertGreater(report["physical_inter_pulse_low_gap_us"]["n"], 0)
        # Gray zero bits mean gaps vary; START to END low interval in
        # the all-zero first frame is 86 ms.
        frame0_end = next(r for r in widths
                          if r["frame"] == 0 and r["role"] == "END")
        self.assertAlmostEqual(
            frame0_end["inter_pulse_low_gap_us"], 86000, places=4)
        self.assertAlmostEqual(
            report["physical_frame_spacing_error_us"]["std"], 0.,
            places=5)
        self.assertAlmostEqual(
            report["nano_minus_rate_scaled_scope_spacing_us"]["std"], 0.,
            places=4)
        self.assertFalse(report["physical_gpio_edge_hardware_timestamped"])

    def test_timestamp_jitter_is_visible_even_with_constant_physical_pulses(self):
        frames, pulses, log = [], [], {}
        for i in range(6):
            t = .1*i
            g = i^(i>>1)
            frames.append({
                "counter_mod_65536": i,
                "gray_hex": f"{g:04X}",
                "bits_msb_first": f"{g:016b}",
                "scope_start_s": t,
            })
            pulses.append((t, .004))
            for bit in range(16):
                if g & (1 << (15-bit)):
                    pulses.append((t+.007+.005*bit, .001))
            pulses.append((t+.090, .007))
            log[i] = {
                "counter": i, "gray_hex": f"{g:04X}",
                "nano_unwrapped_us": 200000+i*100000+(500 if i == 3 else 0),
                "overruns": 0,
            }
        _, _, result = geometry(
            {"decoded_gray": {"frames": frames}}, log, pulses)
        self.assertLess(result["physical_frame_spacing_error_us"]["std"], 1e-6)
        self.assertGreater(
            result["nano_minus_rate_scaled_scope_spacing_us"]["std"], 100)

    def test_ambiguous_full_counter_fails_closed(self):
        record = {"decoded_gray": {"frames": [{
            "counter_mod_65536": 0, "gray_hex": "0000",
            "bits_msb_first": "0"*16, "scope_start_s": 0.,
        }]}}
        nano = {
            0: {"gray_hex": "0000"}, 65536: {"gray_hex": "0000"}
        }
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            geometry(record, nano, [(0., .004), (.09, .007)])


if __name__ == "__main__":
    unittest.main()
