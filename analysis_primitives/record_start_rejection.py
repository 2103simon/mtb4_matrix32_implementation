"""Record-start boundary classification of already rejected onset candidates.

This exact S06-F3/F4 measurement distinguishes scope-acquisition-edge evidence
from interior detector rejects. The caller owns the rejection-window policy.
It does not alter S02 detection, accepted pulses or rejected-event records.
"""


def classify_record_start_boundary_reject(
    row, pulses, record_start_s, *, reject_window_s
):
    """True for rejected candidates attributable to the acquisition boundary.

    With trigger position 0%, the record starts on the triggering CH1 pulse.
    That pulse cannot satisfy the detector's required pre-baseline window.
    Preserve only rejected candidates that occur within the first 1 ms of the
    record and before the first accepted onset as boundary evidence. Rejected
    candidates later in the record remain hard review items.
    """
    if (
        row.get("status") == "PARTIAL_AT_RECORD_START"
        and row.get("sample_index") == 0
    ):
        return True
    if not pulses:
        return False
    time_s = row.get("time_s")
    if time_s is None:
        return False
    offset_s = time_s - record_start_s
    return (
        0.0 <= offset_s <= reject_window_s
        and time_s < pulses[0]["time_s"]
    )
