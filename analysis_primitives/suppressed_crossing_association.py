"""Associate S02 detector-suppressed crossings with accepted pulse onsets.

This measurement annotates retained suppressed-event evidence, not detector-
rejected onsets or independent accepted pulses. It is distinct from cross-trace
clustering and provisional event ownership. Caller-specific minimum event
separation is explicit; native Python ordering, float and error semantics are
preserved without adding sorting, rounding, validation or tolerances.
"""


def associate_suppressed_crossings_with_accepted_pulses(
    pulses, suppressed, *, min_event_separation_s
):
    """Classify detector-suppressed close crossings against accepted pulses.

    A SUPPRESSED_CLOSE_CROSSING within the detector's own minimum event
    separation is retained as within-pulse morphology rather than treated as
    evidence of an independent missed onset. Other or unassociated suppressed
    candidates remain review items.
    """
    associated = []
    unassociated = []
    for row in suppressed:
        if not pulses:
            unassociated.append(dict(row))
            continue
        nearest = min(
            pulses, key=lambda pulse: abs(pulse["time_s"] - row["time_s"]))
        delta_s = row["time_s"] - nearest["time_s"]
        cooked = {
            **row,
            "nearest_pulse_id": nearest["id"],
            "nearest_pulse_time_s": nearest["time_s"],
            "delta_to_nearest_pulse_us": delta_s * 1e6,
        }
        if (
            row.get("status") == "SUPPRESSED_CLOSE_CROSSING"
            and abs(delta_s) <= min_event_separation_s
        ):
            associated.append(cooked)
        else:
            unassociated.append(cooked)
    return associated, unassociated
