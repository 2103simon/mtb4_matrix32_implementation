"""Compatibility entry point; use experiment_analysis.analyze_command_timing for new work."""
from experiment_analysis import analyze_command_timing as _implementation
for _symbol, _value in vars(_implementation).items():
    if not _symbol.startswith("__"):
        globals()[_symbol] = _value
if __name__ == "__main__":
    main()
