"""Offline Matrix32 CH1 onset audit; no hardware access, no automatic validation.

Uses the source-identical S02-v5 negative-onset detector from the sibling
matrix32_nerve_stimulation repository. The integrated analyzer requires this
source-locked negative-onset inventory to be reviewed and approved before
physical pulse-frequency analysis; there is no automatic positive-edge fallback.

Usage:
  python oscilloscope/review_pulse_detection.py --run-dir data/recordings/RUN
  python oscilloscope/review_pulse_detection.py --run-dir ... --record 2

Writes per-record candidate onset and pulse-interval inventories and a
sensitivity/rejection audit. Do not interpret intervals as verified output
rate until the operator signs off detection completeness per record.
"""
from __future__ import annotations
from experiment_analysis.paths import analysis_root

import argparse
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from analysis_primitives.negative_events import (
    NEGATIVE_EVENT_DETECTOR_VERSION, detect_events, estimate_threshold,
)
from analysis_primitives.record_start_rejection import (
    classify_record_start_boundary_reject,
)
from analysis_primitives.suppressed_crossing_association import (
    associate_suppressed_crossings_with_accepted_pulses,
)
from oscilloscope.analyze_integrated_matrix32 import load_wave
from oscilloscope.biphasic_confirmation import (
    VERSION as BIPHASIC_VERSION, confirm_positive_phase,
    MAX_POSITIVE_DELAY_US, POSITIVE_MIN_EXCURSION_V,
)
from oscilloscope.record_scope import atomic_json

SHORTEST_SUPPORTED_PERIOD_S = 1.0 / 400.0
MIN_EVENT_SEPARATION_S = 0.30 * SHORTEST_SUPPORTED_PERIOD_S
RECORD_START_REJECT_WINDOW_S = 0.001


