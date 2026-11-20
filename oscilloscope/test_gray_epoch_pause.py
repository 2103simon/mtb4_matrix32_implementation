"""Three 30-second Gray scope records, 60-second deliberate pauses.

Each scope record gets fresh CRC32 Nano SYNC observations immediately
before arming and after acquiring, BEFORE the waveform download.
The Nano generator remains running throughout the experiment.
No Matrix32 stimulation or Feather communication is performed.

Dry run by default; --run explicitly opts in to real hardware.
The scope must support 3 s/div and 1,000,000-point SINGLE SEQUENCE;
the underlying scope code verifies actual SCPI configuration.
"""
import sys
from test_gray_timecode import main as gray_main


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    defaults = [
        "--count", "3",
        "--points", "1000000",
        "--time-div-us", "3000000",
        "--acquire-timeout", "90",
        "--per-record-epochs",
        "--epoch-sync-count", "30",
        "--pause-between-s", "60",
        "--sync-transport", "binary",
        "--label", "gray_epoch_pause_30s",
    ]
    return gray_main(defaults + list(argv))


if __name__ == "__main__":
    main()
