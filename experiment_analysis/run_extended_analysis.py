"""Run all three independent extended Matrix32 audits and final report.

  python -m experiment_analysis.run_extended_analysis --run-dir data/recordings/RUN

Requires the signed biphasic CH1 review; no hardware, pulse redetection,
review edits, firmware flashing, or recalibration is performed.
"""
from __future__ import annotations
import argparse
from pathlib import Path
from experiment_analysis.paths import use_results_root

from experiment_analysis import analyze_frequency_transitions
from experiment_analysis import analyze_stimulation_shutdown
from experiment_analysis import analyze_command_timing
from experiment_analysis import build_final_experiment_report


def run(run_dir):
    analyze_frequency_transitions.run(run_dir)
    analyze_stimulation_shutdown.run(run_dir)
    analyze_command_timing.run(run_dir)
    return build_final_experiment_report.build(run_dir)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir',required=True,type=Path)
    p.add_argument('--results-root',type=Path)
    args=p.parse_args(argv)
    with use_results_root(args.results_root):
        return run(args.run_dir)

if __name__=='__main__':
    main()