def json_safe(value):
    """Convert NumPy scalar containers to ordinary JSON types, recursively.

    The source-locked S02 detector deliberately returns some numpy.bool_
    and numpy numeric values when called with NumPy waveform arrays.
    Preserve those values and detector logic; normalize only at the report
    serialization boundary.
    """
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return [json_safe(item) for item in value.tolist()]
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def write_csv(path, rows, fields):
    path = Path(path)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def detect_review(t, volts):
    """Run the *unchanged* upstream detector at three threshold sensitivities."""
    if len(t) < 3 or len(t) != len(volts):
        raise ValueError("Expected equally sized nonempty CH1 time/voltage arrays")
    if not np.all(np.isfinite(t)) or not np.all(np.isfinite(volts)):
        raise ValueError("Nonfinite CH1 waveform")
    if not np.all(np.diff(t) > 0):
        raise ValueError("CH1 time axis must increase")
    defaults = estimate_threshold(volts)
    quiet_band = 1.5 * max(
        1.0, 6 * defaults["noise_sigma_v"], 0.25 * defaults["threshold_drop_v"]
    )
    results = {}
    threshold_counts = {}
    for fraction in (0.8, 1.0, 1.2):
        result = detect_events(
            t, volts,
            defaults["baseline_v"] - fraction * defaults["threshold_drop_v"],
            SHORTEST_SUPPORTED_PERIOD_S,
            baseline_v=defaults["baseline_v"],
            noise_sigma_v=defaults["noise_sigma_v"],
            quiet_before_us=200.0,
            quiet_band_v=quiet_band,
            min_quiet_fraction=0.75,
            phase_deadtime_us=150.0,
            min_event_separation_s=MIN_EVENT_SEPARATION_S,
        )
        results[fraction] = result
        threshold_counts[str(fraction)] = len(result["events"])
    primary = results[1.0]
    # Source detector is unchanged; independently audit sensitivity-only
    # events so a stable event count cannot hide different pulse locations.
    primary_times = [float(e["time_s"]) for e in primary["events"]]
    mismatches = []
    for fraction in (0.8, 1.2):
        alt_times = [float(e["time_s"]) for e in results[fraction]["events"]]
        for event in results[fraction]["events"]:
            time_s = float(event["time_s"])
            if not primary_times or min(abs(time_s-p) for p in primary_times) > .0005:
                mismatches.append({
                    "threshold_fraction": fraction,
                    "source": "alternative_threshold_only",
                    "time_s": time_s, "voltage_v": float(event["voltage_v"]),
                    "status": event["status"],
                })
        for event in primary["events"]:
            time_s = float(event["time_s"])
            if not alt_times or min(abs(time_s-p) for p in alt_times) > .0005:
                mismatches.append({
                    "threshold_fraction": fraction,
                    "source": "primary_not_matched_by_alternative",
                    "time_s": time_s, "voltage_v": float(event["voltage_v"]),
                    "status": event["status"],
                })
    # A single location can be flagged at both alternate thresholds; keep
    # the provenance, never silently merge detections or add invented onsets.
    for i, row in enumerate(mismatches, 1):
        row["id"] = f"Q{i:05d}"
    # Keep the S02 negative-onset candidate timestamps unchanged. An
    # independent positive-phase gate decides whether a biphasic pulse is
    # automatically eligible for the frequency inventory. Negative-only
    # dips remain reviewable R candidates rather than disappearing.
    pulses, phase_unpaired = [], []
    for row in primary["events"]:
        phase = confirm_positive_phase(
            t, volts, row,
            baseline_v=float(defaults["baseline_v"]),
            noise_sigma_v=float(defaults["noise_sigma_v"]),
            negative_drop_v=float(defaults["threshold_drop_v"]),
        )
        event = {
            "time_s": float(row["time_s"]),
            "voltage_v": float(row["voltage_v"]),
            "sample_index": int(row["sample_index"]),
            "status": row["status"],
            **phase,
        }
        if phase["phase_status"] == "BIPHASIC_CONFIRMED":
            event["id"] = f"P{len(pulses) + 1:05d}"
            pulses.append(event)
        else:
            event["original_negative_status"] = event["status"]
            event["status"] = "REJECTED_MISSING_POSITIVE_PHASE"
            phase_unpaired.append(event)
    rejected = [dict(row) for row in primary["rejected"]]
    rejected.extend(phase_unpaired)
    boundary = [
        row for row in rejected
        if classify_record_start_boundary_reject(
            row, pulses, float(t[0]),
            reject_window_s=RECORD_START_REJECT_WINDOW_S,
        )
    ]
    associated, unassociated = (
        associate_suppressed_crossings_with_accepted_pulses(
            pulses, primary["suppressed"],
            min_event_separation_s=MIN_EVENT_SEPARATION_S,
        )
    )
    periods = []
    for i, (a, b) in enumerate(zip(pulses, pulses[1:]), 1):
        period = b["time_s"] - a["time_s"]
        periods.append({
            "id": f"T{i:05d}",
            "start_pulse_id": a["id"], "end_pulse_id": b["id"],
            "start_s": a["time_s"], "end_s": b["time_s"],
            "period_s": period, "observed_period_ms": period * 1000,
            "provisional_inverse_period_hz": 1 / period if period > 0 else None,
            "review_status": "UNVERIFIED_PULSE_CANDIDATES",
        })
    counts_identical = len(set(threshold_counts.values())) == 1
    sampling_us = float(np.median(np.diff(t)) * 1e6)
    flags = []
    if sampling_us > 10:
        flags.append("COARSE_SAMPLE_SPACING_REVIEW_SHORT_PULSE_MORPHOLOGY")
    if not counts_identical:
        flags.append("ONSET_THRESHOLD_COUNT_SENSITIVITY")
    if mismatches:
        flags.append("THRESHOLD_SPECIFIC_ONSET_TIMES_REQUIRE_REVIEW")
    if len(rejected) > len(boundary):
        flags.append("INTERIOR_ONSET_REVIEW_REQUIRED")
    if unassociated:
        flags.append("UNASSOCIATED_SUPPRESSED_ONSETS_REVIEW_REQUIRED")
    if any(row["status"] == "RELAXED_QUIET_REVIEW" for row in pulses):
        flags.append("RELAXED_QUIET_ACCEPTED_CANDIDATES_REQUIRE_REVIEW")
    if phase_unpaired:
        flags.append("NEGATIVE_ONLY_EVENTS_REQUIRE_EXPLICIT_REVIEW")
    if primary["partial_start"]:
        flags.append("RECORD_START_MAY_CONTAIN_TRUNCATED_PULSE")
    return {
        "detector": NEGATIVE_EVENT_DETECTOR_VERSION + "+" + BIPHASIC_VERSION,
        "negative_onset_detector": NEGATIVE_EVENT_DETECTOR_VERSION,
        "positive_phase_detector": BIPHASIC_VERSION,
        "positive_phase_window_us": MAX_POSITIVE_DELAY_US,
        "positive_phase_min_excursion_v": POSITIVE_MIN_EXCURSION_V,
        "negative_onset_candidates_before_phase_gate": len(primary["events"]),
        "negative_only_rejected_count": len(phase_unpaired),
        "source": "matrix32_nerve_stimulation/experiments/analysis_primitives/negative_events.py",
        "sampling_interval_us": sampling_us,
        "accepted_pulses": pulses,
        "periods": periods,
        "rejected": rejected,
        "suppressed": [dict(row) for row in primary["suppressed"]],
        "suppressed_associated": associated,
        "suppressed_unassociated": unassociated,
        "boundary_rejected": boundary,
        "threshold": defaults,
        "threshold_sensitivity_event_counts": threshold_counts,
        "threshold_sensitivity_unmatched_events": mismatches,
        "flags": flags,
        "complete_pulse_detection_approved": False,
        "interpretation": (
            "Automatically eligible pulse candidates require negative onset "
            "AND sustained positive phase. Negative-only events are rejected "
            "candidates, not frequency pulses, but remain reviewable. "
            "Inverse periods are provisional unverified interval estimates, "
            "not proof of ACK state application or continuous output."
        ),
    }


