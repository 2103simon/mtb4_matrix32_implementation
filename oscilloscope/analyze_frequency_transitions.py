"""Backward-compatible entry point; implementation lives in experiment_analysis.analyze_frequency_transitions."""
from experiment_analysis import analyze_frequency_transitions as _implementation
for _symbol, _value in vars(_implementation).items():
    if not _symbol.startswith("__"):
        globals()[_symbol] = _value
if __name__ == "__main__":
    main()
