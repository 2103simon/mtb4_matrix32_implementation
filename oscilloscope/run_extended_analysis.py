"""Run all three independent extended Matrix32 audits and final report.

  python -m oscilloscope.run_extended_analysis --run-dir data/recordings/RUN

Requires the signed biphasic CH1 review; no hardware, pulse redetection,
review edits, firmware flashing, or recalibration is performed.
"""
from __future__ import annotations
import argparse
from pathlib import Path

from oscilloscope import analyze_frequency_transitions
from oscilloscope import analyze_stimulation_shutdown
from oscilloscope import analyze_command_timing
from oscilloscope import build_final_experiment_report


def run(run_dir):
    analyze_frequency_transitions.run(run_dir)
    analyze_stimulation_shutdown.run(run_dir)
    analyze_command_timing.run(run_dir)
    return build_final_experiment_report.build(run_dir)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir',required=True,type=Path)
    args=p.parse_args(argv)
    return run(args.run_dir)

if __name__=='__main__':
    main()