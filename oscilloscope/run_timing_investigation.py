"""Run the existing timing diagnostics together, without stimulation.

Offline; reads one completed recording and its original clock evidence.
Outputs are isolated from other analyses. No clock-map replacement.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from experiment_analysis.paths import (
    analysis_root, ensure_derived_destination, raw_run_dir, sha256,
)
from oscilloscope import (
    audit_gray_pulse_timing, clock_qualification_diagnostics,
    evaluate_clock_models,
)
from oscilloscope.record_scope import atomic_json


def run(run_dir):
    root = raw_run_dir(run_dir)
    sources = [root / name for name in (
        "metadata.json", "nano_serial.jsonl", "epoch_clock_analysis.json"
    )]
    fingerprints = {p.name: sha256(p) for p in sources}
    destination = ensure_derived_destination(
        analysis_root(root, legacy_ok=False) / "timing_investigation", root
    )
    # Refuse overwrites, including an incomplete previous attempt.
    destination.mkdir(parents=True, exist_ok=False)
    geometry = audit_gray_pulse_timing.run(root, output_dir=destination)
    _, qualification = clock_qualification_diagnostics.diagnose(
        root, output_dir=destination
    )
    models = evaluate_clock_models.run(root, output_dir=destination)
    epoch = json.loads(sources[2].read_text(encoding="utf-8"))
    usb = {int(r["record_index"]): r for r in epoch["records"]}
    lines = [
        "# Timing synchronization investigation", "",
        f"Recording: {root.name}", "",
        "Diagnostic only. No physical frequency analysis or clock-map replacement.",
        "Gray holdouts use contiguous 1 s and 2 s blocks.",
        "USB results are from the original per-record synchronization epochs.",
        "Absolute host offset and physical application latency are not calibrated.", "",
        "## Scope to Nano", "",
        "| Record | Model | Full RMS (us) | 1 s holdout RMS (us) | 2 s holdout RMS (us) | 2 s holdout p95 (us) |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for row in models["records"]:
        for name, model in row.get("models", {}).items():
            lines.append(
                f"| {row['record_index']} | {name} | "
                f"{model['all_frame_fit']['rms_us']} | "
                f"{model['holdout']['rms_us']} | "
                f"{model['holdout_2x']['rms_us']} | "
                f"{model['holdout_2x']['p95_abs_us']} |"
            )
        lines.append("") if not row.get("models") else None
    lines += ["", "## Nano to PC", "",
              "| Record | Status | Midpoint RMS (us) | Transport median (us) | Transport p90 (us) | Slope sensitivity (ppm) | Feasible offset | Offset width (us) |",
              "| --- | --- | ---: | ---: | ---: | ---: | --- | ---: |"]
    for index, row in sorted(usb.items()):
        fit = row.get("fit") or {}
        diag = row.get("diagnostics") or {}
        delay = diag.get("transport_delay_us") or {}
        offset = row.get("offset_constraint") or {}
        lines.append(
            f"| {index} | {row['status']} | {fit.get('rms_residual_us')} | "
            f"{delay.get('median')} | {delay.get('p90')} | "
            f"{diag.get('absolute_slope_sensitivity_ppm')} | "
            f"{offset.get('feasible')} | {offset.get('intersection_width_us')} |"
        )
    lines += ["", "## Record-specific findings", ""]
    for row in models["records"]:
        lines.append(f"- Record {row['record_index']}: "
                     + (row.get("frame_reconstruction_issue")
                        or row.get("diagnostic_conclusion", "Unavailable")))
    for row in qualification:
        lines.append(f"- Record {row['record_index']} failed checks: "
                     + ("; ".join(row["failed_checks"]) or "none"))
    lines += ["", "## Supporting reports", "",
              "- [Physical Gray pulse geometry](gray_pulse_geometry_report.md)",
              "- [Clock qualification and residuals](clock_qualification_report.md)",
              "- [Clock model validation](clock_model_cv_report.md)", "",
              "A lower fit error does not by itself prove clock accuracy.",
              "Compare independently captured records before adopting any candidate."]
    if fingerprints != {p.name: sha256(p) for p in sources}:
        raise RuntimeError("Source clock evidence changed during analysis")
    atomic_json(destination / "timing_investigation_summary.json", {
        "recording": root.name,
        "source_sha256": fingerprints,
        "geometry": geometry,
        "qualification": qualification,
        "clock_models": models,
        "original_usb_epochs": epoch,
        "absolute_host_offset_verified": False,
        "clock_map_updated": False,
    })
    report = destination / "timing_investigation_report.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[TIMING] Report: {report}")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    return run(args.run_dir)


if __name__ == "__main__":
    main()
