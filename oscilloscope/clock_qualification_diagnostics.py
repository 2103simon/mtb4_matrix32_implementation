"""Explain per-record Gray/Nano/PC clock qualification from saved evidence.

Offline only. Candidate robust fits are DIAGNOSTIC; never replace the
original time mapping or reclassify unqualified as qualified automatically.
"""
from __future__ import annotations
from experiment_analysis.paths import analysis_root

import json
from pathlib import Path

import numpy as np

from oscilloscope.record_scope import atomic_json


def _residual_fit(x, y, mask=None):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if mask is None:
        mask = np.ones(len(x), dtype=bool)
    xx, yy = x[mask], y[mask]
    if len(xx) < 3 or np.ptp(xx) <= 0:
        return None
    origin = float(np.mean(xx))
    slope, offset_local = np.polyfit(xx-origin, yy, 1)
    intercept = offset_local-slope*origin
    residual_us = (slope*x+intercept-y)*1e6
    return {
        "slope_nano_s_per_scope_s": float(slope),
        "offset_nano_s": float(intercept),
        "residual_us": residual_us,
        "rms_all_us": float(np.sqrt(np.mean(residual_us**2))),
        "rms_inliers_us": float(np.sqrt(np.mean(residual_us[mask]**2))),
    }


def _frame_pairs(root, record, nano_frames):
    matches = []
    for frame in record.get("decoded_gray", {}).get("frames", []):
        short = int(frame["counter_mod_65536"])
        choices = [v for count, v in nano_frames.items()
                   if count % 65536 == short]
        if len(choices) != 1:
            return [], "ambiguous Gray counter match"
        ref = choices[0]
        if int(frame["gray_hex"], 16) != int(ref["gray_hex"], 16):
            return [], "Gray code mismatch against Nano serial log"
        matches.append((float(frame["scope_start_s"]),
                        float(ref["nano_unwrapped_us"])/1e6,
                        int(ref["counter"])))
    if len(matches) < 3:
        return [], "fewer than three matched complete Gray frames"
    return matches, None


def _robust_candidate(x, y):
    """Median-absolute-deviation outlier sensitivity, not a new clock truth."""
    baseline = _residual_fit(x, y)
    if baseline is None:
        return None, None, None
    residual = baseline["residual_us"]
    center = float(np.median(residual))
    mad = float(np.median(np.abs(residual-center)))
    # Floor avoids flagging quantized near-perfect edges. The 4-sigma rule
    # is diagnostic only; no events are discarded from official fit.
    tolerance = max(20., 4.*1.4826*mad)
    inlier = np.abs(residual-center) <= tolerance
    if np.count_nonzero(inlier) < max(3, int(np.ceil(.7*len(x)))):
        return baseline, None, {"status": "too_many_candidate_outliers"}
    candidate = _residual_fit(x, y, inlier)
    return baseline, candidate, {
        "status": "diagnostic_only", "threshold_us": tolerance,
        "inliers": int(np.count_nonzero(inlier)),
        "outliers": int(len(x)-np.count_nonzero(inlier)),
        "inlier_mask": inlier,
    }


