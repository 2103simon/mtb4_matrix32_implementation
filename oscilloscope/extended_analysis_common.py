"""Legacy import/CLI compatibility: implementation in experiment_analysis.extended_analysis_common."""
from pathlib import Path as _Path
import sys as _sys
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from experiment_analysis import extended_analysis_common as _implementation
if __name__ == "__main__":
    _implementation.main()
else:
    _sys.modules[__name__] = _implementation
