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
        # PlatformIO Nano Every defaults to 16MHz: TCA DIV64 is 250kHz.
        # An exact 100ms frame is 100 cells of exactly 250 ticks.
        self.assertEqual(250*100, 16000000//64//10)
        # If explicitly configured for 20MHz, an adjacent 312/313
        # tick pair is exactly 2ms and 50 pairs make 100ms.
        self.assertEqual((312+313)*50, 20000000//64//10)


if __name__ == "__main__":
    unittest.main()
