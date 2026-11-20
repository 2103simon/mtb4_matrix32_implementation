"""Offline Nano-to-PC clock diagnostics and provisional timeline composition.

Legacy run: passive FRAME arrival drift estimate only (no offset).
New SYNC-enabled run: four-timestamp host monotonic mapping and optional
per-record scope->Nano->host composition. Does not modify waveform CSVs.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from communication.clock_estimator_diagnostics import diagnose_active_clock
from communication.clock_sync import (
    active_clock_fit, nano_us_to_host_ns, passive_diagnostics,
    read_active_exchanges,
)


def analyze_run(run_dir):
    run_dir = Path(run_dir)
    serial_log = run_dir / "nano_serial.jsonl"
    result = {
        "schema_version": 1,
        "source": str(serial_log),
        "time_domain": "PC monotonic ns, not UTC",
        "passive": passive_diagnostics(serial_log),
        "active": None,
        "scope_records": [],
    }
    exchanges = read_active_exchanges(serial_log)
    result["sync_exchange_count"] = len(exchanges)
    if not exchanges:
        result["status"] = "passive_drift_only"
        return result
    try:
        fit = active_clock_fit(exchanges)
    except ValueError as exc:
        result["status"] = "active_fit_not_qualified"
        result["active_error"] = str(exc)
        return result
    result["active"] = fit
    try:
        result["estimator_diagnostics"] = diagnose_active_clock(exchanges)
    except ValueError as exc:
        result["estimator_diagnostics"] = {
            "status": "not_qualified", "reason": str(exc),
        }
    result["status"] = "provisional_active_mapping"
    align_path = run_dir / "gray_alignment.json"
    if align_path.exists():
        alignment = json.loads(align_path.read_text(encoding="utf-8"))
        if alignment.get("experiment") != "GRAY_TC_V1":
            raise ValueError("Unrecognized Gray scope alignment")
        for record in alignment["records"]:
            t0 = nano_us_to_host_ns(record["nano_time_at_first_sample_s"] * 1e6, fit)
            t1 = nano_us_to_host_ns(record["nano_time_at_last_sample_s"] * 1e6, fit)
            result["scope_records"].append({
                "record_index": record["record_index"],
                "first_sample_host_monotonic_ns_est": round(t0),
                "last_sample_host_monotonic_ns_est": round(t1),
                "scope_edge_fit_rms_us": record["clock_fit"]["edge_fit_rms_us"],
                "nano_host_fit_rms_us": fit["rms_residual_us"],
                "warning": (
                    "Provisional mapping. Residuals are not a valid combined "
                    "total-uncertainty bound; USB delay asymmetry and MCU "
                    "physical-event latency remain uncalibrated."
                ),
            })
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_dir", type=Path)
    p.add_argument("--output", type=Path)
    args = p.parse_args(argv)
    result = analyze_run(args.run_dir)
    out = args.output or args.run_dir / "nano_host_clock.json"
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"[CLOCK] {result['status']}; saved {out}")
    print(f"[PASSIVE] {result['passive']['sample_count']} frames, "
          f"drift {result['passive']['drift_ppm_nano_to_host']:.1f} ppm "
          "(offset uncalibrated)")
    if result["active"]:
        active = result["active"]
        print(f"[ACTIVE] {active['sample_count']} exchanges; drift "
              f"{active['drift_ppm_nano_to_host']:.1f} ppm, "
              f"RMS {active['rms_residual_us']:.1f} us, "
              f"best exchange halfwidth "
              f"{active['best_exchange_offset_halfwidth_us']:.1f} us")
        diagnostics = result.get("estimator_diagnostics", {})
        if diagnostics.get("status") == "diagnostic_only_no_production_fit_replacement":
            print(f"[ESTIMATOR] Stratified low-delay drift "
                  f"{diagnostics['stratified_low_latency']['drift_ppm_nano_to_host']:.1f} ppm; "
                  f"sensitivity {diagnostics['absolute_slope_sensitivity_ppm']:.1f} ppm")
            spread = diagnostics["window_drift_range_ppm"]
            print(f"[ESTIMATOR] 3-window drift range "
                  f"{spread:.1f} ppm" if spread is not None
                  else "[ESTIMATOR] Windows not qualified")
            all_constraint = diagnostics["offset_constraints"][
                "all_exchanges_conditional_on_baseline_slope"
            ]
            print(f"[ESTIMATOR] Offset constraints compatible: "
                  f"{all_constraint['feasible']}; "
                  f"{len(diagnostics['warnings'])} warnings")
            held = diagnostics["blocked_holdout_validation"]
            rms = held["worst_holdout_midpoint_rms_us"]
            bias = held["max_abs_holdout_midpoint_bias_us"]
            if rms is not None and bias is not None:
                print(f"[ESTIMATOR] Holdout: {held['qualified_fold_count']}/3 "
                      f"qualified, worst midpoint RMS {rms:.1f} us, "
                      f"worst absolute bias {bias:.1f} us")
    elif result.get("active_error"):
        print(f"[ACTIVE] Not qualified: {result['active_error']}")
    print(f"[SCOPE] {len(result['scope_records'])} provisional host-time records")


if __name__ == "__main__":
    main()
