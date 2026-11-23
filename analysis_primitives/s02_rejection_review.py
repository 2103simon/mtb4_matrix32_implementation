"""Explain every rejected S02 transition; propose (never silently accept) gap repairs.

Read-only/offline diagnostics for the negative-onset detector. A transition
with a strong negative/positive morphology near a missing cycle in an otherwise
regular accepted-onset train is a *review candidate*, not a verified pulse.
"""
from bisect import bisect_left
import math
from pathlib import Path
import statistics

import matplotlib.pyplot as plt


def _f(value, digits=3):
    return "n/a" if value is None else f"{value:.{digits}f}"


def _envelope(times, volts, lo, hi, max_bins=2400):
    """Min/max display, preserving short excursions in each plot bin."""
    width = max(1, math.ceil((hi - lo) / max_bins))
    centers, lower, upper = [], [], []
    for a in range(lo, hi, width):
        b = min(a + width, hi)
        centers.append(times[(a + b - 1) // 2] * 1e3)
        lower.append(min(volts[a:b]))
        upper.append(max(volts[a:b]))
    return centers, lower, upper


def analyze_rejections(times, volts, accepted, rejected, suppressed, *,
                       commanded_period_s, threshold_v, baseline_v,
                       quiet_band_v):
    """Describe rejected transition positions, waveform and possible gaps."""
    if not times or len(times) != len(volts):
        raise ValueError("Missing/mismatched waveform")
    accepted = sorted(accepted, key=lambda row: row["time_s"])
    rejected = sorted([*rejected, *suppressed],
                      key=lambda row: (row["time_s"], row["sample_index"]))
    accepted_times = [e["time_s"] for e in accepted]
    typical = [b - a for a, b in zip(accepted_times, accepted_times[1:])
               if .5 * commanded_period_s < b - a < 1.5 * commanded_period_s]
    measured_period_s = statistics.median(typical) if typical else commanded_period_s
    dt = statistics.median(b - a for a, b in zip(times[:1001], times[1:1002]))
    results = []
    for number, event in enumerate(rejected, 1):
        t = event["time_s"]
        idx = event["sample_index"]
        adjacent = bisect_left(accepted_times, t)
        previous = accepted_times[adjacent - 1] if adjacent else None
        following = accepted_times[adjacent] if adjacent < len(accepted_times) else None

        # Count the unbroken quiet run directly BEFORE the rejected crossing,
        # not any quiet baseline elsewhere in the record.
        quiet_count = 0
        j = idx - 1
        while j >= 0 and abs(volts[j] - baseline_v) <= quiet_band_v:
            quiet_count += 1
            j -= 1

        # Check for the two-phase waveform following a negative crossing;
        # this is deliberately descriptive, not a calibrated pulse classifier.
        limit = min(len(times), bisect_left(times, t + min(.0004, .3 * measured_period_s)))
        nend = min(limit, bisect_left(times, t + .00016))
        if nend > idx:
            trough_idx = min(range(idx, nend), key=volts.__getitem__)
            negative_min = volts[trough_idx]
        else:
            trough_idx, negative_min = idx, volts[idx]
        if limit > trough_idx + 1:
            positive_max = max(volts[trough_idx + 1:limit])
        else:
            positive_max = None
        drop = baseline_v - threshold_v
        # Require both lobes to be prominent compared with the threshold;
        # the first trigger-boundary return normally fails this condition.
        morphology_support = (
            negative_min <= baseline_v - max(12.0, 1.5 * drop)
            and positive_max is not None
            and positive_max >= baseline_v + max(8.0, drop)
        )

        gap_periods, expected_slot, error = None, None, None
        candidate_in_gap = False
        if previous is not None and following is not None:
            gap = following - previous
            gap_periods = int(round(gap / measured_period_s))
            if 2 <= gap_periods <= 4 and (
                    abs(gap - gap_periods * measured_period_s)
                    <= .12 * gap_periods * measured_period_s):
                slots = [previous + k * measured_period_s
                         for k in range(1, gap_periods)]
                expected_slot = min(slots, key=lambda slot: abs(t - slot))
                error = t - expected_slot
                candidate_in_gap = abs(error) <= .12 * measured_period_s

        boundary = idx == 0 or (previous is None and
                                 t - times[0] < .45 * measured_period_s)
        if boundary:
            tag = "BOUNDARY_FRAGMENT_REVIEW"
        elif candidate_in_gap and morphology_support:
            tag = "POTENTIAL_PULSE_IN_REJECTED_GAP"
        elif candidate_in_gap:
            tag = "TIMED_GAP_CANDIDATE_WEAK_MORPHOLOGY"
        elif morphology_support:
            tag = "STRONG_EVENT_OUTSIDE_DETECTED_GAP_REVIEW"
        else:
            tag = "SECONDARY_CROSSING_OR_WEAK_EVENT_REVIEW"

        # A provisional gap repair may be evaluated independently; never
        # rewrite 'events.csv', 'periods.csv', or the primary statistics.
        propose_rescue = tag == "POTENTIAL_PULSE_IN_REJECTED_GAP"
        safe_status = "".join(ch if ch.isalnum() or ch == "_"
                              else "_" for ch in event["status"])
        image_name = (f"R{number:03d}_t{t*1e3:010.4f}ms_"
                      f"{safe_status}.png")
        results.append({
            "id": f"R{number:03d}",
            "sample_index": idx,
            "time_s": t,
            "voltage_v": event["voltage_v"],
            "original_status": event["status"],
            "diagnostic_tag": tag,
            "quiet_before_us": quiet_count * dt * 1e6,
            "quiet_band_v": quiet_band_v,
            "negative_min_v": negative_min,
            "positive_max_v": positive_max,
            "strong_biphasic_shape": morphology_support,
            "previous_accepted_s": previous,
            "next_accepted_s": following,
            "detected_gap_cycles": gap_periods,
            "expected_slot_s": expected_slot,
            "delta_to_expected_us": error * 1e6 if error is not None else None,
            "proposed_for_gap_repair": propose_rescue,
            "figure": "figures/rejected/" + image_name,
        })
    return results, measured_period_s


def provisional_gap_onsets(accepted, reviews):
    """Optional what-if timeline; never modifies accepted primary onsets."""
    proposals = [r for r in reviews if r["proposed_for_gap_repair"]]
    # Multiple proposals for the SAME nominal missing slot are ambiguous:
    # do not choose one silently.
    used = set()
    unique = []
    ambiguous = []
    for proposal in proposals:
        key = (proposal["previous_accepted_s"], proposal["next_accepted_s"],
               proposal["expected_slot_s"])
        if sum((r["previous_accepted_s"], r["next_accepted_s"],
                r["expected_slot_s"]) == key for r in proposals) != 1:
            ambiguous.append(proposal["id"])
            continue
        if key not in used:
            unique.append(proposal)
            used.add(key)
    combined = [dict(e) for e in accepted]
    for proposal in unique:
        combined.append({
            "sample_index": proposal["sample_index"],
            "time_s": proposal["time_s"],
            "voltage_v": proposal["voltage_v"],
            "status": "PROVISIONAL_GAP_RESCUE_" + proposal["id"],
        })
    combined.sort(key=lambda e: e["time_s"])
    return combined, ambiguous


def save_rejection_figures(output, times, volts, accepted, reviews, *,
                           threshold_v, baseline_v, quiet_band_v,
                           measured_period_s):
    """Write exactly one well-identified PNG for EACH rejection."""
    folder = Path(output) / "figures" / "rejected"
    folder.mkdir(parents=True, exist_ok=True)
    accepted_times = [e["time_s"] for e in accepted]
    for review in reviews:
        center = review["time_s"]
        # Context shows nearby periods; detailed view shows onset morphology.
        broad_half = min(1.4 * measured_period_s, .012)
        near_before = .00045
        near_after = .00065
        fig, axes = plt.subplots(2, 1, figsize=(13, 7))
        for ax, lo_time, hi_time, envelope in [
                (axes[0], center - broad_half, center + broad_half, True),
                (axes[1], center - near_before, center + near_after, False)]:
            start = max(0, bisect_left(times, lo_time))
            stop = min(len(times), bisect_left(times, hi_time))
            if stop <= start:
                continue
            if envelope:
                xx, low, high = _envelope(times, volts, start, stop)
                ax.fill_between(xx, low, high, alpha=.6, label="CH1 min/max envelope")
            else:
                ax.plot([x * 1e3 for x in times[start:stop]], volts[start:stop],
                        lw=1, label="CH1 actual samples")
            ax.axvline(center * 1e3, color="C3", ls="--", lw=1.5,
                       label=f"{review['id']} REJECTED t={center*1e3:.4f} ms")
            ax.axhline(threshold_v, color="C1", ls=":", label="negative threshold")
            ax.axhspan(baseline_v - quiet_band_v,
                       baseline_v + quiet_band_v, alpha=.08,
                       color="C2", label="quiet baseline band")
            expected = review["expected_slot_s"]
            if expected is not None and lo_time < expected < hi_time:
                ax.axvline(expected * 1e3, color="C4", ls="-.",
                           label="expected gap slot (measured median period)")
            for pos in accepted_times:
                if lo_time <= pos <= hi_time:
                    ax.axvline(pos * 1e3, color="C2", alpha=.55,
                               lw=.8, ls="-")
            ax.set(xlim=(max(times[0], lo_time)*1e3,
                          min(times[-1], hi_time)*1e3),
                   ylabel="CH1 voltage (V)")
            ax.grid(alpha=.2)
            ax.legend(loc="upper right", fontsize=7, ncol=2)
        axes[0].set_title(
            f"{review['id']} REJECTED / {review['original_status']} | "
            f"t={center*1e3:.4f} ms | {review['diagnostic_tag']}",
            fontsize=12)
        axes[1].set_title(
            "Zoom: quiet-before=%.1f us, min=%.1f V, subsequent max=%s V, "
            "nearest gap-slot error=%s us" %
            (review["quiet_before_us"], review["negative_min_v"],
             _f(review["positive_max_v"], 1),
             _f(review["delta_to_expected_us"], 2)),
            fontsize=10)
        axes[1].set_xlabel("Scope-relative time (ms)")
        fig.tight_layout()
        fig.savefig(Path(output) / review["figure"], dpi=160)
        plt.close(fig)
    return len(reviews)
