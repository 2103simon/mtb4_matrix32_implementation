"""Standalone Nano-to-host clock synchronization bench test; no scope required.

Nano Every only. Produces FRAME receive observations and four timestamp
SYNC probes. Does not access Matrix32 or the Feather M4. Default dry-run.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from communication.serial_ports import resolve_port
from analyze_nano_clock import analyze_run
from oscilloscope.test_gray_timecode import NanoLog


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--duration-s", type=float, default=30.0)
    p.add_argument("--sync-interval-s", type=float, default=0.25)
    p.add_argument("--sync-transport", choices=("text", "binary"), default="binary",
                   help="CRC32 binary default; text retained for legacy comparison")
    p.add_argument("--nano-port", default=None, help="Optional device override")
    p.add_argument("--output-root", type=Path, default=Path("data/recordings"))
    p.add_argument("--run", action="store_true", help="Open Nano and issue START/SYNC/STOP")
    args = p.parse_args(argv)
    if not 10 <= args.duration_s <= 600:
        p.error("--duration-s must be between 10 and 600")
    if not 0.05 <= args.sync_interval_s <= 10:
        p.error("--sync-interval-s must be between 0.05 and 10")
    if not args.run:
        print("DRY RUN: no Nano accessed; use --run to conduct the bench test.")
        return
    device = resolve_port(args.nano_port, "nano")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_nano_clock_sync"
    root = args.output_root / run_id
    root.mkdir(parents=True, exist_ok=False)
    metadata = {
        "run_id": run_id, "experiment": "NANO_HOST_SYNC_V1",
        "port": device, "duration_requested_s": args.duration_s,
        "sync_interval_requested_s": args.sync_interval_s,
        "clock": "time.monotonic_ns",
        "sync_transport": args.sync_transport,
        "status": "started",
    }
    print(f"[NANO] {device}", flush=True)
    try:
        with NanoLog(device, root/"nano_serial.jsonl") as nano:
            metadata["ping"] = nano.synchronize()
            info = nano.command("INFO")
            metadata["nano_info"] = info
            if "FW GRAY_TC_V1" not in info or "PIN 7" not in info:
                raise RuntimeError("Wrong Nano firmware; flash arduino_nano PlatformIO project")
            start_attempted = False
            try:
                start_attempted = True
                metadata["start_ack"] = nano.command("START")
                end = time.monotonic() + args.duration_s
                metadata["sync_count"] = 0
                while time.monotonic() < end:
                    if args.sync_transport == "binary":
                        nano.binary_sync_exchange()
                    else:
                        nano.sync_exchange()
                    metadata["sync_count"] += 1
                    remaining = end - time.monotonic()
                    if remaining > 0:
                        time.sleep(min(remaining, args.sync_interval_s))
            finally:
                # Nano STOP only; not a Matrix32 emergency stop.
                if start_attempted:
                    try:
                        metadata["stop_ack"] = nano.command("STOP", timeout=2.0)
                    except BaseException as stop_exc:
                        metadata["nano_stop_error"] = repr(stop_exc)
                        print(f"[NANO] STOP not acknowledged: {stop_exc}", flush=True)
        metadata["nano_stream_diagnostics"] = {
            "binary_crc_errors": nano.decoder.bad_crc_count,
            "discarded_stream_bytes": nano.decoder.discarded_byte_count,
            "serial_reader_error": nano.reader_error,
        }
        report = analyze_run(root)
        result_path = root / "nano_host_clock.json"
        result_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        metadata["status"] = "complete"
        print(f"[SYNC] {metadata['sync_count']} four-timestamp measurements", flush=True)
        print(f"[PASSIVE] drift {report['passive']['drift_ppm_nano_to_host']:.1f} ppm",
              flush=True)
        if report["active"]:
            fit = report["active"]
            print(f"[ACTIVE] drift {fit['drift_ppm_nano_to_host']:.1f} ppm; "
                  f"RMS {fit['rms_residual_us']:.1f} us; "
                  f"best exchange halfwidth {fit['best_exchange_offset_halfwidth_us']:.1f} us",
                  flush=True)
        else:
            print(f"[ACTIVE] Not qualified: {report.get('active_error')}", flush=True)
        print(f"[SYNC] Saved {root}", flush=True)
    except BaseException as exc:
        metadata["status"] = "failed"
        metadata["error"] = repr(exc)
        raise
    finally:
        (root / "metadata.json").write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
