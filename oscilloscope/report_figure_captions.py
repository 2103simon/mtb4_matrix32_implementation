"""Compatibility entry point; use experiment_analysis.report_figure_captions for new work."""
from experiment_analysis import report_figure_captions as _implementation
for _symbol, _value in vars(_implementation).items():
    if not _symbol.startswith("__"):
        globals()[_symbol] = _value
if __name__ == "__main__":
    main()
