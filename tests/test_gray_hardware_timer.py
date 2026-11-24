"""Device-free checks of the hardware-TCA buffered Gray protocol and schedule."""
import unittest

from oscilloscope.test_gray_timecode import parser, plan
from oscilloscope.decode_gray_timecode import decode_pulses, gray_to_binary


def hardware_gray_pulses(frame_count):
    """Reference for 1ms hardware-PWM cells, NOT a firmware register test."""
    all_pulses = []
    for index in range(frame_count):
        t0 = .05 + index*.1
        gray = index ^ (index >> 1)
        all_pulses.append((t0, .004))
        for bit in range(16):
            if gray & (1 << (15-bit)):
                all_pulses.append((t0+.007+bit*.005, .001))
        all_pulses.append((t0+.090, .007))
    return sorted(all_pulses)


class HardwareGrayTests(unittest.TestCase):
    def test_explicit_profile_is_buffered_and_only_ch2(self):
        p = parser()
        args = p.parse_args([
            "--expected-frame-us", "100000",
            "--expected-frame-mode", "10HZ_HW_TCA_BUFFERED",
            "--buffered-frames",
        ])
        data = plan(args, p)
        self.assertEqual(data["expected_frame_mode"], "10HZ_HW_TCA_BUFFERED")
        self.assertEqual(data["nominal_frame_s"], .1)
        self.assertEqual(data["channels"], [2])
        self.assertTrue(data["no_stimulation"])

    def test_profile_requires_buffer_optin(self):
        p = parser()
        with self.assertRaises(SystemExit):
            plan(p.parse_args([
                "--expected-frame-mode", "10HZ_HW_TCA_BUFFERED",
            ]), p)

    def test_gray_reference_frame_unchanged(self):
        pulses = hardware_gray_pulses(8)
        frames = decode_pulses(pulses, frame_s=.1)
        self.assertEqual(len(frames), 8)
        self.assertEqual(
            [int(f["counter_mod_65536"]) for f in frames], list(range(8)))

    def test_timer_fractional_tick_budget(self):
        # 20MHz / 64 = 312500 timer ticks/s; every pair of
        # 1ms PWM cells consumes exactly 625 ticks.
        self.assertEqual((312+313)*1000, 2*312500)
        self.assertEqual((312+313)*50, 31250)  # exactly 100ms


if __name__ == "__main__":
    unittest.main()
