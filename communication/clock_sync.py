"""Uncertainty-aware mapping from Nano micros() into PC monotonic time.

Separate *passive* drift diagnostics (frame + serial arrival, unknown latency)
from *active* four-timestamp SYNC estimates (bounded by round trip).
No UTC or exact physical host arrival claims are made.
The existing Feather ClockCompensator remains a distinct per-MCU estimator.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re

MOD32 = 1 << 32
FRAME = re.compile(r"^FRAME (\d+) GRAY ([0-9A-Fa-f]+) EDGE_US (\d+) OVERRUNS (\d+)$")


def _regression(points, *, weighted=False):
    """Return y = a*(x-x_anchor)+y_anchor with centered numerical stability."""
    if len(points) < 3:
        raise ValueError("Need at least 3 observations to fit relative clocks")
    anchor_x, anchor_y = points[0][:2]
    xx = [(x - anchor_x) for x, _, *_ in points]
    yy = [(y - anchor_y) for _, y, *_ in points]
    weights = [w if weighted else 1.0 for _, _, w in points]
    sw = sum(weights)
    mx = sum(w*x for x, w in zip(xx, weights)) / sw
    my = sum(w*y for y, w in zip(yy, weights)) / sw
    den = sum(w*(x-mx)**2 for x, w in zip(xx, weights))
    if den <= 0:
        raise ValueError("No clock-time span to estimate drift")
    slope = sum(w*(x-mx)*(y-my) for x, y, w in zip(xx, yy, weights)) / den
    rel_intercept = my - slope*mx
    residual_ns = [y - (slope*x + rel_intercept) for x,y in zip(xx,yy)]
    rms = math.sqrt(sum(v*v for v in residual_ns) / len(residual_ns))
    return {
        "anchor_nano_us": anchor_x,
        "anchor_host_monotonic_ns": anchor_y,
        "slope_host_ns_per_nano_us": slope,
        "relative_offset_ns": rel_intercept,
        "rms_residual_us": rms/1000,
        "max_abs_residual_us": max(map(abs, residual_ns))/1000,
        "sample_count": len(points),
        "span_s": (max(xx)-min(xx))/1e6,
        "drift_ppm_nano_to_host": (slope/1000-1)*1e6,
    }


def passive_diagnostics(serial_log):
    """Estimate slope from FRAME arrival times; offset is NOT calibrated.

    Serial latency can vary, so slope is exploratory, not a synchronization
    guarantee. Opening the serial port or collecting data is NOT required.
    """
    points = []
    previous_us = None
    wraps = 0
    for line in Path(serial_log).read_text(encoding="utf-8").splitlines():
        obj = json.loads(line)
        match = FRAME.fullmatch(str(obj.get("line", "")))
        if not match:
            continue
        edge = int(match.group(3))
        recv = obj.get("received_monotonic_ns")
        if recv is None:
            continue
        if previous_us is not None and edge < previous_us:
            wraps += 1
        previous_us = edge
        points.append((edge + wraps*MOD32, int(recv), 1.0))
    report = _regression(points)
    report["method"] = "passive_one_way_serial_receive"
    report["drift_quality"] = (
        "unreliable_due_to_serial_latency_variation"
        if report["rms_residual_us"] > 1000
        else "exploratory_only"
    )
    report["offset_calibrated"] = False
    report["host_clock_synchronized"] = False
    report["warning"] = (
        "Relative drift estimate only: serial latency and host scheduling "
        "are mixed into the offset and regression residuals. No causal "
        "one-way delay bound is available."
    )
    return report


def read_active_exchanges(serial_log):
    """Read logged four-timestamp measurements and unwrap Nano micros().

    Supports normal 32-bit wrap when consecutive exchanges are less than
    one micros() rollover apart. Resets/unobserved rollovers fail closed.
    """
    exchanges = []
    last_t2 = None
    last_pc = None
    wraps = 0
    for lineno, line in enumerate(Path(serial_log).open(encoding="utf-8"), 1):
        row = json.loads(line)
        if row.get("type") != "sync_exchange":
            continue
        t1 = int(row["host_send_monotonic_ns"])
        t4 = int(row["host_receive_monotonic_ns"])
        t2 = int(row["nano_receive_us"])
        t3 = int(row["nano_transmit_us"])
        if not (0 <= t2 < MOD32 and 0 <= t3 < MOD32 and t1 < t4):
            raise ValueError(f"Invalid SYNC timestamps at line {lineno}")
        if last_pc is not None and t1 <= last_pc:
            raise ValueError("SYNC host times must strictly increase")
        if last_t2 is not None:
            if t2 < last_t2:
                wraps += 1
            # Consecutive SYNCs must be less than one Nano rollover (~71 min).
            if (t1 - last_pc) >= 3600*1e9:
                raise ValueError("SYNC exchanges too far apart for unique rollover")
        nano_rx = t2 + wraps*MOD32
        nano_tx = nano_rx + ((t3-t2) % MOD32)
        if nano_tx - nano_rx > 1e6:
            raise ValueError("Nano SYNC response took more than one second")
        exchanges.append({
            "seq": int(row["sequence_id"]),
            "t1_host_ns": t1,
            "t4_host_ns": t4,
            "t2_nano_us": nano_rx,
            "t3_nano_us": nano_tx,
        })
        last_t2, last_pc = t2, t1
    return exchanges


def _percentile(sorted_values, percent):
    if not sorted_values:
        raise ValueError("No latency observations")
    index = (len(sorted_values)-1)*percent/100
    lo, hi = int(math.floor(index)), int(math.ceil(index))
    return sorted_values[lo] + (sorted_values[hi]-sorted_values[lo])*(index-lo)


def _latency_summary(exchanges, slope):
    """RTT minus measured Nano processing; not a one-way latency estimate."""
    samples = []
    for entry in exchanges:
        rtt = (entry["t4_host_ns"]-entry["t1_host_ns"])/1000
        processing = (entry["t3_nano_us"]-entry["t2_nano_us"])*slope/1000
        samples.append((rtt, processing, rtt-processing))
    def quantiles(index, percentiles):
        sorted_values = sorted(row[index] for row in samples)
        return {name: _percentile(sorted_values, pct) for name,pct in percentiles}
    full = [("min",0), ("p10",10), ("median",50), ("p90",90),
            ("p99",99), ("max",100)]
    return {
        "sample_count": len(samples),
        "rtt_us": quantiles(0, full),
        "nano_processing_us": quantiles(1, [("median",50), ("p90",90), ("max",100)]),
        "transport_rtt_us": quantiles(2, full),
        "note": "One-way delays and USB/OS asymmetry are not directly measured.",
    }


def active_clock_fit(exchanges, *, min_samples=8, min_span_s=5.0):
    """Fit Nano->host monotonic using four-timestamp exchanges.

    Per-exchange midpoint estimates assume roughly symmetric propagation.
    The half-roundtrip (after Nano handling time) is a conservative
    causal bracket for offset *at the exchange*, conditional on scale.
    It does not include oscillator-fit extrapolation error.
    """
    if len(exchanges) < min_samples:
        raise ValueError(f"Need at least {min_samples} SYNC exchanges")
    pairs = []
    for row in exchanges:
        t1, t4 = row["t1_host_ns"], row["t4_host_ns"]
        t2, t3 = row["t2_nano_us"], row["t3_nano_us"]
        host_elapsed = t4-t1
        nano_elapsed = (t3-t2)*1000
        flight = host_elapsed - nano_elapsed
        if flight < -100000:
            raise ValueError("Negative roundtrip transport interval; clocks/protocol invalid")
        # Small negative values can occur if Nano clock rate differs slightly.
        flight = max(0, flight)
        mid_nano = (t2+t3)/2
        mid_host = (t1+t4)/2
        # Weight low-delay observations more heavily while retaining
        # observations throughout the run to constrain the slope.
        weight = 1.0 / max(flight, 100000.0)**2
        pairs.append((mid_nano, mid_host, weight))
    span = (max(x for x,_,_ in pairs)-min(x for x,_,_ in pairs))/1e6
    if span < min_span_s:
        raise ValueError(
            f"SYNC span {span:.2f}s insufficient for clock drift; "
            f"need >= {min_span_s:.1f}s"
        )
    report = _regression(pairs, weighted=True)
    slope = report["slope_host_ns_per_nano_us"]
    brackets = []
    for row in exchanges:
        # Causal bound on offset b for host = slope*nano+b, without
        # assuming equal upstream/downstream transport times.
        left = row["t1_host_ns"] - slope*row["t2_nano_us"]
        right = row["t4_host_ns"] - slope*row["t3_nano_us"]
        if left > right + 100000:
            raise ValueError("SYNC causal interval reversed")
        brackets.append(max(0, right-left)/2000)  # microseconds halfwidth
    report.update({
        "method": "four_timestamp_sync_weighted_affine",
        "offset_calibrated": True,
        "host_clock_synchronized": False,
        "host_clock_mapping_estimated": True,
        "assumption": (
            "Midpoint regression assumes limited transport asymmetry. "
            "Per-exchange halfwidth is a causal interval, conditional on "
            "fitted slope, NOT a guaranteed end-to-end error bound."
        ),
        "best_exchange_offset_halfwidth_us": min(brackets),
        "median_exchange_offset_halfwidth_us": sorted(brackets)[len(brackets)//2],
        "worst_exchange_offset_halfwidth_us": max(brackets),
        "timestamp_domain": "PC monotonic ns, same host boot; not UTC",
        "latency": _latency_summary(exchanges, slope),
    })
    return report


def nano_us_to_host_ns(nano_us, fit):
    """Estimate PC monotonic nanoseconds; meaningful only for active fit."""
    if not fit.get("host_clock_mapping_estimated"):
        raise ValueError("Passive observations do not calibrate Nano->host offset")
    return (fit["anchor_host_monotonic_ns"]
            + fit["relative_offset_ns"]
            + fit["slope_host_ns_per_nano_us"]
            * (float(nano_us)-fit["anchor_nano_us"]))
