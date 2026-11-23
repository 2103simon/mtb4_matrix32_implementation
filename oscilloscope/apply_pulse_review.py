"""Apply S02-style, source-locked human decisions to ONE scope record.

The detector output is immutable evidence; this script emits a separate
operator-approved timeline ONLY if its source files still match SHA-256,
all flagged candidates have explicit decisions, and the operator signs off
complete review. This is a pulse-onset inventory, not a safety calibration.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from oscilloscope.pulse_review_workflow import (
    ADDITION_FIELDS, CANDIDATE_FIELDS, SOURCE_TABLES, csv_rows,
    sha256, write_csv,
)
from oscilloscope.record_scope import atomic_json

APPROVED_FIELDS = ("id", "origin", "time_s", "sample_index",
                   "original_status", "review_decision")
PERIOD_FIELDS = ("start_id", "end_id", "start_s", "end_s",
                 "period_s", "period_ms", "inverse_period_hz",
                 "validation_scope")


def verified_approval(output):
    """Return a source-matched signoff or None (never trust a stale artifact)."""
    output = Path(output)
    file = output / "review_approval.json"
    if not file.exists():
        return None
    try:
        approval = json.loads(file.read_text(encoding="utf-8"))
        manifest = json.loads((output / "review_manifest.json").read_text(
            encoding="utf-8"))
        expected = approval.get("source_tables_sha256", {})
        if approval.get("human_signed_off") is not True:
            return None
        if manifest["source_ch1_sha256"] != approval["source_ch1_sha256"]:
            return None
        if manifest["source_tables_sha256"] != expected:
            return None
        for filename in SOURCE_TABLES:
            if sha256(output / filename) != expected[filename]:
                return None
        for filename, hashkey in (
            ("review_manifest.json", "review_manifest_sha256"),
            ("review_decisions.csv", "review_decisions_sha256"),
            ("manual_additions.csv", "manual_additions_sha256"),
            ("approved_onsets.csv", "approved_onsets_sha256"),
            ("approved_intervals.csv", "approved_intervals_sha256"),
        ):
            if sha256(output / filename) != approval[hashkey]:
                return None
        return approval
    except (KeyError, OSError, ValueError, TypeError):
        return None



def load_approved_onsets(output, source_ch1):
    """Fail closed when human source-lock evidence is missing or stale.

    Each returned row is a human-confirmed physical voltage onset within ONE
    scope record, never an instantaneous ACK-to-device-apply time.
    """
    output = Path(output)
    approval = verified_approval(output)
    if approval is None:
        raise ValueError("No valid source-locked pulse review approval")
    if sha256(source_ch1) != approval["source_ch1_sha256"]:
        raise ValueError("Original CH1 waveform differs from approved waveform")
    onsets = csv_rows(output / "approved_onsets.csv")
    if len(onsets) != approval["included_onsets"]:
        raise ValueError("Approved onset inventory count mismatch")
    if any(x["review_decision"] != "INCLUDE" for x in onsets):
        raise ValueError("Unexpected status in validated onset inventory")
    times = [float(x["time_s"]) for x in onsets]
    if any(right <= left for left, right in zip(times, times[1:])):
        raise ValueError("Approved pulse timeline is not strictly increasing")
    return approval, onsets


def apply(output, source_ch1, *, reviewer, confirm_accepted, confirm_complete):
    output, source_ch1 = Path(output), Path(source_ch1)
    reviewer = str(reviewer).strip()
    if not reviewer:
        raise ValueError("Named reviewer required")
    if not confirm_complete:
        raise ValueError("Explicit --confirm-complete required for entire scope record")
    manifest = json.loads((output / "review_manifest.json").read_text(
        encoding="utf-8"))
    if manifest.get("schema") != "matrix32_pulse_review_v1":
        raise ValueError("Unexpected review manifest schema")
    if sha256(source_ch1) != manifest["source_ch1_sha256"]:
        raise ValueError("Original CH1 waveform changed: review invalid")
    for filename, checksum in manifest["source_tables_sha256"].items():
        if sha256(output / filename) != checksum:
            raise ValueError(f"Detector table changed since review: {filename}")

    original = {item["id"]: item for item in manifest["candidates"]}
    decisions = csv_rows(output / "review_decisions.csv")
    seen, approved, unresolved = set(), [], []
    for row in decisions:
        id_ = row["id"]
        if id_ not in original or id_ in seen:
            raise ValueError("Unknown or repeated candidate ID: " + id_)
        seen.add(id_)
        event = original[id_]
        for field in ("kind", "sample_index", "detector_status"):
            if str(row[field]) != str(event[field]):
                raise ValueError(f"Candidate {id_} metadata changed: {field}")
        if float(row["time_s"]) != float(event["time_s"]):
            raise ValueError(f"Candidate {id_} timestamp edited")
        decision = row["decision"].strip().upper()
        if decision not in ("", "INCLUDE", "EXCLUDE", "UNCERTAIN"):
            raise ValueError(f"Unknown review decision for {id_}: {decision}")
        if decision == "" and event["kind"] == "accepted" and confirm_accepted:
            decision = "INCLUDE"
        if decision in ("", "UNCERTAIN"):
            unresolved.append(id_)
        if decision == "INCLUDE":
            approved.append({
                "id": id_, "origin": event["kind"],
                "time_s": float(event["time_s"]),
                "sample_index": int(event["sample_index"]),
                "original_status": event["detector_status"],
                "review_decision": "INCLUDE",
            })
    if seen != set(original):
        raise ValueError(
            "Missing original event rows: "
            + ", ".join(sorted(set(original)-seen)[:10]))
    additions = csv_rows(output / "manual_additions.csv")
    ids = set(original)
    audit = json.loads((output / "audit.json").read_text(encoding="utf-8"))
    for row in additions:
        id_ = row["id"].strip()
        if not id_.startswith("M") or id_ in ids or len(id_) < 2:
            raise ValueError("Manual additions require distinct M... event IDs")
        ids.add(id_)
        t = float(row["time_s"])
        if not math.isfinite(t) or not (audit["capture_start_s"] <= t <= audit["capture_end_s"]):
            raise ValueError("Manual event time outside measured CH1 record")
        decision = row["decision"].strip().upper()
        if decision not in ("INCLUDE", "EXCLUDE"):
            unresolved.append(id_)
        if decision == "INCLUDE":
            if not row["note"].strip():
                raise ValueError("Each manual addition needs a waveform-evidence note")
            approved.append({
                "id": id_, "origin": "manual_missing_detector_event",
                "time_s": t, "sample_index": "",
                "original_status": "MANUALLY_IDENTIFIED",
                "review_decision": "INCLUDE",
            })
    if unresolved:
        raise ValueError(
            f"Review incomplete: {len(unresolved)} unresolved entries "
            f"(first: {', '.join(unresolved[:12])}). "
            "Review flagged R/S candidates individually; accepted P rows "
            "may be globally confirmed using --confirm-accepted."
        )
    if not approved:
        raise ValueError("No included onsets; cannot approve pulse timeline")
    approved.sort(key=lambda row: (row["time_s"], row["id"]))
    for a, b in zip(approved, approved[1:]):
        if b["time_s"] <= a["time_s"]:
            raise ValueError("Duplicate/non-increasing included onset timestamps")
    periods = []
    for a, b in zip(approved, approved[1:]):
        delta = b["time_s"]-a["time_s"]
        periods.append({
            "start_id": a["id"], "end_id": b["id"],
            "start_s": a["time_s"], "end_s": b["time_s"],
            "period_s": delta, "period_ms": delta*1000,
            "inverse_period_hz": 1/delta,
            "validation_scope": "operator_onset_review_not_device_latency_calibration",
        })
    existing = output / "review_approval.json"
    if existing.exists():
        if verified_approval(output) is not None:
            raise FileExistsError(
                "Already approved. Preserve prior signoff; to change decisions, "
                "archive the entire report before creating a new reviewed version.")
        raise ValueError("Stale review approval exists; preserve and audit first")
    write_csv(output / "approved_onsets.csv", approved, APPROVED_FIELDS)
    write_csv(output / "approved_intervals.csv", periods, PERIOD_FIELDS)
    approval = {
        "schema": "source_locked_onset_operator_review_v1",
        "record_index": manifest["record_index"],
        "reviewer": reviewer,
        "review_date_utc": datetime.now(timezone.utc).isoformat(),
        "human_signed_off": True,
        "scope": "complete CH1 onset inventory for this one finite record only",
        "source_ch1_sha256": manifest["source_ch1_sha256"],
        "source_tables_sha256": manifest["source_tables_sha256"],
        "review_manifest_sha256": sha256(output / "review_manifest.json"),
        "review_decisions_sha256": sha256(output / "review_decisions.csv"),
        "manual_additions_sha256": sha256(output / "manual_additions.csv"),
        "approved_onsets_sha256": sha256(output / "approved_onsets.csv"),
        "approved_intervals_sha256": sha256(output / "approved_intervals.csv"),
        "accepted_candidate_approvals_confirmed": bool(confirm_accepted),
        "operator_confirmed_complete_detection": True,
        "included_onsets": len(approved),
        "included_intervals": len(periods),
        "provisional_frequency_attribution": True,
        "physical_output_latency_proven": False,
    }
    atomic_json(existing, approval)
    if verified_approval(output) is None:
        raise RuntimeError("Written approval did not pass source-lock replay")
    return approval


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--record", type=int, required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--confirm-accepted", action="store_true",
                        help="Explicitly approve every unmodified P row")
    parser.add_argument("--confirm-complete", action="store_true",
                        help="Sign off visual verification of the full record")
    args = parser.parse_args(argv)
    root = args.run_dir.expanduser().resolve()
    meta = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    matches = [row for row in meta["records"]
               if int(row["index"]) == args.record]
    if len(matches) != 1:
        parser.error("Exactly one matching record is required")
    output = root / "analysis" / f"record_{args.record:05d}" / "pulse_review"
    result = apply(
        output, root / matches[0]["channels"]["CH1"]["path"],
        reviewer=args.reviewer, confirm_accepted=args.confirm_accepted,
        confirm_complete=args.confirm_complete,
    )
    print(f"APPROVED record {args.record}: {result['included_onsets']} onsets, "
          f"{result['included_intervals']} intervals; source-locked approval saved")


if __name__ == "__main__":
    main()
