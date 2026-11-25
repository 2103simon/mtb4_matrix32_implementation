"""Compare two standalone Nano SYNC bench runs (text vs CRC32 binary).

Input: the run directories from test_nano_clock_sync.py.
Reports are diagnostic; best RTT/fit residual are not absolute clock accuracy.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiment_analysis.paths import ensure_derived_destination
from oscilloscope.analyze_nano_clock import analyze_run


def compare(text_run, binary_run):
    reports = {}
    for label, path in (("text", text_run), ("binary", binary_run)):
        path = Path(path)
        metadata = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
        actual = metadata.get("sync_transport", "text")
        if actual != label:
            raise ValueError(
                f"{path}: expected {label!r} transport, metadata reports {actual!r}"
            )
        report = analyze_run(path)
        if report["active"] is None:
            raise ValueError(f"{path}: insufficient active synchronization data")
        fit = report["active"]
        quality = report.get("estimator_diagnostics", {})
        if quality.get("status") != "diagnostic_only_no_production_fit_replacement":
            raise ValueError(f"{path}: estimator diagnostics unavailable: {quality}")
        reports[label] = {
            "run_dir": str(path),
            "sample_count": fit["sample_count"],
            "duration_s": fit["span_s"],
            "drift_ppm_nano_to_host": fit["drift_ppm_nano_to_host"],
            "fit_rms_us": fit["rms_residual_us"],
            "low_latency_drift_ppm": quality["stratified_low_latency"][
                "drift_ppm_nano_to_host"],
            "slope_sensitivity_ppm": quality["absolute_slope_sensitivity_ppm"],
            "window_drift_range_ppm": quality["window_drift_range_ppm"],
            "offset_intersection_feasible": quality["offset_constraints"][
                "all_exchanges_conditional_on_baseline_slope"]["feasible"],
            "estimator_warning_count": len(quality["warnings"]),
            "fit_max_abs_us": fit["max_abs_residual_us"],
            "best_exchange_offset_halfwidth_us":
                fit["best_exchange_offset_halfwidth_us"],
            "rtt_p10_us": fit["latency"]["rtt_us"]["p10"],
            "rtt_median_us": fit["latency"]["rtt_us"]["median"],
            "rtt_p90_us": fit["latency"]["rtt_us"]["p90"],
            "transport_rtt_p10_us": fit["latency"]["transport_rtt_us"]["p10"],
            "transport_rtt_median_us": fit["latency"]["transport_rtt_us"]["median"],
            "nano_processing_median_us":
                fit["latency"]["nano_processing_us"]["median"],
        }
    return {
        "schema_version": 1,
        "status": "transport_comparison_only",
        "baseline": reports["text"],
        "candidate": reports["binary"],
        "difference_binary_minus_text": {
            key: reports["binary"][key] - reports["text"][key]
            for key in (
                "drift_ppm_nano_to_host", "fit_rms_us",
                "low_latency_drift_ppm", "slope_sensitivity_ppm",
                "rtt_p10_us", "rtt_median_us", "rtt_p90_us",
                "transport_rtt_p10_us", "transport_rtt_median_us",
                "best_exchange_offset_halfwidth_us",
                "nano_processing_median_us",
            )
        },
        "caution": (
            "Sequential runs may differ due to USB, OS scheduling and "
            "temperature; this comparison does not prove absolute "
            "clock offset accuracy or symmetric communication latency."
        ),
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--text-run", type=Path, required=True)
    p.add_argument("--binary-run", type=Path, required=True)
    p.add_argument("--output", type=Path)
    args = p.parse_args(argv)
    report = compare(args.text_run, args.binary_run)
    print(f"{'Metric':40s} {'text':>13s} {'binary CRC32':>13s}")
    fields = (
        ("sample_count", "SYNC exchanges"),
        ("drift_ppm_nano_to_host", "Nano->host drift [ppm]"),
        ("low_latency_drift_ppm", "Low-delay drift [ppm]"),
        ("slope_sensitivity_ppm", "Slope sensitivity [ppm]"),
        ("estimator_warning_count", "Estimator warnings"),
        ("rtt_p10_us", "RTT p10 [us]"),
        ("rtt_median_us", "RTT median [us]"),
        ("rtt_p90_us", "RTT p90 [us]"),
        ("nano_processing_median_us", "Nano handler median [us]"),
        ("fit_rms_us", "Fit RMS [us]"),
        ("best_exchange_offset_halfwidth_us", "Best offset halfwidth [us]"),
    )
    for key, label in fields:
        print(f"{label:40s} {report['baseline'][key]:13.2f} "
              f"{report['candidate'][key]:13.2f}")
    for label in ("text", "binary"):
        item = report["baseline"] if label == "text" else report["candidate"]
        print(f"[{label}] common offset interval feasible: "
              f"{item['offset_intersection_feasible']}; "
              f"window drift range: {item['window_drift_range_ppm']} ppm")
    if args.output:
        out = ensure_derived_destination(args.output, args.text_run,
                                         args.binary_run)
        out.write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
        print(f"Saved {out}")
    print("Note: RTT reduction is not proof of sub-millisecond offset accuracy.")


if __name__ == "__main__":
    main()
