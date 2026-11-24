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

from communication.nano_binary_protocol import (
    MixedNanoStreamDecoder, pack_sync_request,
)
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
        self.binary_acks = queue.Queue()
        self.closed = threading.Event()
        self.next_sequence = 0
        self.serial = None
        self.thread = None
        self.logfile = None
        self.reader_error = None
        self.log_lock = threading.Lock()
        self.decoder = MixedNanoStreamDecoder()

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

    def _write_log(self, row):
        with self.log_lock:
            self.logfile.write(json.dumps(row) + "\n")
            self.logfile.flush()

    def _read(self):
        try:
            while not self.closed.is_set():
                raw = self.serial.read(max(1, int(self.serial.in_waiting)))
                if not raw:
                    continue
                # Time closest to receipt of this chunk; NOT a hardware
                # USB-controller arrival timestamp.
                arrival_ns = time.monotonic_ns()
                for kind, value in self.decoder.feed(raw):
                    if kind == "binary_sync_ack":
                        seq, rx_us, tx_us = value
                        self.binary_acks.put((seq, rx_us, tx_us, arrival_ns))
                        self._write_log({
                            "type": "binary_sync_ack",
                            "received_monotonic_ns": arrival_ns,
                            "sequence_id": seq,
                            "nano_receive_us": rx_us,
                            "nano_transmit_us": tx_us,
                            "crc32_verified": True,
                        })
                        continue
                    observed = {
                        "received_monotonic_ns": arrival_ns,
                        "received_utc": utc(),
                        "line": value,
                    }
                    self._write_log(observed)
                    if value.startswith(("ACK ", "ERR ")):
                        self.acks.put((value, arrival_ns))
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
                line, _ = self.acks.get(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            tokens = line.split()
            if len(tokens) >= 3 and tokens[0] == "ACK" and tokens[1] == op and tokens[2] == str(seq):
                return line
            if len(tokens) >= 2 and tokens[0] == "ERR":
                raise RuntimeError(f"Nano rejected command: {line}")
            # Ignore late stale replies; they are preserved in the JSONL log.
        raise TimeoutError(f"No matching ACK {op} {seq} from Nano")

    def sync_exchange(self, *, timeout=1.0):
        """Collect 4 timestamps: PC monotonic ns t1/t4, Nano micros t2/t3."""
        self.next_sequence += 1
        seq = self.next_sequence
        t1 = time.monotonic_ns()
        self.serial.write(f"SYNC {seq}\n".encode("ascii"))
        self.serial.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.reader_error:
                raise RuntimeError(f"Nano reader failed: {self.reader_error}")
            try:
                line, t4 = self.acks.get(
                    timeout=min(0.1, max(0.001, deadline - time.monotonic()))
                )
            except queue.Empty:
                continue
            tokens = line.split()
            if tokens[:3] == ["ACK", "SYNC", str(seq)]:
                if len(tokens) != 7 or tokens[3] != "RX_US" or tokens[5] != "TX_US":
                    raise RuntimeError(f"Malformed Nano SYNC ACK: {line}")
                try:
                    t2, t3 = int(tokens[4]), int(tokens[6])
                except ValueError as exc:
                    raise RuntimeError(f"Invalid Nano SYNC timestamps: {line}") from exc
                if not (0 <= t2 < 2**32 and 0 <= t3 < 2**32):
                    raise RuntimeError(f"Nano SYNC timestamp out of range: {line}")
                row = {
                    "type": "sync_exchange", "sequence_id": seq,
                    "host_send_monotonic_ns": t1,
                    "host_receive_monotonic_ns": t4,
                    "nano_receive_us": t2, "nano_transmit_us": t3,
                    "nano_ack": line,
                }
                self._write_log(row)
                return row
            if tokens and tokens[0] == "ERR":
                raise RuntimeError(f"Nano rejected SYNC: {line}")
        raise TimeoutError(f"No matching ACK SYNC {seq}")

    def binary_sync_exchange(self, *, timeout=1.0):
        """CRC32-verified sequence matched exchange (PC monotonic t1/t4)."""
        self.next_sequence += 1
        seq = self.next_sequence
        wire = pack_sync_request(seq)
        t1 = time.monotonic_ns()
        self.serial.write(wire)
        self.serial.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.reader_error:
                raise RuntimeError(f"Nano serial reader failed: {self.reader_error}")
            try:
                ack_seq, t2, t3, t4 = self.binary_acks.get(
                    timeout=min(0.1, max(0.001, deadline-time.monotonic()))
                )
            except queue.Empty:
                continue
            if ack_seq != seq:
                # Stale ACK is never used for a new exchange.
                continue
            row = {
                "type": "sync_exchange",
                "transport": "binary_crc32_v1",
                "sequence_id": seq,
                "host_send_monotonic_ns": t1,
                "host_receive_monotonic_ns": t4,
                "nano_receive_us": t2,
                "nano_transmit_us": t3,
                "crc32_verified": True,
            }
            self._write_log(row)
            return row
        raise TimeoutError(f"No CRC32-verified binary SYNC ACK {seq}")

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
    p.add_argument("--expected-frame-us", type=int, default=100000,
                   choices=(100000, 200000),
                   help="Require matching Nano 100ms or 200ms Gray frame firmware")
    p.add_argument("--expected-frame-mode", choices=("10HZ_ORIGINAL", "5HZ_DEFERRED"),
                   default="10HZ_ORIGINAL",
                   help="Verify exact INFO FRAME_MODE before START")
    p.add_argument("--nano-port", default=None,
                   help="Optional explicit Nano serial path; by default detects Nano Every")
    p.add_argument("--scope-device", default="auto")
    p.add_argument("--count", type=int, default=1,
                   help="Finite records: 1 first, then 3 for repeated test")
    p.add_argument("--points", type=int, default=100000)
    p.add_argument("--time-div-us", type=float, default=20000,
                   help="200 ms span by default; >=1 complete 100 ms frame")
    p.add_argument("--trigger-position-pct", type=float, default=0.0,
                   help="Trigger position in percent of waveform; 0 = left edge, "
                        "100 = right edge, 50 = centered (default: 0)")
    p.add_argument("--acquire-timeout", type=float, default=15)
    p.add_argument("--sync-transport", choices=("text", "binary"),
                   default="binary", help="CRC32 binary default; text for legacy comparison")
    p.add_argument("--sync-count", type=int, default=12,
                   help="Initial SYNC exchanges (0 for old firmware)")
    p.add_argument("--sync-per-record", type=int, default=4,
                   help="Additional SYNC exchanges after each recording")
    p.add_argument("--per-record-epochs", action="store_true",
                   help="Collect fresh pre/post SYNC batches for EACH scope record")
    p.add_argument("--epoch-sync-count", type=int, default=30,
                   help="SYNC exchanges before AND after each record in epoch mode")
    p.add_argument("--pause-between-s", type=float, default=0.0,
                   help="Deliberate wait after each transfer, before next arming")
    p.add_argument("--label", default="gray_marker_baseline")
    p.add_argument("--output-root", type=Path, default=Path("data/recordings"))
    p.add_argument("--run", action="store_true")
    return p


def plan(args, p):
    if not 1 <= args.count <= 50:
        p.error("--count must be 1..50")
    expected = {"10HZ_ORIGINAL": 100000, "5HZ_DEFERRED": 200000}
    if args.expected_frame_us != expected[args.expected_frame_mode]:
        p.error("Expected Gray mode and frame duration disagree")
    if args.points < 2 or args.points > 20000000:
        p.error("--points must be 2..20000000")
    # Compare the user-provided timebase directly. Multiplying by 1e-6
    # produces 0.19999999999999998 for the valid 20,000 us/div case.
    if not (math.isfinite(args.time_div_us) and args.time_div_us >= 20000):
        p.error("timebase must produce >=200 ms window (>=20000 us/div)")
    if not math.isfinite(args.acquire_timeout) or not 0 < args.acquire_timeout <= 120:
        p.error("invalid --acquire-timeout")
    if (not math.isfinite(args.trigger_position_pct)
            or not 0 <= args.trigger_position_pct <= 100):
        p.error("--trigger-position-pct must be finite and between 0 and 100")
    if not 0 <= args.sync_count <= 200 or not 0 <= args.sync_per_record <= 50:
        p.error("SYNC counts must be 0..200 initial and 0..50 per record")
    if not 8 <= args.epoch_sync_count <= 200:
        p.error("--epoch-sync-count must be 8..200")
    if not math.isfinite(args.pause_between_s) or not 0 <= args.pause_between_s <= 3600:
        p.error("--pause-between-s must be 0..3600 seconds")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.label):
        p.error("invalid --label")
    # record_scope.configure()/verify() interface (no Matrix32 settings).
    args.channels = [2]
    args.scales = {2: 2.0}
    args.trigger_channel = 2
    args.trigger_edge = "rising"
    args.trigger_level = 1.0
    # Keep the user-selected horizontal position. record_scope.configure()
    # sets delay mode OFF and writes HORizontal:POSition AFTER timebase and
    # record-length writes; verify() checks both values before arming.
    return {
        "schema_version": 1, "experiment": "GRAY_TC_V1",
        "source": (f"Arduino Nano Every D7, "
                   f"{1000000/args.expected_frame_us:g} Hz Gray counter"),
        "nominal_frame_s": args.expected_frame_us / 1e6,
        "expected_frame_mode": args.expected_frame_mode,
        "channel_2": "Nano D7; 2 V/div, expected 0-5 V; trigger rising at +1 V",
        "probe_comp": "disconnected",
        "channels": args.channels, "points": args.points,
        "time_div_us": args.time_div_us, "count": args.count,
        "nominal_window_s": 10 * args.time_div_us * 1e-6,
        "nano_port": args.nano_port, "scope_device": args.scope_device,
        "acquire_timeout_s": args.acquire_timeout,
        "trigger_position_pct": args.trigger_position_pct,
        "horizontal_delay_mode_requested": "OFF",
        "no_stimulation": True,
        "not_continuous": True,
        "sync_transport": args.sync_transport,
        "sync_count": args.sync_count,
        "sync_per_record": args.sync_per_record,
        "per_record_epochs": args.per_record_epochs,
        "epoch_sync_count": args.epoch_sync_count if args.per_record_epochs else None,
        "pause_between_s": args.pause_between_s,
    }