def archive_unapproved_report(output):
    """Preserve an unedited, unsigned review before regenerating a fresh one.

    A signed approval, edited decisions or manual additions are protected
    even if the explicit replacement option is given.
    """
    from oscilloscope.pulse_review_workflow import csv_rows
    output = Path(output)
    if not output.exists():
        return None
    for name in ("review_approval.json", "approved_onsets.csv",
                 "approved_intervals.csv"):
        if (output / name).exists():
            raise FileExistsError(
                f"Existing approved evidence ({name}) is protected: {output}")
    decisions = output / "review_decisions.csv"
    additions = output / "manual_additions.csv"
    manifest = output / "review_manifest.json"
    if not all(path.exists() for path in (decisions, additions, manifest)):
        raise FileExistsError(
            f"Existing review is incomplete/ambiguous and cannot be replaced: {output}")
    if any((row.get("decision", "").strip() or row.get("note", "").strip())
           for row in csv_rows(decisions)):
        raise FileExistsError("Operator edits to review_decisions.csv are protected")
    if csv_rows(additions):
        raise FileExistsError("Manual onset additions are protected")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive = output.with_name(output.name + "_unapproved_backup_" + stamp)
    output.rename(archive)
    print(f"[PULSE REVIEW] Archived previous unapproved report: {archive}",
          flush=True)
    return archive


