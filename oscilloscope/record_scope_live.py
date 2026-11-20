"""Repeated *finite* Tektronix records; diagnostic for PC-side acquisition gaps.

Uses the verified MDO4104B-6 USBTMC routines. Not continuous ADC streaming.
Scope-only: never connects to, starts, or controls the Matrix32.
Dry-run by default; USBTMC blocking calls need an external OS watchdog.
"""
import argparse
import hashlib
import json
import math
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from record_scope import (
    acquire, atomic_json, configure, scope_settings, validate_csv, verify,
)
from scope_device import resolve_scope_device, verify_scope_identity
from tek_mdo_usb import capture, query


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--device", default="auto",
                   help="USBTMC node; auto identifies an unambiguous Tektronix")
    p.add_argument("--channel", type=int, default=2,
                   help="One waveform channel; also the trigger source (default CH2)")
    p.add_argument("--scale", type=float, default=1.0,
                   help="Vertical volts/div (default 1.0)")
    p.add_argument("--time-div-us", type=float, default=2000,
                   help="Horizontal microseconds/div (default 2000)")
    p.add_argument("--points", type=int, default=10000,
                   help="Record length in samples (default 10000)")
    p.add_argument("--trigger-edge", choices=("rising", "falling"),
                   default="rising")
    p.add_argument("--trigger-level", type=float, default=1.0,
                   help="Trigger voltage (default +1 V)")
    p.add_argument("--trigger-position-pct", type=float, default=50.0,
                   help="Pre-trigger percentage 0..100 (default 50)")
    p.add_argument("--count", type=int, default=5,
                   help="Number of fresh acquisition cycles (default 5)")
    p.add_argument("--acquire-timeout", type=float, default=15,
                   help="Per-record acquisition polling timeout in s (default 15)")
    p.add_argument("--label", default="probe_comp_ch2")
    p.add_argument("--output-root", type=Path, default=Path("data/recordings"))
    p.add_argument("--run", action="store_true",
                   help="Actually configure scope and acquire (default dry-run)")
    return p


def validate_args(args, parser):
    numeric = (args.scale, args.time_div_us, args.trigger_level,
               args.trigger_position_pct, args.acquire_timeout)
    if not all(math.isfinite(value) for value in numeric):
        parser.error("Acquisition settings must be finite")
    if not 1 <= args.channel <= 4:
        parser.error("--channel must be 1..4")
    if args.scale <= 0 or args.time_div_us <= 0:
        parser.error("Vertical/horizontal scales must be positive")
    if args.points < 2 or args.points > 20_000_000:
        parser.error("--points must be 2..20000000")
    if not 0 <= args.trigger_position_pct <= 100:
        parser.error("--trigger-position-pct must be 0..100")
    if args.count < 1 or args.count > 10000:
        parser.error("--count must be 1..10000")
    if not 0 < args.acquire_timeout <= 120:
        parser.error("--acquire-timeout must be >0 and <=120 seconds")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.label):
        parser.error("--label must contain only letters, digits, '_' or '-'")
    # Explicit shape used by the already validated record_scope functions.
    args.channels = [args.channel]
    args.trigger_channel = args.channel
    args.scales = {args.channel: args.scale}
    args.expected_frequency_hz = None
    args.expected_width_us = None
    args.signal_progress = False
    args.signal_armed = False
    args.await_stop_marker = False
    return {
        "schema_version": 1,
        "mode": "repeated_separate_sample_sequences",
        "channel": args.channel,
        "v_div": args.scale,
        "trigger_channel": args.channel,
        "trigger_edge": args.trigger_edge,
        "trigger_level_v": args.trigger_level,
        "trigger_position_pct": args.trigger_position_pct,
        "time_div_us": args.time_div_us,
        "points": args.points,
        "count": args.count,
        "acquire_timeout_s": args.acquire_timeout,
        "nominal_waveform_span_s": 10 * args.time_div_us * 1e-6,
        "nominal_sample_interval_s": 10 * args.time_div_us * 1e-6 / args.points,
        "scope_sample_clock_synchronized_to_host": False,
        "no_gap_free_continuity_claim": True,
    }


