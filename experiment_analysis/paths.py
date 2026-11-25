"""Resolve immutable recording inputs and external analysis output locations.

Raw acquisition remains under data/recordings/<run>. Derived work is stored
under results/<run>/analysis. A legacy in-recording analysis tree may still
be read by old tests/scripts until the explicit migration is applied.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import warnings

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS_ROOT = REPO_ROOT / "results"
ENV_RESULTS_ROOT = "MTB4_RESULTS_ROOT"
ENV_ALLOW_LEGACY = "MTB4_ALLOW_LEGACY_LAYOUT"


def results_root(override=None):
    value = override if override is not None else os.environ.get(ENV_RESULTS_ROOT)
    return Path(value).expanduser().resolve() if value else DEFAULT_RESULTS_ROOT.resolve()


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for piece in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(piece)
    return h.hexdigest()


def raw_run_dir(run_dir):
    root = Path(run_dir).expanduser().resolve()
    if not (root / "metadata.json").is_file():
        raise FileNotFoundError(f"Expected raw recording metadata.json under {root}")
    return root


def results_dir(run_dir, override=None):
    root = raw_run_dir(run_dir)
    dest = results_root(override) / root.name
    # Prevent an accidental symbolic link or configurable results root from
    # redirecting analysis writes into the raw recording.
    if dest.resolve() == root or root in dest.resolve().parents:
        raise ValueError("Results destination must be outside the raw recording")
    return dest


def _validate_binding(root, dest):
    manifest = dest / "recording_manifest.json"
    if not manifest.is_file():
        return
    item = json.loads(manifest.read_text(encoding="utf-8"))
    if item.get("run_id") != root.name:
        raise ValueError(f"Results manifest does not identify run {root.name}")
    if (root / "analysis").is_dir() and (
            not item.get("signed_review_was_copied_not_regenerated") or
            not item.get("analysis_files_at_migration")):
        raise ValueError(
            "Original legacy analysis still exists but results manifest "
            "does not record a completed migration; keep all source files, "
            "inspect existing results and migrate safely.")
    recorded_meta = item.get("metadata_sha256")
    if recorded_meta and sha256(root / "metadata.json") != recorded_meta:
        raise ValueError("Raw metadata differs from the results recording manifest")


def analysis_root(run_dir, *, results_root_override=None, legacy_ok=True):
    """Path for derived evidence; never creates or mutates the raw tree.

    A pre-migration legacy analysis directory is accepted only as a
    compatibility bridge. After migration the external path always wins.
    Set MTB4_ALLOW_LEGACY_LAYOUT=0 to refuse old layouts immediately.
    """
    root = raw_run_dir(run_dir)
    dest = results_dir(root, results_root_override)
    target = dest / "analysis"
    _validate_binding(root, dest)
    if target.is_dir():
        return target
    legacy = root / "analysis"
    if legacy.exists():
        if legacy.is_symlink():
            raise ValueError("Refusing a linked analysis directory inside raw data")
        # In the managed repo recordings tree the migration is mandatory,
        # rather than silently continuing to overwrite raw/analysis.
        managed = (REPO_ROOT / "data" / "recordings").resolve()
        is_managed = (root == managed or managed in root.parents)
        override = os.environ.get(ENV_ALLOW_LEGACY)
        allowed = legacy_ok and (
            override == "1" or (not is_managed and override != "0"))
        if not allowed:
            raise FileNotFoundError(
                f"Analysis is still embedded in raw recording: {legacy}. "
                "Run python -m experiment_analysis.migrate_layout --run-dir "
                "RAW_RUN --apply first. Only synthetic/legacy test fixtures "
                "may use MTB4_ALLOW_LEGACY_LAYOUT=1 to opt in.")
        warnings.warn(
            f"Legacy analysis path in raw recording: {legacy}; "
            "migrate it to the separate results tree before further analysis.",
            DeprecationWarning, stacklevel=2)
        return legacy
    return target



def ensure_derived_destination(destination, *raw_directories):
    """Validate a diagnostic output location and create only its parent folder.

    Explicit --output values must not point anywhere inside managed raw data,
    nor into the supplied recording directories (which may live elsewhere).
    """
    out = Path(destination).expanduser().resolve()
    blocked = [(REPO_ROOT / 'data' / 'recordings').resolve()]
    blocked.extend(Path(p).expanduser().resolve() for p in raw_directories)
    if any(out == root or root in out.parents for root in blocked):
        raise ValueError(f'Refusing to write derived output inside raw recordings: {out}')
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def derived_json_path(run_dir, output=None, *, filename):
    """Select external results by default and reject explicit raw-file writes."""
    root = raw_run_dir(run_dir)
    target = Path(output) if output is not None else analysis_root(root) / filename
    return ensure_derived_destination(target, root)


@contextlib.contextmanager
def use_results_root(override=None):
    """Temporarily select a results destination for nested CLI runners."""
    if override is None:
        yield
        return
    previous = os.environ.get(ENV_RESULTS_ROOT)
    os.environ[ENV_RESULTS_ROOT] = str(results_root(override))
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(ENV_RESULTS_ROOT, None)
        else:
            os.environ[ENV_RESULTS_ROOT] = previous
