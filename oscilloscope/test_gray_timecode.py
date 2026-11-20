"""Scope-only Gray timecode experiment: command Nano, capture CH2.

Requires GRAY_TC_V1 firmware on Arduino Nano Every. CH2 = Nano D7
marker and trigger; PROBE COMP is disconnected.
Matrix32 is never connected/commanded.
Default dry-run; --run is an explicit hardware opt-in.
USBTMC can block: invoke under external 'timeout'.
"""
import argparse
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

# Allow direct invocation as python oscilloscope/test_gray_timecode.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decode_gray_timecode import decode_csv
from record_scope import (
    acquire, atomic_json, configure, scope_settings, validate_csv, verify,
)
from scope_device import resolve_scope_device, verify_scope_identity
from tek_mdo_usb import capture, query


def utc():
    return datetime.now(timezone.utc).isoformat()


class NanoLog:
    """Single serial reader, command ACK routing, and append-only receive log."""

    def __init__(self, port, path, baud=115200):
        self.port = port
        self.path = Path(path)
        self.baud = baud
        self.acks = queue.Queue()
        self.closed = threading.Event()
        self.next_sequence = 0
        self.serial = None
        self.thread = None
        self.logfile = None
        self.reader_error = None

    def __enter__(self):
        import serial  # pyserial is only needed for --run
        self.logfile = self.path.open("w", encoding="utf-8")
        try:
            self.serial = serial.Serial(self.port, self.baud, timeout=0.05,
                                        write_timeout=1.0)
            self.thread = threading.Thread(target=self._read,
                                           name="gray-nano-serial", daemon=True)
            self.thread.start()
            return self
        except BaseException:
            self.logfile.close()
            raise

    def _read(self):
        try:
            while not self.closed.is_set():
                raw = self.serial.readline()
                if not raw:
                    continue
                observed = {
                    "received_monotonic_ns": time.monotonic_ns(),
                    "received_utc": utc(),
                    "line": raw.decode("ascii", errors="replace").strip(),
                }
                self.logfile.write(json.dumps(observed) + "\n")
                self.logfile.flush()
                line = observed["line"]
                if line.startswith(("ACK ", "ERR ")):
                    self.acks.put(line)
        except BaseException as exc:
            self.reader_error = f"{type(exc).__name__}: {exc}"

    def command(self, op, *, timeout=5.0):
        self.next_sequence += 1
        seq = self.next_sequence
        self.serial.write(f"{op} {seq}\n".encode("ascii"))
        self.serial.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.reader_error:
                raise RuntimeError(f"Nano reader failed: {self.reader_error}")
            try:
                line = self.acks.get(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            tokens = line.split()
            if len(tokens) >= 3 and tokens[0] == "ACK" and tokens[1] == op and tokens[2] == str(seq):
                return line
            if len(tokens) >= 2 and tokens[0] == "ERR":
                raise RuntimeError(f"Nano rejected command: {line}")
            # Ignore late stale replies; they are preserved in the JSONL log.
        raise TimeoutError(f"No matching ACK {op} {seq} from Nano")

    def synchronize(self, *, timeout=8.0):
        # Opening Nano serial may reset firmware. PING is idempotent; START
        # intentionally is not retried after ambiguous timeout.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                return self.command("PING", timeout=min(0.6, max(0.1, deadline - time.monotonic())))
            except TimeoutError:
                pass
        raise TimeoutError(f"Nano did not respond to PING on {self.port}")

    def __exit__(self, *exc):
        self.closed.set()
        if self.thread is not None:
            self.thread.join(timeout=1.0)
        if self.serial is not None:
            self.serial.close()
        if self.logfile is not None:
            self.logfile.close()


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--nano-port", default="auto",
                   help="Explicit serial node recommended if Feather is also attached")
    p.add_argument("--scope-device", default="auto")
    p.add_argument("--count", type=int, default=1,
                   help="Finite records: 1 first, then 3 for repeated test")
    p.add_argument("--points", type=int, default=100000)
    p.add_argument("--time-div-us", type=float, default=20000,
                   help="200 ms span by default; >=1 complete 100 ms frame")
    p.add_argument("--acquire-timeout", type=float, default=15)
    p.add_argument("--label", default="gray_marker_baseline")
    p.add_argument("--output-root", type=Path, default=Path("data/recordings"))
    p.add_argument("--run", action="store_true")
    return p


def plan(args, p):
    if not 1 <= args.count <= 50:
        p.error("--count must be 1..50")
    if args.points < 2 or args.points > 20000000:
        p.error("--points must be 2..20000000")
    # Compare the user-provided timebase directly. Multiplying by 1e-6
    # produces 0.19999999999999998 for the valid 20,000 us/div case.
    if not (math.isfinite(args.time_div_us) and args.time_div_us >= 20000):
        p.error("timebase must produce >=200 ms window (>=20000 us/div)")
    if not math.isfinite(args.acquire_timeout) or not 0 < args.acquire_timeout <= 120:
        p.error("invalid --acquire-timeout")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.label):
        p.error("invalid --label")
    # record_scope.configure()/verify() interface (no Matrix32 settings).
    args.channels = [2]
    args.scales = {2: 2.0}
    args.trigger_channel = 2
    args.trigger_edge = "rising"
    args.trigger_level = 1.0
    args.trigger_position_pct = 50.0
    return {
        "schema_version": 1, "experiment": "GRAY_TC_V1",
        "source": "Arduino Nano Every D7, 10 Hz framed Gray counter",
        "channel_2": "Nano D7; 2 V/div, expected 0-5 V; trigger rising at +1 V",
        "probe_comp": "disconnected",
        "channels": args.channels, "points": args.points,
        "time_div_us": args.time_div_us, "count": args.count,
        "nominal_window_s": 10 * args.time_div_us * 1e-6,
        "nano_port": args.nano_port, "scope_device": args.scope_device,
        "acquire_timeout_s": args.acquire_timeout,
        "no_stimulation": True,
        "not_continuous": True,
    }


