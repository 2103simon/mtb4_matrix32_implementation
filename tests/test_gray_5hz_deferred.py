"""Hardware-free checks for the opt-in 5Hz deferred Gray experiment."""
import unittest

from oscilloscope.decode_gray_timecode import decode_pulses
from oscilloscope.test_gray_timecode import parser, plan
from oscilloscope.audit_gray_pulse_timing import geometry


def gray_waveform(*, period):
    pulses = []
    frames = []
    log = {}
    for i in range(5):
        t = i*period
        gray = i^(i>>1)
        pulses.append((t, .004))
        for bit in range(16):
            if gray & (1 << (15-bit)):
                pulses.append((t + .007 + bit*.005, .001))
        pulses.append((t + .090, .007))
        frames.append({
            "counter_mod_65536": i, "gray_hex": f"{gray:04X}",
            "bits_msb_first": f"{gray:016b}", "scope_start_s": t,
        })
        log[i] = {
            "counter": i, "gray_hex": f"{gray:04X}",
            "nano_unwrapped_us": 500000+int(round(period*1e6))*i,
            "overruns": 0,
        }
    return sorted(pulses), frames, log


class Gray5HzTests(unittest.TestCase):
    def test_200ms_waveform_is_decoded_without_false_100ms_spacing(self):
        pulses, _, _ = gray_waveform(period=.2)
        frames = decode_pulses(pulses, frame_s=.2)
        self.assertEqual(len(frames), 5)
        for item in frames[1:]:
            self.assertAlmostEqual(item["scope_start_delta_s"], .2)
            self.assertAlmostEqual(item["expected_start_delta_s"], .2)
            self.assertAlmostEqual(item["spacing_residual_s"], 0.)

    def test_audit_compares_physical_and_digital_to_200ms(self):
        pulses, frames, log = gray_waveform(period=.2)
        _, spans, result = geometry(
            {"decoded_gray": {"frames": frames}}, log, pulses,
            frame_s=.2)
        self.assertEqual(len(spans), 4)
        self.assertLess(
            abs(result["physical_frame_spacing_error_us"]["median"]), .001)
        self.assertEqual(result["nominal_frame_s"], .2)
        self.assertGreater(
            result["phase_warp_test"]["heldout_data_rises"], 0)

    def test_plan_fails_for_mismatched_clock_profile(self):
        p = parser()
        args = p.parse_args([
            "--expected-frame-us", "200000",
            "--expected-frame-mode", "10HZ_ORIGINAL",
        ])
        with self.assertRaises(SystemExit):
            plan(args, p)

    def test_opt_in_plan_documents_no_stimulation(self):
        p = parser()
        args = p.parse_args([
            "--expected-frame-us", "200000",
            "--expected-frame-mode", "5HZ_DEFERRED",
            "--points", "1000000",
            "--time-div-us", "2000000",
        ])
        details = plan(args, p)
        self.assertEqual(details["nominal_frame_s"], .2)
        self.assertEqual(details["expected_frame_mode"], "5HZ_DEFERRED")
        self.assertTrue(details["no_stimulation"])


if __name__ == "__main__":
    unittest.main()
