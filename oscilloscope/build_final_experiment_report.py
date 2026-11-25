"""Compatibility entry point; use experiment_analysis.build_final_experiment_report for new work."""
from experiment_analysis import build_final_experiment_report as _implementation
for _symbol, _value in vars(_implementation).items():
    if not _symbol.startswith("__"):
        globals()[_symbol] = _value
if __name__ == "__main__":
    main()
