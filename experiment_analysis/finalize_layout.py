"""Move an already verified legacy analysis OUT of a raw recording.

This never deletes files. The original tree is archived in results/<run>/
_legacy_source_analysis/ on the same filesystem, after checking original
recording and signed reviews. The migrated working analysis remains intact.
Run dry first; --apply is explicit. No raw waveforms are copied or modified.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

from experiment_analysis.paths import raw_run_dir, results_dir, sha256
from experiment_analysis.migrate_layout import inventory, verify_migration


def finalize(run_dir, *, results_root=None, apply=False):
    verified = verify_migration(run_dir, results_root=results_root)
    raw = raw_run_dir(run_dir)
    dest = results_dir(raw, results_root)
    source = raw / 'analysis'
    archive = dest / '_legacy_source_analysis'
    receipt = dest / 'legacy_analysis_archive_receipt.json'

    if source.is_symlink() or archive.is_symlink():
        raise ValueError('Refusing to archive symlinked analysis directories')
    if not source.exists():
        if archive.is_dir() and receipt.is_file():
            record = json.loads(receipt.read_text(encoding='utf-8'))
            if record.get('run_id') != raw.name:
                raise ValueError('Archived-analysis receipt has the wrong recording ID')
            return dict(status='ALREADY_ARCHIVED', run_id=raw.name,
                        archive=str(archive), source_recording_clean=True,
                        verified=verified)
        raise FileNotFoundError(
            f'No source analysis tree, or archive/receipt missing: {source}; '
            'inspect manually; nothing was changed')
    if not source.is_dir():
        raise ValueError('Original analysis is not a regular directory')
    if archive.exists() or receipt.exists():
        raise FileExistsError(
            f'Refusing to overwrite an existing archive or receipt in {dest}')
    if os.stat(raw).st_dev != os.stat(dest).st_dev:
        raise ValueError(
            'Raw recording and results use different filesystems. '
            'This finalization requires an atomic same-filesystem rename.')
    recorded = json.loads((dest/'recording_manifest.json').read_text(encoding='utf-8'))
    if (not recorded.get('signed_review_was_copied_not_regenerated')
            or not recorded.get('analysis_files_at_migration')):
        raise ValueError('A verified original-analysis copy is required')
    original_inventory = inventory(source)
    # The original signed review must still agree with the migration manifest.
    # Other generated reports may have changed since migration.
    signed_count = 0
    for rel, prior in recorded['analysis_files_at_migration'].items():
        if '/pulse_review/' not in '/' + rel:
            continue
        name = Path(rel).name
        if not (name.endswith('.csv') or
                name in ('review_approval.json', 'review_manifest.json')):
            continue
        row = original_inventory.get(rel)
        if row != prior:
            raise ValueError(
                f'Original human review differs from archived source hash: {rel}')
        signed_count += 1
    dry = dict(
        status='READY_TO_ARCHIVE' if not apply else 'ARCHIVED',
        run_id=raw.name,
        original_analysis_files=len(original_inventory),
        original_analysis_bytes=sum(v['bytes'] for v in original_inventory.values()),
        checked_signed_review_files=signed_count,
        archive=str(archive),
        raw_analysis_path_removed=False if not apply else True,
        raw_unchanged_files=verified['raw_files_unchanged'],
        no_files_deleted=True,
    )
    if not apply:
        return dry

    # POSIX rename is atomic here. Preserve the complete original tree
    # for independent comparison/backup, without copying raw waveforms.
    source.rename(archive)
    try:
        verify_migration(raw, results_root=results_root)
        if source.exists() or not archive.is_dir():
            raise ValueError('Original analysis archive move did not complete')
        payload = dict(dry, status='ARCHIVED', verified_source_hashes=True,
                       archive_was_moved_not_copied=True,
                       original_analysis_files_inventory=original_inventory)
        with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8', dir=dest,
                prefix='.legacy_archive_receipt.', suffix='.tmp',
                delete=False) as handle:
            pending = Path(handle.name)
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write('\n')
        pending.replace(receipt)
    except BaseException:
        # Restore the original layout in the rare event verification or
        # receipt publication fails; never delete the preserved copy.
        if archive.is_dir() and not source.exists():
            archive.rename(source)
        raise
    return dry


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir', required=True, type=Path)
    p.add_argument('--results-root', type=Path)
    p.add_argument('--apply', action='store_true',
                   help='Move original analysis into ignored results backup; never delete')
    args = p.parse_args(argv)
    outcome = finalize(args.run_dir, results_root=args.results_root, apply=args.apply)
    print(json.dumps(outcome, indent=2))
    if args.apply:
        print('The original analysis was MOVED to the results archive. '
              'Keep it until an independent backup is confirmed; no files deleted.')


if __name__ == '__main__':
    main()
