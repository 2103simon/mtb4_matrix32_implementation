"""Join independently captured scope records to the Nano's Gray frame clock.

Use metadata.json + nano_serial.jsonl created by test_gray_timecode.py.
The resulting affine mapping is to Nano micros() time, NOT calibrated UTC.
USB serial receive timestamps are diagnostics, never physical marker times.
No waveform samples are modified or synthesized across acquisition gaps.
"""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import statistics

FRAME = re.compile(
    r"^FRAME (\d+) GRAY ([0-9A-Fa-f]+) EDGE_US (\d+) OVERRUNS (\d+)$"
)
MOD16 = 1 << 16
MOD32_US = 1 << 32


def read_nano_frames(path):
    """Strict serial-log parser; unwrap 32-bit micros for long sessions."""
    frames = {}
    previous_count = None
    previous_us = None
    previous_unwrapped_us = None
    wrap_count = 0
    with Path(path).open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            record = json.loads(line)
            matched = FRAME.fullmatch(str(record.get("line", "")))
            if not matched:
                continue
            count, gray, edge_us, overruns = (
                int(matched.group(1)),
                int(matched.group(2), 16),
                int(matched.group(3)),
                int(matched.group(4)),
            )
            if not (0 <= count <= 0xFFFFFFFF and 0 <= edge_us < MOD32_US):
                raise ValueError(f"Invalid Nano frame/counter at line {line_number}")
            binary = count & 0xFFFF
            expected_gray = binary ^ (binary >> 1)
            if gray != expected_gray:
                raise ValueError(f"Nano Gray/code mismatch at line {line_number}")
            if count in frames or (previous_count is not None and count <= previous_count):
                raise ValueError("Nano frame counters repeat or are unordered; possible reset")
            if previous_us is not None and edge_us < previous_us:
                wrap_count += 1
            unwrapped_us = edge_us + wrap_count * MOD32_US
            if previous_unwrapped_us is not None and unwrapped_us <= previous_unwrapped_us:
                raise ValueError("Nano clock not increasing after unwrapping")
            frames[count] = {
                "counter": count,
                "gray_hex": f"{gray:04X}",
                "nano_edge_us": edge_us,
                "nano_unwrapped_us": unwrapped_us,
                "overruns": overruns,
                "host_received_monotonic_ns": record.get("received_monotonic_ns"),
            }
            previous_count = count
            previous_us = edge_us
            previous_unwrapped_us = unwrapped_us
    if not frames:
        raise ValueError("No valid FRAME messages in Nano serial log")
    return frames


def fit_affine(pairs):
    """Least-squares map x=scope seconds to y=Nano seconds, centered."""
    if len(pairs) < 3:
        raise ValueError("At least three matched physical frame edges are required")
    xx = [p[0] for p in pairs]
    yy = [p[1] for p in pairs]
    mean_x = statistics.mean(xx)
    mean_y = statistics.mean(yy)
    spread = sum((x - mean_x) ** 2 for x in xx)
    if spread <= 0:
        raise ValueError("Scope edge times have no spread")
    slope = sum((x - mean_x) * (y - mean_y)
                for x, y in pairs) / spread
    offset = mean_y - slope * mean_x
    errors = [(slope * x + offset - y) * 1e6 for x, y in pairs]
    rms_us = math.sqrt(sum(v*v for v in errors) / len(errors))
    return {"slope_nano_s_per_scope_s": slope,
            "offset_nano_s": offset,
            "edge_fit_rms_us": rms_us,
            "edge_fit_max_abs_us": max(map(abs, errors)),
            "matched_frames": len(pairs)}


