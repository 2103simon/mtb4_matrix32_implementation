"""Publish a small, self-contained and versionable scientific report snapshot.

Only the authoritative report, original technical report, and figures
referenced by those reports are copied. Raw waveforms, signed decisions,
CSV/JSON analysis tables and MCU/BLE logs are NEVER copied into Git.
Other local evidence links are converted to explicit plain-text references.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile

from experiment_analysis.paths import analysis_root, raw_run_dir, sha256, REPO_ROOT

IMAGE_RE = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')
LINK_RE = re.compile(r'(?<!!)\[([^\]]+)\]\(([^)]+)\)')
TAG_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$')
REPORTS = ('final_experiment_report.md', 'extended_analysis_technical_report.md')
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PUBLICATION_BYTES = 35 * 1024 * 1024


def _safe_relative(source_root, link):
    if re.match(r'^[a-z]+://', link, re.I) or link.startswith('#'):
        return None
    if '?' in link or '#' in link:
        raise ValueError(f"Figure link with query/fragment unsupported: {link}")
    if Path(link).is_absolute():
        raise ValueError(f"Absolute local publication path forbidden: {link}")
    candidate = (source_root / link).resolve()
    if candidate == source_root or source_root not in candidate.parents:
        raise ValueError(f"Figure leaves analysis root: {link}")
    return candidate.relative_to(source_root)


def _clean_links(text, root, run_id):
    def render_link(match):
        label, uri = match.groups()
        if uri.startswith(('http://','https://','#','mailto:')):
            return match.group(0)
        local = _safe_relative(root, uri)
        if local is None:
            return match.group(0)
        return f"{label} (local supporting evidence: \`results/{run_id}/analysis/{local.as_posix()}\`)"
    return LINK_RE.sub(render_link, text)


def publish(run_dir, *, reports_root=None, tag=None, analysis_path=None):
    raw = raw_run_dir(run_dir)
    source = Path(analysis_path).resolve() if analysis_path else analysis_root(raw, legacy_ok=False)
    if not source.is_dir():
        raise FileNotFoundError("Migrate and generate results before publishing")
    result_root = source.parent
    manifest_path = result_root / "recording_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError("Verified recording_manifest.json required: migrate first")
    binding = json.loads(manifest_path.read_text(encoding="utf-8"))
    if binding.get('run_id') != raw.name or binding.get('metadata_sha256') != sha256(raw / 'metadata.json'):
        raise ValueError("Published results do not match source recording")
    target_root = (Path(reports_root).expanduser().resolve()
                   if reports_root else REPO_ROOT / 'reports')
    if target_root == raw or raw in target_root.parents:
        raise ValueError('Published reports must not be inside raw data')
    tag = tag or datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    if not TAG_RE.fullmatch(tag):
        raise ValueError('Snapshot tag must be alphanumeric, ., _ or -')
    target = target_root / raw.name / tag
    if target.exists():
        raise FileExistsError(f"Publication already exists: {target}. Use a new --tag.")
    markdown = {}
    figures = {}
    total = 0
    for name in REPORTS:
        path = source / name
        if not path.is_file():
            raise FileNotFoundError(f"Required scientific/technical report absent: {path}")
        original = path.read_text(encoding="utf-8")
        for match in IMAGE_RE.finditer(original):
            rel = _safe_relative(source, match.group(2))
            if rel is None:
                raise ValueError(f"External figure link cannot be archived: {match.group(2)}")
            item = source / rel
            if not item.is_file() or item.is_symlink():
                raise FileNotFoundError(f"Missing or linked publication figure: {item}")
            if item.suffix.lower() not in ('.png','.jpg','.jpeg','.svg','.webp'):
                raise ValueError(f"Unexpected figure extension: {item}")
            if item.stat().st_size > MAX_IMAGE_BYTES:
                raise ValueError(f"Figure exceeds 8 MiB Git-publication limit: {item}")
            figures[rel.as_posix()] = item
        text = _clean_links(original, source, raw.name)
        # The original generated scientific report remains in results/.
        # Only local resource links are rendered as readable provenance paths.
        preface = (f"<!-- Source: results/{raw.name}/analysis/{name}. "
                   "Published snapshot; scientific numbers unmodified. -->\n\n")
        markdown[name] = (preface + text, sha256(path))
        total += len(markdown[name][0].encode('utf-8'))
    total += sum(path.stat().st_size for path in figures.values())
    if total > MAX_PUBLICATION_BYTES:
        raise ValueError('Publication exceeds 35 MiB; keep heavy plots in results instead')
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f'.publish-{tag}-', dir=target.parent))
    try:
        items = {}
        for name, (text, source_digest) in markdown.items():
            (stage / name).write_text(text, encoding='utf-8')
            items[name] = dict(source_sha256=source_digest,
                               published_sha256=sha256(stage/name),
                               bytes=(stage/name).stat().st_size)
        for name, src in sorted(figures.items()):
            dest = stage / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            if sha256(dest) != sha256(src):
                raise ValueError(f'Figure publication verification failed: {name}')
            items[name] = dict(source_sha256=sha256(src),
                               published_sha256=sha256(dest),
                               bytes=dest.stat().st_size)
        (stage/'README.md').write_text(
            f"# Matrix32 experiment: {raw.name}\n\n"
            f"Published report snapshot **{tag}**.\n\n"
            "- [Scientific conclusions](final_experiment_report.md)\n"
            "- [Detailed technical consolidation](extended_analysis_technical_report.md)\n\n"
            "All referenced figures are bundled. Links to raw evidence and "
            "generated statistical tables are deliberately descriptive rather "
            "than live, because those large or signed resources remain outside "
            "Git under the corresponding results directory. This publication "
            "does not contain raw samples or replace the archived experiment.\n",
            encoding='utf-8')
        publication = {
            'schema':'matrix32_publication_snapshot_v1',
            'run_id':raw.name, 'tag':tag,
            'metadata_sha256':binding['metadata_sha256'],
            'binding_manifest_sha256':sha256(manifest_path),
            'files':items,
            'source_analysis_at_publication':str(source),
            'raw_data_included':False,
            'signed_review_included':False,
        }
        (stage/'publication_manifest.json').write_text(
            json.dumps(publication,indent=2,sort_keys=True)+'\n',encoding='utf-8')
        stage.rename(target)
    except BaseException:
        shutil.rmtree(stage)
        raise
    return target


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir',required=True,type=Path)
    parser.add_argument('--reports-root',type=Path)
    parser.add_argument('--tag')
    args=parser.parse_args(argv)
    dest=publish(args.run_dir,reports_root=args.reports_root,tag=args.tag)
    print(f"[PUBLISHED] {dest}")
    print("Review the snapshot, then git add reports/ and commit/push explicitly.")


if __name__=='__main__':
    main()
