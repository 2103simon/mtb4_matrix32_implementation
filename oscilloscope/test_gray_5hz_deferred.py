"""Clock-only A/B acquisition: Nano Every 5Hz deferred-serial Gray profile.

No Matrix32, no Feather, no CH1 stimulation. Exactly the same 20-s,
3-epoch / 30+30 SYNC and 60-s pause design as the original 10Hz trial.
New profile must first be explicitly flashed with:
  cd arduino_nano
  pio run -e nano_every_5hz_deferred -t upload --upload-port /dev/ttyACM1
Then dry-run and explicitly --run this wrapper. A mismatched Nano INFO
fails closed before START.

Note: serial commands are deliberately only processed during idle in
the experimental profile; therefore SYNC RTT/offset may worsen even if
physical D7 pulse scheduling becomes more regular.
"""
import sys
from test_gray_epoch_pause import main as epoch_main


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    return epoch_main([
        "--expected-frame-us", "200000",
        "--expected-frame-mode", "5HZ_DEFERRED",
        "--label", "gray_5hz_deferred_epoch_20s",
    ] + list(argv))


if __name__ == "__main__":
    main()