def run(args, requested):
    from matrix32.timing_marker import discover_timing_marker_port
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_" + args.label
    root = args.output_root / run_id
    (root / "waveforms").mkdir(parents=True, exist_ok=False)
    metadata = {
        "plan": requested, "run_id": run_id, "status": "started",
        "started_utc": utc(), "records": [],
        "clock_synchronization_verified": False,
        "sample_gap_free": False,
    }
    git = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
    metadata["git_commit"] = git.stdout.strip() if git.returncode == 0 else None
    nano_start_attempted = False
    try:
        port = (discover_timing_marker_port() if args.nano_port == "auto"
                else args.nano_port)
        metadata["nano_port"] = port
        print(f"[NANO] Serial port {port}", flush=True)
        with NanoLog(port, root / "nano_serial.jsonl") as nano:
            metadata["ping"] = nano.synchronize()
            info = nano.command("INFO")
            metadata["nano_info"] = info
            if "FW GRAY_TC_V1" not in info or "PIN 7" not in info:
                raise RuntimeError("Wrong Nano firmware or pin; upload arduino_nano PlatformIO project")
            if "RUN 1" in info:
                nano.command("STOP")
            # No automatic retry of START: ambiguous response may mean
            # generator already started. Preserve serial log on failure.
            nano_start_attempted = True
            metadata["start_ack"] = nano.command("START")
            print(f"[NANO] Gray-code timebase running ({metadata['start_ack']})",
                  flush=True)
            try:
                node = resolve_scope_device(args.scope_device)
                metadata["scope_node"] = str(node)
                with open(node, "r+b", buffering=0) as f:
                    metadata["scope_identity"] = verify_scope_identity(query(f, "*IDN?"))
                    print(f"[SCOPE] {metadata['scope_identity']}", flush=True)
                    configure(f, args)
                    metadata["scope_settings"] = scope_settings(
                        f, args.channels, args.trigger_channel,
                        horizontal_position=True)
                    verify(metadata["scope_settings"], args)
                    for index in range(1, args.count + 1):
                        print(f"[SCOPE] Acquiring {index}/{args.count}", flush=True)
                        row = {"index": index, "status": "started", "started_utc": utc()}
                        metadata["records"].append(row)
                        try:
                            t0 = time.monotonic()
                            row["acquisition"] = acquire(f, args.acquire_timeout)
                            row["acquisition_host_s"] = time.monotonic() - t0
                            row["channels"] = {}
                            for ch in (2,):
                                file = root / "waveforms" / f"record_{index:05d}_ch{ch}.csv"
                                t1 = time.monotonic()
                                report = capture(f, ch, args.points, file)
                                row["channels"][f"CH{ch}"] = {
                                    "path": str(file.relative_to(root)),
                                    "transfer_and_write_s": time.monotonic() - t1,
                                    "transfer": report,
                                    "waveform": validate_csv(file, args.points),
                                }
                            marker_csv = root / row["channels"]["CH2"]["path"]
                            row["decoded_gray"] = decode_csv(marker_csv)
                            row["status"] = "captured"
                            row["completed_utc"] = utc()
                            print(
                                f"[GRAY] Record {index}: "
                                f"{row['decoded_gray']['decoded_frame_count']} complete frames; "
                                f"codes {[x['counter_mod_65536'] for x in row['decoded_gray']['frames']]}",
                                flush=True)
                        except BaseException as exc:
                            row["status"] = "failed"
                            row["error"] = f"{type(exc).__name__}: {exc}"
                            raise
                        finally:
                            atomic_json(root / "metadata.json", metadata)
                metadata["status"] = "complete"
            finally:
                if nano_start_attempted:
                    try:
                        metadata["stop_ack"] = nano.command("STOP", timeout=2.0)
                    except BaseException as exc:
                        metadata["nano_stop_error"] = f"{type(exc).__name__}: {exc}"
                        # This is a bench timing signal, not stimulation.
                        print(f"[NANO] STOP acknowledgement failed: {exc}", flush=True)
    except BaseException as exc:
        metadata["status"] = "failed"
        metadata["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        metadata["finished_utc"] = utc()
        atomic_json(root / "metadata.json", metadata)
        print(f"[GRAY] Saved {root} ({metadata['status']})", flush=True)
    return metadata


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    requested = plan(args, p)
    print(json.dumps(requested, indent=2), flush=True)
    if not args.run:
        print("DRY RUN: no serial port or scope accessed.")
        return
    run(args, requested)


if __name__ == "__main__":
    main()
