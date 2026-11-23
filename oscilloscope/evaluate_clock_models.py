"""Cross-validate alternative scope-to-Nano clock maps from saved Gray frames.

Offline only. No hardware, detector reruns, review edits, or replacement of
epoch_clock_analysis.json. Reported maps are CANDIDATES, NOT certified.
Test held-out contiguous Gray blocks to distinguish nonlinear drift from
frame-timestamp jitter; preserve the independent USB clock diagnostics.
Usage:
  python -m oscilloscope.evaluate_clock_models --run-dir DATA_DIR
  python -m oscilloscope.evaluate_clock_models --run-dir DATA_DIR --block-s 2
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

import numpy as np

from oscilloscope.align_gray_records import read_nano_frames
from oscilloscope.clock_qualification_diagnostics import _frame_pairs
from oscilloscope.record_scope import atomic_json

MODEL_NAMES = (
    "affine", "quadratic", "cubic",
    "local_linear_0p75s", "local_linear_1p5s", "local_linear_3s",
)


def _xy(pairs):
    x = np.array([p[0] for p in pairs], dtype=float)
    y = np.array([p[1] for p in pairs], dtype=float)
    if len(x) < 12 or np.any(np.diff(x) <= 0) or np.any(np.diff(y) <= 0):
        raise ValueError("Need >=12 strictly increasing scope/Nano Gray pairs")
    return x, y


def predict(model, x_train, y_train, x_query):
    """Numerically stable fitted Nano seconds at requested scope times."""
    x_train, y_train, x_query = (np.asarray(a, dtype=float)
                                for a in (x_train, y_train, x_query))
    if len(x_train) < 5 or not np.all(np.isfinite(y_train)):
        raise ValueError("Insufficient finite training observations")
    origin = float(np.mean(x_train))
    y_origin = float(np.mean(y_train))
    # Fit nano-scope deviation instead of a large unwrapped Nano epoch;
    # the underlying mean clock slope stays close to unity.
    residual = y_train-y_origin-(x_train-origin)
    centered = x_train-origin
    prediction_x = x_query-origin
    if model in ("affine", "quadratic", "cubic"):
        degree = {"affine": 1, "quadratic": 2, "cubic": 3}[model]
        coefficients = np.polynomial.polynomial.polyfit(
            centered, residual, degree)
        delta = np.polynomial.polynomial.polyval(prediction_x, coefficients)
    elif model.startswith("local_linear_"):
        bandwidth = {
            "local_linear_0p75s": .75,
            "local_linear_1p5s": 1.5,
            "local_linear_3s": 3.,
        }[model]
        values = []
        for q in x_query:
            offset = x_train-float(q)
            weights = np.exp(-.5*np.square(offset/bandwidth))
            if np.count_nonzero(weights > 1e-8) < 3:
                raise ValueError("Local fit has insufficient nearby training data")
            design = np.column_stack([np.ones(len(offset)), offset])
            solution = np.linalg.lstsq(
                design*np.sqrt(weights)[:, None],
                residual*np.sqrt(weights), rcond=None)[0]
            values.append(float(solution[0]))
        delta = np.asarray(values)
    else:
        raise ValueError(f"Unknown model {model}")
    return y_origin + prediction_x + delta


def _metrics(error_us):
    e = np.asarray(error_us, dtype=float)
    if not len(e):
        return {"n": 0, "rms_us": None, "median_abs_us": None,
                "p95_abs_us": None, "max_abs_us": None, "bias_us": None}
    absolute = np.abs(e)
    return {
        "n": int(len(e)),
        "rms_us": float(np.sqrt(np.mean(e*e))),
        "median_abs_us": float(np.median(absolute)),
        "p95_abs_us": float(np.percentile(absolute, 95)),
        "max_abs_us": float(np.max(absolute)),
        "bias_us": float(np.mean(e)),
    }


def blocked_holdout(x, y, *, block_s=1., models=MODEL_NAMES):
    """Predict *contiguous, withheld* blocks; never fit on test frames.

    Every interior block is a fold. Endpoint blocks are omitted because they
    require extrapolation, which is a distinct problem. No frames are removed
    as outliers. Model selection on this same CV is exploratory and should
    not be confused with independent final validation.
    """
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    if block_s <= 0 or len(x) != len(y):
        raise ValueError("Invalid block duration or frame arrays")
    base = float(x[0])
    block_index = np.floor((x-base)/block_s).astype(int)
    # Exclude first and last observed blocks: score interpolation only.
    interior = range(int(block_index.min())+1, int(block_index.max()))
    predictions = {model: [] for model in models}
    observations = []
    fold_rows = []
    for block in interior:
        test = block_index == block
        train = ~test
        if np.count_nonzero(test) < 2 or np.count_nonzero(train) < 12:
            continue
        if x[train].min() >= x[test].min() or x[train].max() <= x[test].max():
            continue
        for model in models:
            try:
                predicted = predict(model, x[train], y[train], x[test])
            except (ValueError, np.linalg.LinAlgError):
                continue
            error = (predicted-y[test])*1e6
            predictions[model].extend(error.tolist())
            for t, value in zip(x[test], error):
                fold_rows.append({"scope_s": float(t), "block": int(block),
                                  "model": model, "heldout_error_us": float(value)})
        observations.extend(x[test].tolist())
    metrics = {model: _metrics(predictions[model]) for model in models}
    return metrics, fold_rows, int(len(set(row["block"] for row in fold_rows)))


def model_stability(model, x, y):
    """Assess non-physical clock reversal and local drift-rate variation."""
    grid = np.linspace(float(x[0]), float(x[-1]), 1001)
    predicted = predict(model, x, y, grid)
    slope = np.diff(predicted)/np.diff(grid)
    median = float(np.median(slope))
    range_ppm = float((np.max(slope)-np.min(slope))/median*1e6)
    return {
        "monotonic": bool(np.all(slope > 0)),
        "slope_nano_s_per_scope_s_median": median,
        "slope_ppm_span": range_ppm,
        "max_local_slope_deviation_from_median_ppm": float(
            np.max(np.abs(slope-median))/median*1e6),
    }


def assess_models(x, y, *, block_s=1., gate_us=100.):
    """Sensitivity at 1s and 2s gaps; classify diagnostic evidence only."""
    detail, points, folds = blocked_holdout(x, y, block_s=block_s)
    longer, _, longer_folds = blocked_holdout(x, y, block_s=2*block_s)
    fits = {}
    for name in MODEL_NAMES:
        full = (predict(name, x, y, x)-y)*1e6
        stability = model_stability(name, x, y)
        fits[name] = {
            "all_frame_fit": _metrics(full),
            "holdout_block_s": block_s,
            "holdout": detail[name],
            "holdout_2x_block_s": 2*block_s,
            "holdout_2x": longer[name],
            "clock_rate_stability": stability,
        }
    baseline = fits["affine"]["holdout"]["rms_us"]
    affine_double = fits["affine"]["holdout_2x"]["rms_us"]
    affine_adequate = (
        baseline is not None and affine_double is not None
        and baseline < gate_us and affine_double < gate_us
        and fits["affine"]["clock_rate_stability"]["monotonic"]
    )
    # Predeclared conservative exploratory screen, NOT original qualification.
    # A satisfactory affine model needs no nonlinear replacement.
    viable = [
        name for name in MODEL_NAMES
        if (fits[name]["holdout"]["n"] >= 20
            and fits[name]["holdout_2x"]["n"] >= 15
            and fits[name]["clock_rate_stability"]["monotonic"]
            and fits[name]["clock_rate_stability"][
                "max_local_slope_deviation_from_median_ppm"] < 250
            and fits[name]["holdout"]["rms_us"] < gate_us
            and fits[name]["holdout_2x"]["rms_us"] < gate_us
            and (baseline is None or
                 fits[name]["holdout"]["rms_us"] <= .8*baseline))
    ]
    best = (min(viable, key=lambda name:
                max(fits[name]["holdout"]["rms_us"],
                    fits[name]["holdout_2x"]["rms_us"]))
            if viable and not affine_adequate else None)
    if affine_adequate:
        conclusion = "Affinely modeled Gray time passes both blocked holdouts; a nonlinear replacement is not justified."
    elif best:
        conclusion = ("A nonlinear candidate improves blocked predictions; "
                      "independent frame/GPIO timing verification is still needed.")
    else:
        conclusion = ("No model passes both blocked holdouts; test Nano GPIO "
                      "timestamp fidelity, scope CH2 capture and clock drift.")
    return {
        "models": fits,
        "cv_fold_count": folds, "cv_double_block_fold_count": longer_folds,
        "selection_is_exploratory_only": True,
        "candidate_for_independent_validation": best,
        "affine_predictive_model_adequate": bool(affine_adequate),
        "diagnostic_conclusion": conclusion,
        "can_replace_official_clock_mapping": False,
        "same_cv_used_for_selection": True,
        "recommendation": conclusion,
    }, points


def plot_model_evaluation(path, x, y, evaluation, cv_points, *, index):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axs = plt.subplots(2, 1, figsize=(15, 10), layout="constrained")
    for name in MODEL_NAMES:
        prediction = predict(name, x, y, x)
        if name == "affine" or name == evaluation["candidate_for_independent_validation"]:
            axs[0].plot(x, (prediction-y)*1e6, lw=1.1,
                        label=f"{name} full-fit residual")
    axs[0].axhline(100, color="tab:red", ls="--", alpha=.5)
    axs[0].axhline(-100, color="tab:red", ls="--", alpha=.5)
    axs[0].set(ylabel="Nano minus fitted? prediction - observation (µs)",
               title=f"Record {index}: all-frame fit (training data only)")
    for name in MODEL_NAMES:
        rows = [r for r in cv_points if r["model"] == name]
        if rows:
            axs[1].plot([r["scope_s"] for r in rows],
                        [r["heldout_error_us"] for r in rows],
                        marker=".", markersize=3, lw=.65,
                        label=f"{name}, 1x blocked holdout")
    axs[1].axhline(100, color="tab:red", ls="--", alpha=.5)
    axs[1].axhline(-100, color="tab:red", ls="--", alpha=.5)
    axs[1].set(xlabel="Scope-local Gray frame time (s)",
               ylabel="Withheld prediction error (µs)",
               title="Contiguous time blocks withheld from fit")
    for ax in axs:
        ax.grid(alpha=.2)
        if ax.get_legend_handles_labels()[0]:
            ax.legend(fontsize=8, ncol=2)
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return str(path)


def run(run_dir, *, block_s=1., gate_us=100.):
    """Read source logs; create only new analysis outputs and diagnostics."""
    root = Path(run_dir).expanduser().resolve()
    meta = json.loads((root/"metadata.json").read_text(encoding="utf-8"))
    clock = json.loads((root/"epoch_clock_analysis.json").read_text(encoding="utf-8"))
    serial = read_nano_frames(root/"nano_serial.jsonl")
    originals = {int(r["record_index"]): r for r in clock["records"]}
    reports, overview = [], []
    for record in sorted(meta["records"], key=lambda r: int(r["index"])):
        index = int(record["index"])
        pairs, issue = _frame_pairs(root, record, serial)
        report = {
            "record_index": index, "original_clock_status":
                originals.get(index, {}).get("status", "UNAVAILABLE"),
            "frame_reconstruction_issue": issue,
            "source_gray_frame_count": len(pairs),
            "original_gray_edge_rms_us": originals.get(index, {}).get(
                "scope_edge_fit_rms_us"),
            "usb_midpoint_fit_rms_us": (originals.get(index, {})
                                       .get("fit") or {}).get("rms_residual_us"),
            "usb_rtt_slope_sensitivity_ppm": (
                (originals.get(index, {}).get("diagnostics") or {})
                .get("absolute_slope_sensitivity_ppm")),
            "usb_causal_offset_feasible": (
                (originals.get(index, {}).get("offset_constraint") or {})
                .get("feasible")),
            "usb_causal_offset_intersection_width_us": (
                (originals.get(index, {}).get("offset_constraint") or {})
                .get("intersection_width_us")),
            "original_warnings": originals.get(index, {}).get("warnings", []),
            "original_maps_untouched": True,
        }
        if issue is None:
            x, y = _xy(pairs)
            assessment, cv_points = assess_models(
                x, y, block_s=block_s, gate_us=gate_us)
            report.update(assessment)
            folder = root/"analysis"/f"record_{index:05d}"
            folder.mkdir(parents=True, exist_ok=True)
            with (folder/"clock_model_holdout_predictions.csv").open(
                    "w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=("scope_s", "block",
                                        "model", "heldout_error_us"))
                writer.writeheader()
                writer.writerows(cv_points)
            report["figure"] = plot_model_evaluation(
                folder/"clock_model_cv_comparison.png",
                x, y, assessment, cv_points, index=index)
        reports.append(report)
        overview.append(report)

    analysis = root/"analysis"
    analysis.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "matrix32_cv_clock_model_diagnostics_v1",
        "source": str(root),
        "block_s": block_s,
        "additional_block_s": 2*block_s,
        "gate_us": gate_us,
        "records": reports,
        "auto_recalibration_permitted": False,
        "absolute_host_offset_verified": False,
        "caution": (
            "CV measures interpolation between Gray frames, not independent "
            "electrical timing or Matrix32 SF application. Model selection "
            "uses same CV set, so estimates can be optimistic. Always inspect "
            "USB offset and frame-timestamp implementation before substitution."
        ),
    }
    atomic_json(analysis/"clock_model_cv_summary.json", payload)
    lines = [
        "# Can we recover the existing scope/Nano timing in post-processing?", "",
        "Offline **diagnostic only**: raw waveforms, source-locked approvals, "
        "epoch_clock_analysis.json and the official clock mapping are unchanged.",
        "",
        "Each model is trained excluding contiguous Gray-frame blocks. "
        "Held-out prediction is evaluated with both "
        f"{block_s:g}-s and {2*block_s:g}-s blocks; "
        "the first/last blocks are omitted to avoid extrapolation. "
        "All matched Gray frames remain in the score unless held out.",
        "",
        "| Record | Original | Gray RMS (µs) | Affine withheld RMS | "
        "Best model* | Best withheld RMS (µs) | Best 2×-block RMS (µs) | "
        "USB offset feasibility |",
        "| ---: | --- | ---: | ---: | --- | ---: | ---: | --- |",
    ]
    for r in reports:
        models = r.get("models", {})
        model = r.get("candidate_for_independent_validation")
        affine = models.get("affine", {}).get("holdout", {}).get("rms_us")
        selected = models.get(model, {}) if model else {}
        lines.append(
            f"| {r['record_index']} | {r['original_clock_status']} | "
            f"{r['original_gray_edge_rms_us']} | {affine} | "
            f"{model or ('affine retained' if r.get('affine_predictive_model_adequate') else 'none')} | "
            f"{selected.get('holdout', {}).get('rms_us')} | "
            f"{selected.get('holdout_2x', {}).get('rms_us')} | "
            f"{r['usb_causal_offset_feasible']} |"
        )
    lines += [
        "", "*Best is a candidate selected using the reported withheld-block "
        "scores; it is not independently verified and must not automatically "
        "replace the clock map.", "",
        "## Scientific decisions", "",
        "- Consistent improvement with low withheld-block error and a "
        "monotonic, stable local clock slope suggests a nonlinear mapping "
        "might recover relative timing; confirm on independent evidence.",
        "- Improvement in full-fit residual but NOT withheld prediction "
        "indicates overfitting or unpredictable frame timestamp jitter.",
        "- A smooth residual trend alone does not prove clock drift. It may "
        "reflect Nano scheduling or the relation between the timestamp "
        "record and actual physical GPIO transition.",
        "- Passing USB midpoint/RTT/causal-feasibility checks does not "
        "calibrate absolute host offset or BLE command-application delay.",
        "- Electrical CH1 frequencies from approved negative onsets are "
        "usable regardless of this clock model selection. Command-to-onset "
        "attribution remains provisional until offset is bounded.",
        "",
        "## Per-record figures and model details", "",
    ]
    for r in reports:
        i = r["record_index"]
        lines.extend([f"### Record {i}: {r['original_clock_status']}", ""])
        if r["frame_reconstruction_issue"]:
            lines.append(f"**Input problem:** {r['frame_reconstruction_issue']}")
        else:
            lines.append(f"**Assessment:** {r['diagnostic_conclusion']}")
            for name in MODEL_NAMES:
                model = r["models"][name]
                lines.append(
                    f"- {name}: withheld {block_s:g}s RMS "
                    f"{model['holdout']['rms_us']} µs; "
                    f"withheld {2*block_s:g}s RMS "
                    f"{model['holdout_2x']['rms_us']} µs; "
                    f"slope deviation "
                    f"{model['clock_rate_stability']['max_local_slope_deviation_from_median_ppm']:.1f} ppm."
                )
            lines += ["", f"![Record {i} model comparison]"
                      f"(record_{i:05d}/clock_model_cv_comparison.png)", ""]
        lines.append(
            f"USB midpoint RMS: {r['usb_midpoint_fit_rms_us']} µs; "
            f"RTT slope sensitivity: "
            f"{r['usb_rtt_slope_sensitivity_ppm']} ppm; "
            f"causal offset feasible: {r['usb_causal_offset_feasible']}; "
            "not independently calibrated."
        )
        lines.append("")
    path = analysis/"clock_model_cv_report.md"
    path.write_text("\n".join(lines)+"\n", encoding="utf-8")
    print(f"[CLOCK CV] Wrote {path}")
    for r in reports:
        selected = r.get("candidate_for_independent_validation")
        print(f"[CLOCK CV {r['record_index']}] "
              f"original={r['original_clock_status']}; "
              f"candidate={selected or 'none'}; "
              f"affine heldout RMS="
              f"{r.get('models', {}).get('affine', {}).get('holdout', {}).get('rms_us')} us")
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--block-s", type=float, default=1.)
    parser.add_argument("--gate-us", type=float, default=100.)
    args = parser.parse_args(argv)
    if not (math.isfinite(args.block_s) and args.block_s >= .25 and
            math.isfinite(args.gate_us) and args.gate_us > 0):
        parser.error("block-s must be >=0.25; gate-us must be positive")
    return run(args.run_dir, block_s=args.block_s, gate_us=args.gate_us)


if __name__ == "__main__":
    main()
