"""Pure center/robust-noise arithmetic on caller-selected voltage samples.

C05-1/C05-2: The caller alone chooses the input samples and their scientific
meaning (full record, stride, pretrigger window, or event-relative window).
There is no time window, resampling, filtering, minimum-count gate, nonfinite
policy, signal qualification, or amplitude interpretation in this module.

For a given (reiterable) selection of samples, match the historical M00
`baseline_stats` calculation exactly, including statistics.median's original
empty/odd/even, numeric and nonfinite behavior and operation order.
"""
from __future__ import annotations

import statistics


def median_mad_scale(selected_values):
    """Return (median, 1.4826 * MAD) for already-selected values.

    Arithmetic is intentionally identical to M00's original baseline_stats::

        center = statistics.median(values)
        mad = statistics.median(abs(x - center) for x in values)
        return center, 1.4826 * mad

    This function does not change inputs or convert them to a list. In
    particular, callers must supply the same reusable sequence they used
    previously; accepting a one-shot iterator does not mean it is silently
    materialized. An empty sequence raises statistics.StatisticsError, as
    before. Nonfinite input is not repaired or silently discarded.
    """
    center = statistics.median(selected_values)
    mad = statistics.median(abs(value - center) for value in selected_values)
    return center, 1.4826 * mad

def median_or_none(selected_values):
    """Median of already-selected values, or None for a false-y selection.

    Matches the original M05 median wrappers exactly: no MAD or scale is
    calculated. In particular, this intentionally retains the old truth-value
    check, statistics.median arithmetic and exception/nonfinite behavior.
    The caller owns window selection, minimum counts, and missing-event rules.
    Iterators are not materialized or filtered.
    """
    return statistics.median(selected_values) if selected_values else None