def acquire_one(f, args, *, index, root, prev=None):
    """Fresh arm -> verified stopped sequence -> transfer; timings are host brackets."""
    record = {
        "index": index,
        "status": "started",
        "started_utc": utc_now(),
        "cycle_begin_monotonic_ns": time.monotonic_ns(),
    }
    try:
        # Freshness is procedural: acquire() STOPs then RUNs, confirms RUN,
        # and waits for STOP after the trigger. It does not prove sample uniqueness.
        record["acquisition"] = acquire(f, args.acquire_timeout)
        record["acquisition_observed_complete_monotonic_ns"] = time.monotonic_ns()
        if not record["acquisition"]["seen_running"]:
            raise RuntimeError("Scope did not observe fresh acquisition RUN")
        if record["acquisition"]["first_stopped_observation"] is None:
            raise RuntimeError("Scope did not observe sequence completion")
        file = root / "waveforms" / f"record_{index:05d}_ch{args.channel}.csv"
        record["transfer_begin_monotonic_ns"] = time.monotonic_ns()
        record["transfer"] = capture(f, args.channel, args.points, file)
        record["transfer_end_monotonic_ns"] = time.monotonic_ns()
        record["waveform"] = str(file.relative_to(root))
        record["waveform_timing"] = validate_csv(file, args.points)
        record["waveform_sha256"] = hashlib.sha256(file.read_bytes()).hexdigest()
        record["transfer_duration_s"] = round(
            (record["transfer_end_monotonic_ns"] -
             record["transfer_begin_monotonic_ns"]) / 1e9, 6)
        record["scope_window_s"] = record["waveform_timing"]["span_s"]
        record["status"] = "captured"
        if prev is not None:
            # This interval includes transfer/rearm processing, NOT a measured
            # physical gap between last scope sample and next first sample.
            record["previous_cycle_start_to_start_s"] = round(
                (record["cycle_begin_monotonic_ns"] -
                 prev["cycle_begin_monotonic_ns"]) / 1e9, 6)
            record["previous_transfer_end_to_cycle_begin_s"] = round(
                (record["cycle_begin_monotonic_ns"] -
                 prev["transfer_end_monotonic_ns"]) / 1e9, 6)
            record["identical_csv_to_previous"] = (
                record["waveform_sha256"] == prev["waveform_sha256"])
    except BaseException as exc:
        record["status"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        record["cycle_end_monotonic_ns"] = time.monotonic_ns()
        record["finished_utc"] = utc_now()
        record["cycle_duration_s"] = round(
            (record["cycle_end_monotonic_ns"] -
             record["cycle_begin_monotonic_ns"]) / 1e9, 6)
        atomic_json(root / f"record_{index:05d}.json", record)
    return record


def run(args, plan):
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_" + args.label
    root = args.output_root / run_id
    (root / "waveforms").mkdir(parents=True, exist_ok=False)
    summary = {
        "plan": plan, "run_id": run_id, "status": "started",
        "started_utc": utc_now(), "records": [],
        "scope_sample_clock_synchronized_to_host": False,
        "gap_free_acquisition_verified": False,
        "description": (
            "Independent finite triggered records with distinct scope-relative "
            "time axes. No guaranteed continuity or scope-to-PC time alignment."
        ),
    }
    git = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
    summary["git_commit"] = git.stdout.strip() if git.returncode == 0 else None
    previous = None
    try:
        node = resolve_scope_device(args.device)
        summary["scope_node"] = str(node)
        print(f"[SCOPE] USBTMC node {node}", flush=True)
        with open(node, "r+b", buffering=0) as f:
            summary["identity"] = verify_scope_identity(query(f, "*IDN?"))
            print(f"[SCOPE] {summary['identity']}", flush=True)
            configure(f, args)
            summary["verified_settings"] = scope_settings(
                f, args.channels, args.trigger_channel, horizontal_position=True)
            verify(summary["verified_settings"], args)
            for index in range(1, args.count + 1):
                print(f"[SCOPE] Acquiring {index}/{args.count} ...", flush=True)
                # acquire_one commits a per-record JSON before returning/raising.
                try:
                    entry = acquire_one(f, args, index=index, root=root, prev=previous)
                except BaseException:
                    summary["records"].append(
                        {"index": index, "status": "failed",
                         "metadata": f"record_{index:05d}.json"})
                    raise
                summary["records"].append({
                    "index": index,
                    "status": entry["status"],
                    "metadata": f"record_{index:05d}.json",
                    "waveform": entry["waveform"],
                    "cycle_duration_s": entry["cycle_duration_s"],
                    "transfer_duration_s": entry["transfer_duration_s"],
                    "scope_window_s": entry["scope_window_s"],
                    "identical_csv_to_previous": entry.get("identical_csv_to_previous"),
                })
                previous = entry
                print(
                    f"[SCOPE] Record {index}: {entry['waveform_timing']['samples']} points, "
                    f"{entry['scope_window_s'] * 1000:.3f} ms window, "
                    f"{entry['cycle_duration_s']:.3f} s host cycle, "
                    f"{entry['transfer_duration_s']:.3f} s download/CSV",
                    flush=True)
                atomic_json(root / "summary.json", summary)
            summary["status"] = "complete"
    except BaseException as exc:
        summary["status"] = "failed"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        summary["finished_utc"] = utc_now()
        summary["captured_count"] = sum(
            item["status"] == "captured" for item in summary["records"])
        atomic_json(root / "summary.json", summary)
        print(f"[SCOPE] Saved {root} ({summary['status']})", flush=True)
    return summary


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    plan = validate_args(args, parser)
    print(json.dumps(plan, indent=2), flush=True)
    if not args.run:
        print("DRY RUN: no hardware accessed.")
        return
    run(args, plan)


if __name__ == "__main__":
    main()
