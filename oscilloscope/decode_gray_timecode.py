"""Decode Arduino Nano Gray timecode from Tektronix CH2 CSV.

Reads time_s,voltage_v; recognizes complete framed messages only.
No attempt to reconstruct physically unrecorded gaps or to infer host sync.
"""
import argparse
import csv
import json
import math
from pathlib import Path

START_S = 0.004
DATA_S = 0.007
SLOT_S = 0.005
ONE_S = 0.001
END_S = 0.090
END_WIDTH_S = 0.007
FRAME_S = 0.100
BITS = 16


def gray_to_binary(gray):
    value = gray
    while gray:
        gray >>= 1
        value ^= gray
    return value


def binary_to_gray(value):
    return (value ^ (value >> 1)) & 0xFFFF


def edges_from_csv(path, threshold=2.0):
    """Return rising edges and high pulse widths, retaining incomplete pulses."""
    rises = []
    pulses = []
    first_t = None
    last_t = None
    previous = None
    rise = None
    with Path(path).open(newline="") as f:
        reader = csv.DictReader(f)
        if not {"time_s", "voltage_v"}.issubset(reader.fieldnames or []):
            raise ValueError("CSV needs time_s and voltage_v")
        for row in reader:
            t = float(row["time_s"])
            v = float(row["voltage_v"])
            if not math.isfinite(t) or not math.isfinite(v):
                raise ValueError("Non-finite CSV sample")
            if previous is not None and t <= previous[0]:
                raise ValueError("Waveform time axis must increase")
            if first_t is None:
                first_t = t
            if previous is not None:
                old_t, old_v = previous
                # Linear interpolation gives a fractional-sample edge estimate.
                if (old_v < threshold <= v) or (old_v >= threshold > v):
                    cross = old_t + (t - old_t) * (threshold - old_v) / (v - old_v)
                    if old_v < threshold <= v:
                        rise = cross
                        rises.append(cross)
                    elif rise is not None:
                        pulses.append((rise, cross - rise))
                        rise = None
            previous = (t, v)
            last_t = t
    if first_t is None:
        raise ValueError("Empty waveform CSV")
    return pulses, {"first_time_s": first_t, "last_time_s": last_t,
                    "rising_edges": len(rises), "complete_high_pulses": len(pulses)}


def decode_pulses(pulses, *, tolerance_s=0.00045):
    """Decode only complete frames with correct delimiters and slot pulses.

    The thresholded edge detector is intentionally conservative. Invalid and
    partial frames are not silently converted into counter values.
    """
    if not 0 < tolerance_s < ONE_S / 2:
        raise ValueError("tolerance_s must be positive and <0.5 ms")
    decoded = []
    for idx, (start_t, duration) in enumerate(pulses):
        if abs(duration - START_S) > tolerance_s:
            continue
        end_candidates = [
            (j, t, width) for j, (t, width) in enumerate(pulses[idx+1:], idx+1)
            if abs(t - (start_t + END_S)) <= tolerance_s
            and abs(width - END_WIDTH_S) <= tolerance_s
        ]
        if len(end_candidates) != 1:
            continue
        end_idx, end_t, _ = end_candidates[0]
        slots = [0] * BITS
        used = set()
        valid = True
        for j in range(idx + 1, end_idx):
            t, width = pulses[j]
            nearest = round((t - (start_t + DATA_S)) / SLOT_S)
            if not 0 <= nearest < BITS:
                valid = False
                break
            expected = start_t + DATA_S + nearest * SLOT_S
            if (abs(t - expected) > tolerance_s or
                    abs(width - ONE_S) > tolerance_s or nearest in used):
                valid = False
                break
            slots[nearest] = 1
            used.add(nearest)
        if not valid:
            continue
        gray = 0
        for bit in slots:
            gray = (gray << 1) | bit
        counter = gray_to_binary(gray)
        decoded.append({
            "scope_start_s": round(start_t, 9),
            "scope_end_s": round(end_t, 9),
            "gray_hex": f"{gray:04X}",
            "counter_mod_65536": counter,
            "bits_msb_first": "".join(map(str, slots)),
            "data_one_pulses": len(used),
        })
    for prev, current in zip(decoded, decoded[1:]):
        difference = (current["counter_mod_65536"] -
                      prev["counter_mod_65536"]) % 65536
        current["increment_from_previous"] = difference
        current["scope_start_delta_s"] = round(
            current["scope_start_s"] - prev["scope_start_s"], 9)
        current["expected_start_delta_s"] = round(difference * FRAME_S, 9)
        current["spacing_residual_s"] = round(
            current["scope_start_delta_s"] -
            current["expected_start_delta_s"], 9)
    return decoded