def build_alignment(metadata, nano_frames, *, max_rms_us=100, strict=True):
    """Match all waveform records; optionally retain over-threshold fits.

    strict=True preserves legacy fail-closed behavior. strict=False only
    relaxes the RMS gate for diagnostic reporting, not counter validation.
    """
    if metadata.get("plan", {}).get("experiment") != "GRAY_TC_V1":
        raise ValueError("Expected GRAY_TC_V1 metadata")
    records = metadata.get("records", [])
    if not records:
        raise ValueError("No scope records")
    result = {
        "schema_version": 1,
        "experiment": "GRAY_TC_V1",
        "time_axis": "Nano micros() since board boot, unwrapped (not UTC)",
        "physical_sample_gaps_recovered": False,
        "host_clock_synchronized": False,
        "records": [],
        "warnings": [],
    }
    prev_last_count = None
    prev_last_edge = None
    for index, record in enumerate(records, 1):
        if record.get("status") != "captured":
            raise ValueError(f"Record {index} not captured")
        decoded = record["decoded_gray"]["frames"]
        if len(decoded) < 3:
            raise ValueError(f"Record {index} has fewer than three complete frames")
        # Low-16 bit Gray code is ambiguous without the serial frame log.
        matches = []
        previous_count = None
        for frame in decoded:
            short = int(frame["counter_mod_65536"])
            candidates = [entry for count, entry in nano_frames.items()
                          if count % MOD16 == short]
            if len(candidates) != 1:
                raise ValueError(
                    f"Record {index}: counter {short} has "
                    f"{len(candidates)} serial matches; full counter ambiguous"
                )
            ref = candidates[0]
            if int(frame["gray_hex"], 16) != int(ref["gray_hex"], 16):
                raise ValueError(f"Record {index}: Gray code disagrees with serial log")
            if previous_count is not None and ref["counter"] <= previous_count:
                raise ValueError(f"Record {index}: counter order is invalid")
            if ref["overruns"]:
                result["warnings"].append(
                    f"Nano reported {ref['overruns']} timing overruns at frame {ref['counter']}"
                )
            matches.append((float(frame["scope_start_s"]),
                            ref["nano_unwrapped_us"] / 1e6))
            previous_count = ref["counter"]
        fit = fit_affine(matches)
        fit_passes = fit["edge_fit_rms_us"] <= max_rms_us
        if not fit_passes:
            warning = (
                f"Record {index}: edge-fit RMS {fit['edge_fit_rms_us']:.1f} us "
                f"exceeds threshold {max_rms_us:.1f} us"
            )
            if strict:
                raise ValueError(warning)
            result["warnings"].append(warning)
        first_count = nano_frames[
            next(k for k in nano_frames if k % MOD16 == decoded[0]["counter_mod_65536"])
        ]["counter"]
        last_count = previous_count
        delta = None
        if prev_last_count is not None:
            if first_count <= prev_last_count:
                raise ValueError("Scope acquisitions overlap or counter reset")
            delta = {
                "missing_complete_frame_ids_between": first_count - prev_last_count - 1,
                "physical_marker_start_separation_s": (
                    matches[0][1] - prev_last_edge
                ),
                "note": (
                    "Marker-start separation only; this is not the exact "
                    "unrecorded sample duration because the records also "
                    "contain partial frames at their edges."
                )
            }
        prev_last_count = last_count
        prev_last_edge = matches[-1][1]
        preamble = record["channels"]["CH2"]["transfer"]["waveform_preamble"]
        count_samples = int(record["channels"]["CH2"]["waveform"]["samples"])
        xzero = float(preamble["xzero_s"])
        xincr = float(preamble["xincr_s"])
        ptoff = float(preamble["ptoff"])
        first_scope_s = xzero + (0 - ptoff) * xincr
        last_scope_s = xzero + ((count_samples - 1) - ptoff) * xincr
        slope = fit["slope_nano_s_per_scope_s"]
        offset = fit["offset_nano_s"]
        entry = {
            "record_index": index,
            "scope_waveform": record["channels"]["CH2"]["path"],
            "first_full_frame_counter": first_count,
            "last_full_frame_counter": last_count,
            "first_nano_edge_s": matches[0][1],
            "last_nano_edge_s": matches[-1][1],
            "nano_time_at_first_sample_s": slope * first_scope_s + offset,
            "nano_time_at_last_sample_s": slope * last_scope_s + offset,
            "scope_sample_interval_s": xincr,
            "nano_seconds_per_scope_second": slope,
            "clock_fit": fit,
            "edge_fit_rms_threshold_us": max_rms_us,
            "edge_fit_rms_below_threshold": fit_passes,
            "gap_from_previous": delta,
        }
        result["records"].append(entry)
    result["decoded_frames_total"] = sum(
        record["clock_fit"]["matched_frames"] for record in result["records"]
    )
    result["source_record_count"] = len(records)
    result["nano_serial_frame_count"] = len(nano_frames)
    result["edge_fit_rms_threshold_us"] = max_rms_us
    result["all_fit_rms_below_threshold"] = all(
        record["edge_fit_rms_below_threshold"] for record in result["records"]
    )
    result["interpretation"] = (
        "A waveform sample at scope t_s maps to estimated Nano time "
        "a*t_s+b using that record's clock_fit coefficients. Preserve fit "
        "residuals. Nano-to-PC and Nano-to-UTC synchronization are NOT "
        "established by this file. Gray frame matching does not restore "
        "physically missing waveform data."
    )
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_dir", type=Path, help="Directory containing metadata.json and nano_serial.jsonl")
    p.add_argument("--max-fit-rms-us", type=float, default=100.0)
    p.add_argument("--output", type=Path,
                   help="JSON output (default: RUN_DIR/gray_alignment.json)")
    args = p.parse_args(argv)
    if not math.isfinite(args.max_fit_rms_us) or args.max_fit_rms_us <= 0:
        p.error("--max-fit-rms-us must be positive")
    meta = json.loads((args.run_dir / "metadata.json").read_text())
    nano = read_nano_frames(args.run_dir / "nano_serial.jsonl")
    result = build_alignment(meta, nano, max_rms_us=args.max_fit_rms_us)
    out = args.output or args.run_dir / "gray_alignment.json"
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Matched {result['decoded_frames_total']} Gray frames in "
          f"{result['source_record_count']} scope records. Saved {out}")
    for record in result["records"]:
        print(f"Record {record['record_index']}: "
              f"{record['first_full_frame_counter']}..{record['last_full_frame_counter']}, "
              f"clock ratio {record['nano_seconds_per_scope_second']:.9f}, "
              f"RMS residual {record['clock_fit']['edge_fit_rms_us']:.1f} us")


if __name__ == "__main__":
    main()
