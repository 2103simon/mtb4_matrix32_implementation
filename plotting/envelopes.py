"""Display-only min/max waveform envelope; never used for numerical analysis.

Original sample extrema are preserved within every consecutive display bin.
Input times are returned in their original units. No smoothing, interpolation,
event detection, or scientific sample selection takes place.
"""
import math


def minmax_envelope(times, values, bins=1700):
    """Return original-time midpoint and min/max samples per display bin.

    Arithmetic and grouping match S02's original minmax_envelope exactly.
    """
    stride = max(1, math.ceil(len(values) / bins))
    xs, lows, highs = [], [], []
    for start in range(0, len(values), stride):
        end = min(start + stride, len(values))
        xs.append(times[(start + end - 1) // 2])
        section = values[start:end]
        lows.append(min(section))
        highs.append(max(section))
    return xs, lows, highs
