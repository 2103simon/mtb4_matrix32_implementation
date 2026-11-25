"""Backward-compatible entry point; implementation lives in experiment_analysis.extended_analysis_common."""
from experiment_analysis import extended_analysis_common as _implementation
for _symbol, _value in vars(_implementation).items():
    if not _symbol.startswith("__"):
        globals()[_symbol] = _value
if __name__ == "__main__":
    main()
