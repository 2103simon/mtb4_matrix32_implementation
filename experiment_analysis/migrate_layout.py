"""Copy-and-verify migration from raw/analysis to results/<run>/analysis.

By default this is a dry run. --apply copies existing analysis evidence and
signed reviews byte-for-byte without modifying or deleting any raw files.
This utility NEVER removes source analysis automatically.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import tempfile

from experiment_analysis.paths import raw_run_dir, results_dir, sha256


def _files(folder):
    for base, dirs, files in os.walk(folder, followlinks=False):
        root = Path(base)
        for name in dirs:
            if (root / name).is_symlink():
                raise ValueError(f"Cannot migrate symlinked directory: {root / name}")
        for name in files:
            path = root / name
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Refusing symlink or special file: {path}")
            yield path


def inventory(folder):
    folder = Path(folder)
    if not folder.is_dir():
        return {}
    return {p.relative_to(folder).as_posix():
            {"bytes": p.stat().st_size, "sha256": sha256(p)}
            for p in sorted(_files(folder))}


def _verify_file_copy(source, dest, expected):
    if not dest.is_file() or dest.stat().st_size != expected["bytes"]:
        raise ValueError(f"Copied analysis file missing or size mismatch: {dest}")
    if sha256(dest) != expected["sha256"]:
        raise ValueError(f"Copied analysis file hash mismatch: {dest}")


def migrate(run_dir, *, results_root=None, apply=False):
    raw = raw_run_dir(run_dir)
    dest = results_dir(raw, results_root)
    old = raw / "analysis"
    new = dest / "analysis"
    if old.is_symlink() or new.is_symlink():
        raise ValueError("Migration refuses linked analysis trees")
    if not old.is_dir():
        raise FileNotFoundError(f"No old analysis to migrate: {old}")
    old_files = inventory(old)
    raw_files = {
        p.relative_to(raw).as_posix(): {
            "bytes": p.stat().st_size, "sha256": sha256(p)
        } for p in sorted(_files(raw))
        if p != old and old not in p.parents
    }
    preview = {
        "run_id": raw.name, "raw_dir": str(raw), "results_dir": str(dest),
        "analysis_file_count": len(old_files),
        "analysis_bytes": sum(x["bytes"] for x in old_files.values()),
        "raw_file_count": len(raw_files),
        "raw_bytes": sum(x["bytes"] for x in raw_files.values()),
        "status": "DRY_RUN" if not apply else "VERIFIED_COPY",
    }
    if not apply:
        return preview
    if new.exists() or (dest / "recording_manifest.json").exists():
        raise FileExistsError(
            f"Migration destination already exists; no files overwritten: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{raw.name}.migrating-",
                                   dir=dest.parent))
    try:
        target = stage / "analysis"
        # copytree retains the old signed decisions exactly; no review
        # regeneration, rehashing of source timestamps or redetection.
        shutil.copytree(old, target, symlinks=False)
        copied = inventory(target)
        if copied != old_files:
            raise ValueError("Source and destination analysis inventories differ")
        for rel, data in old_files.items():
            _verify_file_copy(old / rel, target / rel, data)
        # Source raw evidence has remained byte-for-byte identical.
        verify_raw = {
            p.relative_to(raw).as_posix(): {
                "bytes": p.stat().st_size, "sha256": sha256(p)
            } for p in sorted(_files(raw))
            if old not in p.parents
        }
        if verify_raw != raw_files:
            raise ValueError("Recording changed during migration; no results committed")
        manifest = {
            "schema": "matrix32_recording_results_binding_v1",
            "run_id": raw.name,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "raw_dir_at_migration": str(raw),
            "metadata_sha256": sha256(raw / "metadata.json"),
            "raw_files": raw_files,
            "analysis_files_at_migration": old_files,
            "signed_review_was_copied_not_regenerated": True,
            "raw_source_retained_after_migration": True,
        }
        (stage / "recording_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        stage.rename(dest)  # Same-filesystem atomic publication of verified copy.
    except BaseException:
        shutil.rmtree(stage)
        raise
    return preview



def verify_migration(run_dir, *, results_root=None):
    """Check raw-source hashes and copied signed review without touching files."""
    raw = raw_run_dir(run_dir)
    dest = results_dir(raw, results_root)
    manifest_path = dest / 'recording_manifest.json'
    if not manifest_path.is_file():
        raise FileNotFoundError(f'No verified migration manifest: {manifest_path}')
    meta = json.loads(manifest_path.read_text(encoding='utf-8'))
    if meta.get('run_id') != raw.name or meta.get('schema') != 'matrix32_recording_results_binding_v1':
        raise ValueError('Wrong or unsupported recording migration manifest')
    original = raw / 'analysis'
    files = {
        p.relative_to(raw).as_posix(): {
            'bytes': p.stat().st_size, 'sha256': sha256(p)
        } for p in sorted(_files(raw)) if original not in p.parents
    }
    if files != meta['raw_files']:
        raise ValueError('Raw recording has changed since migration')
    if sha256(raw / 'metadata.json') != meta['metadata_sha256']:
        raise ValueError('Recording metadata changed since migration')
    approved = {}
    for rel, expected in meta['analysis_files_at_migration'].items():
        # Reports and plots may be legitimately regenerated. Signed review
        # decisions and all underlying review CSV source tables may not.
        if '/pulse_review/' not in '/' + rel:
            continue
        filename = Path(rel).name
        if filename not in (
            'review_approval.json', 'review_manifest.json',
            'review_decisions.csv', 'manual_additions.csv',
            'approved_onsets.csv', 'approved_intervals.csv',
            'accepted_onsets.csv', 'rejected_onsets.csv',
            'suppressed_onsets.csv', 'candidate_intervals.csv',
        ):
            continue
        target = dest / 'analysis' / rel
        _verify_file_copy(None, target, expected)
        approved[rel] = expected['sha256']
    return {
        'status': 'VERIFIED', 'run_id': raw.name,
        'raw_files_unchanged': len(files),
        'signed_review_source_files_unchanged': len(approved),
        'results_dir': str(dest),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--results-root", type=Path)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--apply", action="store_true",
                        help="Copy and verify analysis; NEVER delete raw data")
    actions.add_argument("--verify", action="store_true",
                        help="Recheck raw files and signed approval against migration hashes")
    args = parser.parse_args(argv)
    result = (verify_migration(args.run_dir, results_root=args.results_root)
              if args.verify else migrate(args.run_dir, results_root=args.results_root,
                                           apply=args.apply))
    print(json.dumps(result, indent=2))
    if args.apply:
        print("Source analysis was intentionally LEFT IN PLACE. "
              "Check the new reports and make an independent backup before "
              "removing the old duplicate manually.")


if __name__ == "__main__":
    main()
