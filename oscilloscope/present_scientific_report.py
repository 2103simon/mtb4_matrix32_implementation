"""Compatibility entry point; use experiment_analysis.present_scientific_report for new work."""
from experiment_analysis import present_scientific_report as _implementation
for _symbol, _value in vars(_implementation).items():
    if not _symbol.startswith("__"):
        globals()[_symbol] = _value
if __name__ == "__main__":
    main()