def run(args, requested):
    from communication.serial_ports import resolve_port
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_" + args.label
    root = args.output_root / run_id
    (root / "waveforms").mkdir(parents=True, exist_ok=False)
    metadata = {
        "plan": requested, "run_id": run_id, "run_dir": str(root),
        "status": "started",
        "started_utc": utc(), "records": [],
        "clock_synchronization_verified": False,
        "sample_gap_free": False,
    }
    git = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
    metadata["git_commit"] = git.stdout.strip() if git.returncode == 0 else None
    nano_start_attempted = False
    try:
        port = resolve_port(args.nano_port, "nano")
        metadata["nano_port"] = port
        print(f"[NANO] Serial port {port}", flush=True)
        with NanoLog(port, root / "nano_serial.jsonl") as nano:
            metadata["ping"] = nano.synchronize()
            info = nano.command("INFO")
            metadata["nano_info"] = info
            expected_firmware = (
                "FW GRAY_TC_V1", "PIN 7",
                f"FRAME_US {args.expected_frame_us}",
                f"FRAME_MODE {args.expected_frame_mode}",
            )
            if any(part not in info for part in expected_firmware):
                raise RuntimeError(
                    "Wrong Nano firmware/Gray frame mode; expected "
                    + ", ".join(expected_firmware) + "; received " + info)
            if "RUN 1" in info:
                nano.command("STOP")
            # No automatic retry of START: ambiguous response may mean
            # generator already started. Preserve serial log on failure.
            nano_start_attempted = True
            metadata["start_ack"] = nano.command("START")
            print(f"[NANO] Gray-code timebase running ({metadata['start_ack']})",
                  flush=True)
            metadata["sync_measurements"] = []
            sync_method = (nano.binary_sync_exchange if args.sync_transport == "binary"
                           else nano.sync_exchange)
            if not args.per_record_epochs:
                for _ in range(args.sync_count):
                    metadata["sync_measurements"].append(sync_method())
                print(f"[SYNC] Collected {len(metadata['sync_measurements'])} initial exchanges",
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
                            if args.per_record_epochs:
                                # The Arduino and its Gray counter keep running.
                                # New estimator epoch = disjoint SYNC sample IDs.
                                # Capture pre-arm stamps and post-acquisition stamps
                                # before downloading the waveform (which can be slow).
                                epoch = {
                                    "epoch_id": index, "status": "started",
                                    "clock_domain": "PC monotonic ns",
                                    "pre_sync_sequence_ids": [],
                                    "post_sync_sequence_ids": [],
                                }
                                row["clock_epoch"] = epoch
                                for _ in range(args.epoch_sync_count):
                                    measurement = sync_method()
                                    metadata["sync_measurements"].append(measurement)
                                    epoch["pre_sync_sequence_ids"].append(measurement["sequence_id"])
                                epoch["pre_sync_end_host_monotonic_ns"] = time.monotonic_ns()
                                print(f"[EPOCH {index}] pre-arm SYNC: "
                                      f"{len(epoch['pre_sync_sequence_ids'])}", flush=True)
                            t0 = time.monotonic()
                            row["arm_request_monotonic_ns"] = time.monotonic_ns()
                            row["acquisition"] = acquire(f, args.acquire_timeout)
                            row["acquisition_host_s"] = time.monotonic() - t0
                            row["acquisition_completed_monotonic_ns"] = time.monotonic_ns()
                            # Verify position stayed at the requested location
                            # throughout the acquisition, before any transfer.
                            row["scope_settings_after_acquisition"] = scope_settings(
                                f, args.channels, args.trigger_channel,
                                horizontal_position=True)
                            verify(row["scope_settings_after_acquisition"], args)
                            if args.per_record_epochs:
                                for _ in range(args.epoch_sync_count):
                                    measurement = sync_method()
                                    metadata["sync_measurements"].append(measurement)
                                    epoch["post_sync_sequence_ids"].append(measurement["sequence_id"])
                                epoch["post_sync_end_host_monotonic_ns"] = time.monotonic_ns()
                                epoch["status"] = "collected"
                                print(f"[EPOCH {index}] post-acquisition SYNC: "
                                      f"{len(epoch['post_sync_sequence_ids'])}", flush=True)
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
                            row["decoded_gray"] = decode_csv(
                                marker_csv, frame_s=args.expected_frame_us/1e6)
                            row["status"] = "captured"
                            row["completed_utc"] = utc()
                            if not args.per_record_epochs:
                                for _ in range(args.sync_per_record):
                                    metadata["sync_measurements"].append(sync_method())
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
                        if index < args.count and args.pause_between_s:
                            pause_start = time.monotonic_ns()
                            print(f"[PAUSE] {args.pause_between_s:.1f} s before record "
                                  f"{index+1}; Nano remains running", flush=True)
                            time.sleep(args.pause_between_s)
                            row["pause_after_transfer"] = {
                                "requested_s": args.pause_between_s,
                                "start_monotonic_ns": pause_start,
                                "end_monotonic_ns": time.monotonic_ns(),
                            }
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
    return run(args, requested)


if __name__ == "__main__":
    main()