def diagnose(root):
    root = Path(root)
    epoch_path = root / "epoch_clock_analysis.json"
    if not epoch_path.exists():
        raise FileNotFoundError(f"Missing original epoch clock analysis: {epoch_path}")
    epoch = json.loads(epoch_path.read_text(encoding="utf-8"))
    metadata = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    nano_path = root / "nano_serial.jsonl"
    from oscilloscope.align_gray_records import read_nano_frames
    nano_frames = read_nano_frames(nano_path)
    record_meta = {int(r["index"]): r for r in metadata["records"]}
    results = []
    figures = []
    for entry in epoch["records"]:
        idx = int(entry["record_index"])
        source = record_meta[idx]
        pairs, problem = _frame_pairs(root, source, nano_frames)
        scope_rms = entry.get("scope_edge_fit_rms_us")
        gate = float(entry.get("scope_edge_fit_rms_threshold_us", 100.))
        fit = entry.get("fit") or {}
        diagnostic = entry.get("diagnostics") or {}
        offset = entry.get("offset_constraint") or {}
        checks = [
            {"name": "Scope/Nano Gray edge RMS",
             "observed": scope_rms, "threshold": gate,
             "pass": scope_rms is not None and scope_rms <= gate,
             "failure": "Gray decoded frame edges do not fit one affine "
                        "scope-to-Nano clock within the adopted residual tolerance."},
            {"name": "Nano/PC midpoint fit RMS",
             "observed": fit.get("rms_residual_us"), "threshold": 1000.,
             "pass": fit.get("rms_residual_us") is not None
                     and fit["rms_residual_us"] <= 1000.,
             "failure": "USB SYNC midpoint residuals are too large."},
            {"name": "RTT-selection slope sensitivity (ppm)",
             "observed": diagnostic.get("absolute_slope_sensitivity_ppm"),
             "threshold": 50.,
             "pass": diagnostic.get("absolute_slope_sensitivity_ppm") is not None
                     and diagnostic["absolute_slope_sensitivity_ppm"] <= 50.,
             "failure": "Estimated Nano-to-host drift changes substantially "
                        "when low-round-trip-time samples are selected."},
            {"name": "Causally feasible common host offset",
             "observed": offset.get("feasible"),
             "threshold": True,
             "pass": offset.get("feasible") is True,
             "failure": "No common causal offset satisfies all USB four-timestamp "
                        "observations at the fitted Nano/PC slope."},
        ]
        baseline, robust, robustness = (None, None, None)
        if not problem:
            x = np.asarray([p[0] for p in pairs])
            y = np.asarray([p[1] for p in pairs])
            baseline, robust, robustness = _robust_candidate(x, y)
        output = analysis_root(root) / f"record_{idx:05d}"
        output.mkdir(parents=True, exist_ok=True)
        plot = output / "clock_residual_diagnostics.png"
        if baseline is not None:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            fig, ax = plt.subplots(figsize=(13, 4.5), layout="constrained")
            ax.scatter(x, baseline["residual_us"], s=14, alpha=.8,
                       label="Official all-frame affine fit residual")
            if robust is not None and robustness is not None:
                mask = robustness["inlier_mask"]
                ax.scatter(x[~mask], baseline["residual_us"][~mask],
                           s=45, marker="x", label="Candidate outliers")
                ax.plot(x, robust["residual_us"], lw=.75, alpha=.7,
                        label="Robust candidate residual (diagnostic only)")
            ax.axhline(gate, color="tab:red", ls="--",
                       label=f"±{gate:g} µs scope fit gate (RMS)")
            ax.axhline(-gate, color="tab:red", ls="--")
            ax.set(xlabel="Scope-local frame start time (s)",
                   ylabel="Residual to Nano Gray time (µs)",
                   title=f"Record {idx}: clock residuals; "
                         f"status {entry['status']}")
            ax.grid(alpha=.2)
            ax.legend(fontsize=8)
            fig.savefig(plot, dpi=140)
            plt.close(fig)
            figures.append(str(plot))
        result = {
            "record_index": idx,
            "original_status": entry["status"],
            "original_warnings": entry.get("warnings", []),
            "checks": checks,
            "failed_checks": [c["name"] for c in checks if not c["pass"]],
            "matched_gray_frames": len(pairs),
            "frame_diagnostic_error": problem,
            "baseline_gray_edge_rms_us": (
                baseline["rms_all_us"] if baseline else None),
            "robust_candidate": (
                {"inliers": robustness["inliers"],
                 "outliers": robustness["outliers"],
                 "threshold_us": robustness["threshold_us"],
                 "inlier_rms_us": robust["rms_inliers_us"],
                 "all_frame_rms_us": robust["rms_all_us"],
                 "slope_nano_s_per_scope_s": robust["slope_nano_s_per_scope_s"]}
                if robust is not None and robustness is not None else None),
            "clock_map_updated": False,
            "cross_clock_alignment_verified": False,
            "plot": str(plot) if baseline else None,
        }
        results.append(result)
    output = analysis_root(root)
    atomic_json(output / "clock_qualification_diagnostics.json", {
        "schema": "matrix32_clock_qualification_diagnostic_v1",
        "records": results,
        "source_epoch_analysis": str(epoch_path),
        "original_clock_maps_untouched": True,
        "diagnostic_candidate_is_not_a_replacement_map": True,
    })
    markdown = [
        "# Scope / Nano / PC clock qualification audit", "",
        "Original clock status is preserved. This report exposes the **actual "
        "qualifier checks** and tests sensitivity to a small number of "
        "mis-detected or shifted Gray frame edges. Alternative fits are "
        "diagnostic only and never replace original clock mappings.",
        "",
        "| Record | Status | Gray RMS (µs) | RMS gate (µs) | "
        "Failed checks | Candidate robust inliers/outliers |",
        "| ---: | --- | ---: | ---: | --- | --- |",
    ]
    for r in results:
        g = next(x for x in r["checks"]
                 if x["name"] == "Scope/Nano Gray edge RMS")
        rb = r["robust_candidate"]
        markdown.append(
            f"| {r['record_index']} | {r['original_status']} | "
            f"{g['observed']} | {g['threshold']} | "
            f"{'; '.join(r['failed_checks']) or 'none'} | "
            f"{str(rb['inliers'])+'/'+str(rb['outliers']) if rb else 'n/a'} |"
        )
    markdown += [
        "", "## Interpretation", "",
        "- **Gray edge-fit RMS above 100 µs** means the decoded CH2 "
        "frame start and Nano-reported frame times are not sufficiently "
        "consistent under the established all-frame affine fit.",
        "- **RTT slope sensitivity** or **causal offset failure** indicates "
        "a separate USB synchronization uncertainty; improving Gray edges "
        "alone cannot solve it.",
        "- The fitted Nano/PC drift (ppm) is descriptive; the qualifier "
        "specifically checks slope sensitivity to RTT selection, not a "
        "simple hard cap on drift magnitude.",
        "- Robust candidate improvement with a few identified outlier "
        "frames suggests checking those raw CH2 Gray edges and the "
        "corresponding Nano serial entries. It does not authorize quietly "
        "discarding frames or promoting status.",
        "- If broad residuals persist, consider trigger/threshold timing "
        "bias, counter-frame matching, Nano firmware scheduling jitter "
        "or frame-start time measurement uncertainty. Diagnose from the "
        "residual plot before changing any acceptance threshold.",
        "- None of these data supplies an independently calibrated "
        "absolute host-clock offset or a physical SF-application latency.",
        "",
    ]
    for r in results:
        i = r["record_index"]
        markdown += [f"## Record {i}: {r['original_status']}", ""]
        for warning in r["original_warnings"]:
            markdown.append(f"- Original warning: {warning}")
        for check in r["checks"]:
            markdown.append(
                f"- **{'PASS' if check['pass'] else 'FAIL'} — "
                f"{check['name']}**: {check['observed']} "
                f"(criterion {check['threshold']})."
            )
        if r["frame_diagnostic_error"]:
            markdown.append(f"- Gray reconstruction: {r['frame_diagnostic_error']}")
        if r["robust_candidate"]:
            rb = r["robust_candidate"]
            markdown.append(
                f"- Outlier sensitivity: {rb['outliers']} of "
                f"{rb['inliers']+rb['outliers']} frames marked candidate "
                f"outliers. Alternative inlier RMS {rb['inlier_rms_us']:.1f} µs, "
                f"all-frame RMS {rb['all_frame_rms_us']:.1f} µs. "
                "NOT a validated replacement fit."
            )
        if r["plot"]:
            markdown += ["", f"![Record {i} Gray residuals]"
                         f"(record_{i:05d}/clock_residual_diagnostics.png)", ""]
    report_path = output / "clock_qualification_report.md"
    report_path.write_text("\n".join(markdown)+"\n", encoding="utf-8")
    return report_path, results