def analyze_record(root, record, *, overwrite_unapproved=False):
    idx = int(record["index"])
    if record.get("status") != "captured":
        raise ValueError(f"Record {idx} was not completely captured")
    path = root / record["channels"]["CH1"]["path"]
    output = analysis_root(root) / f"record_{idx:05d}" / "pulse_review"
    # Refuse before writing detector tables or figures: make_scaffold() can
    # detect a stale review only AFTER those source-hashed tables exist.
    # A rerun must not invalidate prior evidence or replace approved markers
    # with automated candidates, even when the original waveform is unchanged.
    protected = (
        "review_manifest.json", "review_decisions.csv", "manual_additions.csv",
        "review_approval.json", "approved_onsets.csv", "approved_intervals.csv",
    )
    if overwrite_unapproved and output.exists():
        archive_unapproved_report(output)
    if any((output / name).exists() for name in protected):
        raise FileExistsError(
            f"Record {idx}: existing pulse-review evidence at {output}. "
            "Preserve/archive the entire review before generating a new "
            "version. To refresh an approved waveform figure, use "
            "plot_approved_ch1_panels.py without rerunning detection."
        )
    t, volts = load_wave(path)
    output.mkdir(parents=True, exist_ok=True)
    try:
        result = detect_review(t, volts)
    except ValueError as exc:
        result = {
            "detector": NEGATIVE_EVENT_DETECTOR_VERSION,
            "complete_pulse_detection_approved": False,
            "status": "DETECTOR_UNQUALIFIED",
            "reason": str(exc),
            "sampling_interval_us": float(np.median(np.diff(t)) * 1e6),
            "threshold": {},
            "threshold_sensitivity_event_counts": {},
            "threshold_sensitivity_unmatched_events": [],
            "accepted_pulses": [], "periods": [],
            "rejected": [], "suppressed": [],
            "flags": ["DETECTOR_UNQUALIFIED"],
        }
    # Preserve S06's near-ADC-rail detector-review gate on the original
    # raw Tektronix codes, separately from calibrated voltage extrema.
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if "raw" in (reader.fieldnames or []):
            rail_count = sum(
                abs(float(row["raw"])) >= 32766
                for row in reader if row.get("raw") not in (None, "")
            )
        else:
            rail_count = None
    result["near_storage_rail_count"] = rail_count
    if rail_count is None:
        result["flags"].append("RAW_ADC_CODES_MISSING_CANNOT_CHECK_RAIL")
    elif rail_count:
        result["flags"].append("NEAR_STORAGE_RAIL_REVIEW")
    result["interior_rejected_count"] = (
        len(result["rejected"])-len(result.get("boundary_rejected", []))
    )
    result["record_start_boundary_rejected_count"] = len(
        result.get("boundary_rejected", [])
    )
    result["record_index"] = idx
    result["source_ch1"] = str(record["channels"]["CH1"]["path"])
    result["sampling_points"] = int(len(t))
    result["capture_start_s"] = float(t[0])
    result["capture_end_s"] = float(t[-1])
    atomic_json(output / "audit.json", json_safe({
        k: v for k, v in result.items()
        if k not in ("accepted_pulses", "periods", "rejected",
                     "suppressed", "suppressed_associated",
                     "suppressed_unassociated", "boundary_rejected",
                     "threshold_sensitivity_unmatched_events")
    } | {
        "accepted_count": len(result["accepted_pulses"]),
        "period_count": len(result["periods"]),
        "rejected_count": len(result["rejected"]),
        "suppressed_count": len(result["suppressed"]),
        "unassociated_suppressed_count": len(
            result.get("suppressed_unassociated", [])
        ),
        "threshold_specific_onset_review_count": len(
            result.get("threshold_sensitivity_unmatched_events", [])
        ),
    }))
    phase_fields = ("phase_status", "positive_time_s", "positive_delay_us",
                    "positive_threshold_v", "positive_max_v",
                    "positive_window_us")
    write_csv(output / "accepted_onsets.csv", result["accepted_pulses"],
              ("id", "time_s", "voltage_v", "sample_index", "status")
              + phase_fields)
    write_csv(output / "periods_unverified.csv", result["periods"],
              ("id", "start_pulse_id", "end_pulse_id", "start_s", "end_s",
               "period_s", "observed_period_ms",
               "provisional_inverse_period_hz", "review_status"))
    write_csv(output / "rejected_onsets.csv", result["rejected"],
              ("sample_index", "time_s", "voltage_v", "status")
              + phase_fields + ("original_negative_status",))
    write_csv(output / "suppressed_onsets.csv", result["suppressed"],
              ("sample_index", "time_s", "voltage_v", "status"))
    write_csv(output / "threshold_sensitive_onsets.csv",
              result.get("threshold_sensitivity_unmatched_events", []),
              ("id", "threshold_fraction", "source", "time_s",
               "voltage_v", "status"))
    # Reuse S01/S02 plotting code and create a source-locked human review.
    # Existing edited review files are never overwritten silently.
    # Normalize NumPy scalars also for the later report metrics JSON and
    # reviewer context, not only the initial audit.json write.
    result = json_safe(result)
    from oscilloscope.pulse_review_workflow import (
        make_scaffold, plot_report, report_markdown,
    )
    manifest = make_scaffold(output, path, result)
    try:
        figures = plot_report(output, t, volts, result)
    except ImportError as exc:
        result["plot_warning"] = f"Plot dependency missing: {exc}"
        figures = []
    report_markdown(result, output, manifest, figures, root=root, record=record)
    print(f"[PULSE REPORT {idx}] {output / 'report.md'}", flush=True)
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--record", type=int,
                   help="Specific scope record index; default: every record")
    p.add_argument("--overwrite-unapproved", action="store_true",
                   help="Archive and regenerate ONLY an unedited, unsigned "
                        "review; refuses approved evidence or operator edits")
    args = p.parse_args(argv)
    root = args.run_dir.expanduser().resolve()
    meta = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    if meta.get("plan", {}).get("integrated_experiment") != "MATRIX32_MCU_NANO_SCOPE_V1":
        p.error("Not a Matrix32 integrated recording")
    selected = [
        x for x in meta["records"]
        if args.record is None or int(x["index"]) == args.record
    ]
    if not selected:
        p.error("No matching records")
    for record in selected:
        result = analyze_record(root, record,
                                overwrite_unapproved=args.overwrite_unapproved)
        print(f"[PULSE REVIEW {result['record_index']}] "
              f"{len(result['accepted_pulses'])} biphasic pulse candidates, "
              f"{len(result['rejected'])} rejected, "
              f"{len(result['suppressed'])} suppressed; "
          f"{result.get('negative_only_rejected_count', 0)} negative-only; "
              f"flags={result['flags']}")
    print("No event list has been manually approved: frequency claims remain provisional.")


if __name__ == "__main__":
    main()
