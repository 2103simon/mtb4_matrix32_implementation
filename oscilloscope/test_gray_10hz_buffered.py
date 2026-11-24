"""Three scope-only 20s Gray records with 10Hz Nano RAM-buffered telemetry.

The Nano emits physical Gray on D7 during each capture, stores FRAME
timestamps in its 256-entry SRAM buffer, and transmits FRAME lines ONLY
after the scope acquisition has completed. Each subsequent recording
gets a fresh BUFFER_BEGIN after its pre-SYNC batch. No Matrix32 output.

Flash PlatformIO environment nano_every_10hz_buffered explicitly.
Dry-run default; --run is required for connected hardware.
"""
import sys
from test_gray_epoch_pause import main as epoch_main


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    return epoch_main([
        "--expected-frame-us", "100000",
        "--expected-frame-mode", "10HZ_BUFFERED",
        "--buffered-frames",
        "--label", "gray_10hz_buffered_epoch_20s",
    ] + list(argv))


if __name__ == "__main__":
    main()