def audit_physical_pulses(pulses, frames, *, tolerance_s=0.00045):
    """Account for EVERY complete high pulse on the physical CH2 waveform.

    START and END plus 1-bit slots are distinguished by timing and width.
    Unassigned pulses are retained explicitly for edge-review, never silently
    ignored; a successful frame decode alone is not a completeness proof.
    """
    assigned = set()
    per_frame = []
    for frame in frames:
        t0 = frame["scope_start_s"]
        frame_parts = []
        expectations = [("START", t0, START_S)]
        expectations.extend(
            (f"BIT_{bit_idx:02d}",
             t0 + DATA_S + bit_idx * SLOT_S, ONE_S)
            for bit_idx, value in enumerate(frame["bits_msb_first"])
            if value == "1"
        )
        expectations.append(("END", t0 + END_S, END_WIDTH_S))
        for name, t_expected, width_expected in expectations:
            matches = [
                i for i, (t, width) in enumerate(pulses)
                if i not in assigned
                and abs(t - t_expected) <= tolerance_s
                and abs(width - width_expected) <= tolerance_s
            ]
            if len(matches) != 1:
                frame_parts.append({
                    "role": name, "matched": False,
                    "matching_pulse_count": len(matches),
                })
                continue
            i = matches[0]
            assigned.add(i)
            frame_parts.append({
                "role": name, "matched": True, "pulse_index": i,
                "start_s": pulses[i][0], "width_s": pulses[i][1],
                "rise_residual_us": (pulses[i][0] - t_expected) * 1e6,
                "width_residual_us": (pulses[i][1] - width_expected) * 1e6,
            })
        per_frame.append({
            "counter_mod_65536": frame["counter_mod_65536"],
            "scope_start_s": t0,
            "expected_pulses": len(expectations),
            "matched_pulses": sum(part["matched"] for part in frame_parts),
            "all_start_data_end_pulses_accounted_for": all(
                part["matched"] for part in frame_parts
            ),
            "pulses": frame_parts,
        })
    unassigned = [
        {"pulse_index": i, "start_s": float(t), "width_s": float(width)}
        for i, (t, width) in enumerate(pulses)
        if i not in assigned
    ]
    return {
        "complete_high_pulse_count": len(pulses),
        "decoded_frame_count": len(frames),
        "assigned_pulse_count": len(assigned),
        "unassigned_pulse_count": len(unassigned),
        "unassigned_pulses": unassigned,
        "frames": per_frame,
        "all_complete_high_pulses_accounted_for": (
            bool(frames) and not unassigned
            and all(x["all_start_data_end_pulses_accounted_for"] for x in per_frame)
        ),
        "audit_scope": (
            "All complete thresholded high pulses (START, each 1 bit, END) "
            "are accounted for; does not prove missing weak edges or "
            "truncated start/end boundary pulses were observed."
        ),
    }


def decode_csv(path, threshold=2.0, tolerance_s=0.00045):
    pulses, stats = edges_from_csv(path, threshold=threshold)
    frames = decode_pulses(pulses, tolerance_s=tolerance_s)
    return {
        "source": str(path),
        "threshold_v": threshold,
        "pulse_tolerance_s": tolerance_s,
        "waveform": stats,
        "frames": frames,
        "decoded_frame_count": len(frames),
        "edge_audit": audit_physical_pulses(pulses, frames, tolerance_s=tolerance_s),
        "time_alignment_verified": False,
        "interpretation": (
            "Each Gray counter identifies its position modulo 65536 in the "
            "Nano timecode sequence. Absolute experiment time requires matching "
            "the full counter to Nano FRAME serial logs. Serial arrival is not "
            "the exact physical marker-edge time."
        ),
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("csv", nargs="+", type=Path,
                   help="One or more CH2 CSV waveforms from separate acquisitions")
    p.add_argument("--threshold", type=float, default=2.0)
    p.add_argument("--tolerance-us", type=float, default=450)
    p.add_argument("--output", type=Path, help="Write JSON report")
    args = p.parse_args(argv)
    if not math.isfinite(args.threshold) or not 0 < args.tolerance_us < 500:
        p.error("Invalid threshold or tolerance")
    report = {
        "schema_version": 1, "decoder": "GRAY_TC_V1",
        "records": [decode_csv(file, args.threshold, args.tolerance_us * 1e-6)
                    for file in args.csv],
    }
    payload = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
    print(payload)


if __name__ == "__main__":
    main()
