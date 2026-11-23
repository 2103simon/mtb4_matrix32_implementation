"""Established S02-v5 negative-voltage onset detector and signal gate.

No dependency on any experiment report script. Do not alter its scientific
rules without source-locked regression and operator review.
"""
from collections import deque
import math
import statistics
from analysis_primitives.baseline import median_mad_scale

NEGATIVE_EVENT_DETECTOR_VERSION = "S02-negative-event-v5-noise-tolerant-quiet"


def estimate_threshold(volts, *, require_signal=True):
    """Estimate the established S02 robust baseline/noise threshold.

    require_signal=True preserves the qualified detector gate. Setting it False
    exposes the exact same baseline/noise/threshold components for diagnostics
    and plotting only; callers must not promote a weak trace to accepted events.
    """
    stride = max(1, len(volts) // 20000)
    sample = volts[::stride]
    baseline, noise_sigma = median_mad_scale(sample)
    excursion = max(0.0, baseline - min(volts))
    cutoff = max(6 * noise_sigma, 0.18 * excursion, 1.0)
    signal_gate_v = max(8 * noise_sigma, 3.0)
    signal_gate_passed = excursion >= signal_gate_v
    result = {
        "baseline_v": baseline,
        "noise_sigma_v": noise_sigma,
        "negative_excursion_v": excursion,
        "threshold_v": baseline - cutoff,
        "threshold_drop_v": cutoff,
        "signal_gate_v": signal_gate_v,
        "signal_gate_passed": signal_gate_passed,
    }
    if require_signal and not signal_gate_passed:
        raise ValueError(
            "No sufficiently strong negative CH1 excursion for this detector; "
            "do not report frequency without manual review"
        )
    return result


def detect_events(times, volts, threshold, commanded_period_s, *,
                  baseline_v=None, noise_sigma_v=0.0, quiet_before_us=200.0,
                  quiet_band_v=None, min_quiet_fraction=0.75,
                  phase_deadtime_us=150.0, min_event_separation_s=None):
    """Negative onsets with noise-tolerant prequiet and audited phase recrossings.

    Uses a percentage of quiet samples in the PRECEDING fixed window instead
    of uninterrupted quiet, plus a nearby-baseline and biphasic morphology
    guard for onsets that fail the original uninterrupted-quiet rule.
    Timings are observed; no pulse is placed at a commanded frequency slot.
    min_event_separation_s is optional so a dynamic-frequency analysis can use
    a separation derived from the shortest permitted period in the record
    instead of incorrectly assuming one constant commanded period.
    """
    if len(times) != len(volts) or len(times) < 3:
        raise ValueError("Invalid waveform for event detection")
    dt = statistics.median(times[i + 1] - times[i]
                           for i in range(len(times) - 1))
    if commanded_period_s <= 0 or dt <= 0:
        raise ValueError("Invalid period or sample interval")
    if not (math.isfinite(threshold) and math.isfinite(noise_sigma_v) and
            noise_sigma_v >= 0 and math.isfinite(quiet_before_us) and
            quiet_before_us > 0):
        raise ValueError("Invalid negative threshold, noise or quiet interval")
    if not (math.isfinite(min_quiet_fraction) and
            0.5 <= min_quiet_fraction <= 1):
        raise ValueError("min_quiet_fraction must be between 0.5 and 1")
    if (not math.isfinite(phase_deadtime_us) or phase_deadtime_us < 0 or
            phase_deadtime_us * 1e-6 >= .30 * commanded_period_s):
        raise ValueError("Deadtime must be <30% of the commanded period")
    if baseline_v is None:
        step = max(1, len(volts) // 20000)
        baseline_v = statistics.median(volts[::step])
    if not math.isfinite(baseline_v) or threshold >= baseline_v:
        raise ValueError("Threshold must be below baseline")
    if min_event_separation_s is None:
        min_separation = .30 * commanded_period_s
    else:
        min_separation = float(min_event_separation_s)
        if (not math.isfinite(min_separation) or min_separation <= 0
                or min_separation >= commanded_period_s):
            raise ValueError(
                "min_event_separation_s must be finite, positive and below "
                "the shortest supported period"
            )
    phase_deadtime_s = phase_deadtime_us * 1e-6
    confirm = max(2, min(4, int(30e-6 / dt)))
    quiet_samples = max(2, math.ceil(quiet_before_us * 1e-6 / dt))
    if quiet_band_v is None:
        quiet_band_v = 1.5 * max(1.0, 6 * noise_sigma_v,
                                 .25 * (baseline_v - threshold))
    if not math.isfinite(quiet_band_v) or quiet_band_v <= 0:
        raise ValueError("quiet_band_v must be finite and positive")
    max_leading_gap_s = max(30e-6, 3 * dt)
    partial_start = abs(volts[0] - baseline_v) > quiet_band_v

    recent_quiet = deque()
    recent_quiet_count = 0
    adjacent_quiet_idx = None
    uninterrupted_quiet = 0
    crossing_start = None
    prequiet_occupancy = 0.
    prequiet_full = False
    prequiet_adjacent = False
    accepted, suppressed, rejected = [], [], []
    last_event_time = None
    for i, value in enumerate(volts):
        is_quiet = abs(value - baseline_v) <= quiet_band_v
        run_before = uninterrupted_quiet
        if value > threshold:
            crossing_start = None
        else:
            if crossing_start is None:
                crossing_start = i
                prequiet_occupancy = (recent_quiet_count / quiet_samples
                                      if i >= quiet_samples else 0.)
                prequiet_full = run_before >= quiet_samples
                prequiet_adjacent = (adjacent_quiet_idx is not None and
                                     times[i] - times[adjacent_quiet_idx]
                                     <= max_leading_gap_s)
            if i - crossing_start + 1 == confirm:
                event = {"sample_index": crossing_start,
                         "time_s": times[crossing_start],
                         "voltage_v": volts[crossing_start],
                         "status": "CANDIDATE_REVIEW"}
                lag = (event["time_s"] - last_event_time
                       if last_event_time is not None else None)
                if crossing_start == 0:
                    event["status"] = "PARTIAL_AT_RECORD_START"
                    rejected.append(event)
                elif lag is not None and lag < phase_deadtime_s:
                    event["status"] = "SECONDARY_PHASE_WITHIN_DEADTIME"
                    suppressed.append(event)
                elif lag is not None and lag < min_separation:
                    event["status"] = "SUPPRESSED_CLOSE_CROSSING"
                    suppressed.append(event)
                elif not prequiet_adjacent or prequiet_occupancy < min_quiet_fraction:
                    event["status"] = "REJECTED_INSUFFICIENT_BASELINE"
                    rejected.append(event)
                elif prequiet_full:
                    accepted.append(event)
                    last_event_time = event["time_s"]
                else:
                    # Relaxation is conditional on a complete biphasic
                    # waveform, not just a plausible pulse-slot timestamp.
                    width = min(len(volts), crossing_start +
                                max(2, int(.00035 / dt)))
                    window = volts[crossing_start:width]
                    drop = baseline_v - threshold
                    strong_shape = (min(window) <= baseline_v - max(12., 1.5*drop)
                                    and max(window) >= baseline_v + max(8., drop))
                    if strong_shape:
                        event["status"] = "RELAXED_QUIET_REVIEW"
                        accepted.append(event)
                        last_event_time = event["time_s"]
                    else:
                        event["status"] = "REJECTED_RELAXED_WEAK_MORPHOLOGY"
                        rejected.append(event)
        uninterrupted_quiet = uninterrupted_quiet + 1 if is_quiet else 0
        if is_quiet:
            adjacent_quiet_idx = i
        recent_quiet.append(1 if is_quiet else 0)
        recent_quiet_count += 1 if is_quiet else 0
        if len(recent_quiet) > quiet_samples:
            recent_quiet_count -= recent_quiet.popleft()

    return {"events": accepted, "suppressed": suppressed,
            "rejected": rejected, "partial_start": partial_start,
            "confirm_samples": confirm,
            "min_separation_s": min_separation, "dt_s": dt,
            "quiet_before_us": quiet_before_us,
            "quiet_samples": quiet_samples,
            "quiet_band_v": quiet_band_v,
            "min_quiet_fraction": min_quiet_fraction,
            "phase_deadtime_us": phase_deadtime_us,
            "max_leading_gap_s": max_leading_gap_s}


