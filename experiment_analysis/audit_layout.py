"""Read-only audit of one migrated recording, Git report snapshots and leftovers.

This command NEVER modifies files, nor approves pulses. Use --scan-all to
list additional recording directories that still contain old analysis trees.
The output distinguishes intentional archival copies from active residuals.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess

from experiment_analysis.paths import REPO_ROOT, raw_run_dir, results_dir, sha256
from experiment_analysis.migrate_layout import verify_migration, inventory

IMAGE_LINK = re.compile(r'!\[[^\]]*\]\(([^)]+)\)')


def _publication_checks(raw, publication):
    errors = []
    package = publication
    manifest = package / 'publication_manifest.json'
    if not manifest.is_file():
        return [f'Missing publication manifest: {manifest}'], 0
    data = json.loads(manifest.read_text(encoding='utf-8'))
    if data.get('run_id') != raw.name:
        errors.append(f'Publication run ID mismatch: {package}')
    if data.get('metadata_sha256') != sha256(raw / 'metadata.json'):
        errors.append(f'Publication points to different raw metadata: {package}')
    if data.get('raw_data_included') is not False:
        errors.append(f'Publication claims to contain raw data: {package}')
    if data.get('signed_review_included') is not False:
        errors.append(f'Publication claims to contain signed review: {package}')
    files = data.get('files')
    if not isinstance(files, dict) or not files:
        errors.append(f'No publication file inventory: {package}')
        files = {}
    for rel, evidence in files.items():
        p = package / rel
        if p.resolve() == package.resolve() or package.resolve() not in p.resolve().parents:
            errors.append(f'Publication path escapes snapshot: {rel}')
            continue
        if not p.is_file() or p.is_symlink():
            errors.append(f'Published file missing or linked: {p}')
            continue
        if sha256(p) != evidence.get('published_sha256'):
            errors.append(f'Published file hash changed: {p}')
        if p.suffix.lower() in ('.csv', '.jsonl', '.bin', '.npz', '.parquet'):
            errors.append(f'Raw/intermediate data accidentally published: {p}')
    for filename in ('final_experiment_report.md',
                     'extended_analysis_technical_report.md'):
        doc = package / filename
        if not doc.is_file():
            errors.append(f'Missing report document: {doc}')
            continue
        if filename not in files:
            errors.append(f'Report absent from publication manifest: {filename}')
        md = doc.read_text(encoding='utf-8')
        images = IMAGE_LINK.findall(md)
        if not images:
            errors.append(f'Report has no embedded figures: {doc}')
        for src in images:
            local = (package / src).resolve()
            if package.resolve() not in local.parents:
                errors.append(f'Figure escaped snapshot: {src}')
            elif not local.is_file() or src not in files:
                errors.append(f'Referenced figure missing from snapshot/inventory: {src}')
    # Any other content beyond README and publication manifest is accidental.
    actual = {p.relative_to(package).as_posix() for p in package.rglob('*')
              if p.is_file()}
    permitted = set(files) | {'README.md', 'publication_manifest.json'}
    extras = actual - permitted
    if extras:
        errors.append(f'Unmanifested publication files: {sorted(extras)}')
    return errors, len(files)


def audit(run_dir, *, scan_all=False):
    raw = raw_run_dir(run_dir)
    dest = results_dir(raw)
    errors = []
    warnings = []
    verified = None
    try:
        verified = verify_migration(raw)
    except (ValueError, FileNotFoundError, KeyError) as exc:
        errors.append(f'Recording/signed review migration verification failed: {exc}')
    analysis = dest / 'analysis'
    if not analysis.is_dir():
        errors.append(f'External analysis directory missing: {analysis}')
    else:
        for name in ('final_experiment_report.md',
                     'extended_analysis_technical_report.md'):
            if not (analysis / name).is_file():
                errors.append(f'Generated report missing in results: {name}')
        signed = [p for p in analysis.rglob('review_approval.json')
                  if 'pulse_review' in p.parts and p.is_file()]
        if not signed:
            errors.append('No signed pulse-review approval found in external results')
    embedded = raw / 'analysis'
    if embedded.exists():
        errors.append(
            f'Active analysis directory still inside raw recording: {embedded}. '
            'Use finalize_layout --apply after verifying the results.')
    archive = dest / '_legacy_source_analysis'
    receipt = dest / 'legacy_analysis_archive_receipt.json'
    if archive.exists() != receipt.exists():
        errors.append('A legacy analysis archive/receipt exists without its counterpart')
    elif archive.is_dir() and receipt.is_file():
        note = json.loads(receipt.read_text(encoding='utf-8'))
        if note.get('run_id') != raw.name:
            errors.append('Legacy analysis archival receipt identifies wrong run')
        elif inventory(archive) != note.get('original_analysis_files_inventory'):
            errors.append('Preserved pre-migration analysis archive changed since cleanup')
    repo_reports = REPO_ROOT / 'reports' / raw.name
    packages = sorted(p for p in repo_reports.iterdir() if p.is_dir()) if repo_reports.is_dir() else []
    if not packages:
        errors.append(f'No published report snapshots found for {raw.name}')
    total_publication_files = 0
    for package in packages:
        bad, count = _publication_checks(raw, package)
        errors.extend(bad)
        total_publication_files += count
    repo_root = REPO_ROOT
    tracked = subprocess.run(
        ['git', 'ls-files', '--cached'],
        cwd=repo_root, text=True, capture_output=True, check=True,
    ).stdout.splitlines()
    unexpected = [p for p in tracked if p.startswith(('data/', 'results/', 'analysis/'))]
    if unexpected:
        errors.append(f'Generated/raw files tracked by Git: {unexpected[:12]}')
    legacy_repository_analysis = repo_root / 'analysis'
    if legacy_repository_analysis.is_dir():
        leftovers = sorted(p.relative_to(repo_root).as_posix()
                           for p in legacy_repository_analysis.rglob('*')
                           if p.is_file() or p.is_symlink())
        if leftovers:
            errors.append('Untracked old top-level analysis outputs still exist: '
                          + str(leftovers[:12]))
        else:
            warnings.append('Empty top-level analysis/ directory remains; '
                            'you may remove it with rmdir after inspection')
    expected_publications = {
        p.relative_to(repo_root).as_posix()
        for package in packages for p in package.rglob('*') if p.is_file()
    }
    not_staged = sorted(expected_publications - set(tracked))
    if not_staged:
        errors.append(f'Local publication files not Git-tracked: {not_staged[:12]}')
    other_residuals = []
    if scan_all:
        recordings = REPO_ROOT / 'data' / 'recordings'
        if recordings.is_dir():
            other_residuals = sorted(str(d.relative_to(REPO_ROOT))
                for d in recordings.iterdir()
                if d.is_dir() and d != raw and (d / 'analysis').exists())
        if other_residuals:
            warnings.append(
                f'{len(other_residuals)} OTHER historic recordings have analysis/ '
                'trees; archive/migrate each if you intend to clean all recordings.')
    return {
        'status': ('FAIL' if errors else
                   'PASS_WITH_HISTORICAL_RESIDUALS' if other_residuals
                   else 'PASS'),
        'run_id': raw.name,
        'raw_files_verified': None if verified is None else verified['raw_files_unchanged'],
        'signed_review_files_verified': (None if verified is None
                                        else verified['signed_review_source_files_unchanged']),
        'raw_recording_has_analysis_dir': embedded.exists(),
        'preserved_legacy_analysis_archive': archive.is_dir(),
        'published_snapshots_checked': len(packages),
        'published_manifest_files_checked': total_publication_files,
        'historic_recordings_with_embedded_analysis': other_residuals,
        'warnings': warnings,
        'errors': errors,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir', required=True, type=Path)
    p.add_argument('--scan-all', action='store_true',
                   help='Also list older runs with their own embedded analysis')
    p.add_argument('--strict', action='store_true',
                   help='Return exit code 2 when report contains any errors')
    args = p.parse_args(argv)
    report = audit(args.run_dir, scan_all=args.scan_all)
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.strict and report['errors']:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
