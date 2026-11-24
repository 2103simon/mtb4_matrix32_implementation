"""Period-state labeling based on caller-supplied nominal period centers.

The caller retains ownership of its experiment's period centers, radius,
state names and interpretation. This intentionally preserves the original
S06-F3/F4 binary-float comparison, insertion-order iteration, single-match
requirement, and failure behavior; no rounding or tolerance is applied.
"""


def classify_period_by_centers(value, *, period_centers_s, period_radius_s):
    """Return the original unique '<name>_LIKE_PERIOD' or review label."""
    matches = [
        name for name, center in period_centers_s.items()
        if abs(value - center) <= period_radius_s
    ]
    return matches[0] + "_LIKE_PERIOD" if len(matches) == 1 else "OTHER_PERIOD_REVIEW"
