"""Register an untouched NEW recording for separate analysis results.

For migrated legacy recordings use migrate_layout.py instead. Writes only
results/<run>/recording_manifest.json; never edits raw files or signed reviews.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from experiment_analysis.paths import raw_run_dir, results_dir, sha256
from experiment_analysis.migrate_layout import _files


def register(run_dir, *, results_root=None):
    raw = raw_run_dir(run_dir)
    metadata = json.loads((raw / 'metadata.json').read_text(encoding='utf-8'))
    if metadata.get('status') != 'complete':
        raise ValueError('Only complete recordings can be registered for final analysis')
    dest = results_dir(raw, results_root)
    manifest = dest / 'recording_manifest.json'
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding='utf-8'))
        if (data.get('run_id') != raw.name or
                data.get('metadata_sha256') != sha256(raw / 'metadata.json')):
            raise ValueError('Existing results manifest does not match raw recording')
        return manifest
    if (raw / 'analysis').exists():
        raise ValueError('This recording has legacy analysis evidence; migrate it instead')
    files = {p.relative_to(raw).as_posix(): {
        'bytes': p.stat().st_size, 'sha256': sha256(p),
    } for p in sorted(_files(raw))}
    dest.mkdir(parents=True, exist_ok=True)
    item = {
        'schema': 'matrix32_recording_results_binding_v1',
        'run_id': raw.name,
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'raw_dir_at_migration': str(raw),
        'metadata_sha256': sha256(raw / 'metadata.json'),
        'raw_files': files,
        'analysis_files_at_migration': {},
        'signed_review_was_copied_not_regenerated': False,
        'raw_source_retained_after_migration': True,
    }
    # Atomic, fail-closed creation. Existing manifest must never be silently
    # overwritten (including another worker registering at the same time).
    payload = json.dumps(item, indent=2, sort_keys=True) + '\n'
    try:
        fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise FileExistsError('Results manifest created concurrently; verify it')
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(payload)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--results-root', type=Path)
    args = parser.parse_args(argv)
    path = register(args.run_dir, results_root=args.results_root)
    print('[REGISTERED] '+str(path))


if __name__ == '__main__':
    main()
