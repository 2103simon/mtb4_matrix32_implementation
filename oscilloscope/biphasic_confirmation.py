"""Independent positive-phase confirmation for S02 negative onset candidates.

Does not change the source-locked negative-onset detector or its timestamps.
A pulse for the current biphasic experiment must exhibit a sustained positive
threshold crossing shortly AFTER the candidate negative onset.

Threshold 8 V above robust baseline (or larger when CH1 detection drop/noise
requires it); a 550 us window stays below a quarter of the minimum 2.5 ms
stimulation period. Three consecutive positive samples reject single-sample
noise spikes. A partial pulse at the end of a record stays unconfirmed.
"""
from __future__ import annotations

import math

import numpy as np

VERSION = "BIPHASIC_POSITIVE_CONFIRM_V1"
MAX_POSITIVE_DELAY_US = 550.0
POSITIVE_MIN_EXCURSION_V = 8.0
MIN_CONSECUTIVE_POSITIVE_SAMPLES = 3


def confirm_positive_phase(t, volts, onset, *, baseline_v, noise_sigma_v,
                           negative_drop_v):
    """Return the measured positive crossing (or explicit rejection evidence).

    Existing negative event timestamp remains unchanged. The search window
    starts at the negative crossing; it does not borrow a later cycle.
    """
    idx = int(onset["sample_index"])
    if not 0 <= idx < len(t):
        raise ValueError("Negative onset sample index outside waveform")
    if not (math.isfinite(baseline_v) and math.isfinite(noise_sigma_v)
            and math.isfinite(negative_drop_v) and negative_drop_v > 0
            and noise_sigma_v >= 0):
        raise ValueError("Invalid voltage baseline/noise/negative threshold")
    threshold = baseline_v + max(
        POSITIVE_MIN_EXCURSION_V, negative_drop_v, 6.0 * noise_sigma_v)
    end = int(np.searchsorted(
        t, float(t[idx]) + MAX_POSITIVE_DELAY_US * 1e-6,
        side="right"))
    window = np.asarray(volts[idx:end])
    # The baseline-to-positive transition must occur after a negative phase;
    # the established negative detector already verifies onset crossing.
    peak = float(np.max(window)) if window.size else None
    mask = window >= threshold
    count = MIN_CONSECUTIVE_POSITIVE_SAMPLES
    run = (np.convolve(mask.astype(np.int8), np.ones(count, dtype=np.int8),
                       mode="valid") if len(mask) >= count else np.empty(0))
    hits = np.flatnonzero(run == count)
    if not hits.size:
        return {
            "phase_status": "MISSING_POSITIVE_PHASE",
            "positive_threshold_v": threshold,
            "positive_max_v": peak,
            "positive_time_s": None,
            "positive_delay_us": None,
            "positive_window_us": MAX_POSITIVE_DELAY_US,
        }
    positive_idx = idx + int(hits[0])
    return {
        "phase_status": "BIPHASIC_CONFIRMED",
        "positive_threshold_v": threshold,
        "positive_max_v": peak,
        "positive_time_s": float(t[positive_idx]),
        "positive_delay_us": float((t[positive_idx]-t[idx]) * 1e6),
        "positive_window_us": MAX_POSITIVE_DELAY_US,
    }
