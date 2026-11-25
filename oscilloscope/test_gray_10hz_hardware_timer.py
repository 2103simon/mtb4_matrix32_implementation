"""Three 20-second clock-only Nano hardware-timed Gray reference captures.

Requires PlatformIO nano_every_10hz_hardware_timer explicitly.
D7 is driven by ATmega4809 TCA0-WO1 hardware PWM, not digitalWrite.
A TCA overflow ISR only prepares the NEXT 1ms cell and records EDGE_US
shortly AFTER the hardware START transition. The ISR timestamp is NOT
a latched independent physical edge time. Nano FRAME logs are buffered,
then transmitted strictly outside each scope acquisition.

Matrix32/Feather are never operated. Dry-run default; --run explicit.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oscilloscope.test_gray_epoch_pause import main as epoch_main


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    return epoch_main([
        "--expected-frame-us", "100000",
        "--expected-frame-mode", "10HZ_HW_TCA_BUFFERED",
        "--buffered-frames",
        "--label", "gray_10hz_hardware_timer_epoch_20s",
    ] + list(argv))


if __name__ == "__main__":
    main()
