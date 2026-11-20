"""Three 20-second Gray scope records, 60-second deliberate pauses.

Each scope record gets fresh CRC32 Nano SYNC observations immediately
before arming and after acquiring, BEFORE the waveform download.
The Nano generator remains running throughout the experiment.
No Matrix32 stimulation or Feather communication is performed.

Dry run by default; --run explicitly opts in to real hardware.
The scope must support 2 s/div and 1,000,000-point SINGLE SEQUENCE;
the underlying scope code verifies actual SCPI configuration.
"""
import sys
from test_gray_timecode import main as gray_main


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    defaults = [
        "--count", "3",
        "--points", "1000000",
        "--time-div-us", "2000000",
        "--trigger-position-pct", "0",
        "--acquire-timeout", "90",
        "--per-record-epochs",
        "--epoch-sync-count", "30",
        "--pause-between-s", "60",
        "--sync-transport", "binary",
        "--label", "gray_epoch_pause_20s",
    ]
    result = gray_main(defaults + list(argv))
    if result is not None and result.get("status") == "complete":
        # Analyze ONLY after the scope and serial session have fully closed.
        from pathlib import Path
        from analyze_gray_epoch_experiment import analyze
        from record_scope import atomic_json

        root = Path(result["run_dir"])
        print(f"[EPOCH] Offline analysis of {root}", flush=True)
        alignment, report = analyze(root)
        atomic_json(root / "gray_alignment.json", alignment)
        atomic_json(root / "epoch_clock_analysis.json", report)
        print(f"[EPOCH] {report['qualified_provisional_records']} of "
              f"{len(report['records'])} records with provisional local fits",
              flush=True)
        for row in report["records"]:
            fit = row.get("fit")
            metric = (f"{fit['drift_ppm_nano_to_host']:.1f} ppm, "
                      f"RMS {fit['rms_residual_us']:.1f} us"
                      if fit else "no local clock fit")
            print(f"[EPOCH {row['record_index']}] {row['status']}: {metric}",
                  flush=True)
            for warning in row["warnings"]:
                print(f"  [WARN] {warning}", flush=True)
        print(f"[EPOCH] Saved {root / 'epoch_clock_analysis.json'}", flush=True)
    return result


if __name__ == "__main__":
    main()
