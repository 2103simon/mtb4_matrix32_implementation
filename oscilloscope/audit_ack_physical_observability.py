"""Backward-compatible entry point; implementation lives in experiment_analysis.audit_ack_physical_observability."""
from experiment_analysis import audit_ack_physical_observability as _implementation
for _symbol, _value in vars(_implementation).items():
    if not _symbol.startswith("__"):
        globals()[_symbol] = _value
if __name__ == "__main__":
    main()
