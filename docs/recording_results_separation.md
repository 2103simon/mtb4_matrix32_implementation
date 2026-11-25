# Separate recording evidence, analysis results and published reports

## Why

A raw recording is an immutable experimental source, not a workspace for plots.
Its `metadata.json`, CH1/CH2 waveform CSVs, MCU/BLE logs, Nano logs, clock
evidence, and original recorded settings stay together in
`data/recordings/<RUN_ID>/`.

The **operator-signed** pulse review is not raw acquisition data but is
source-locked, valuable scientific evidence. It is copied unchanged into
`results/<RUN_ID>/analysis/record_00001/pulse_review/`, alongside all derived
analyses, figures and reports. Existing relative Markdown links are preserved
by retaining the same internal `analysis/` structure.

For curated scientific results intended to remain in Git, explicitly publish
a small **immutable snapshot** under `reports/<RUN_ID>/<TAG>/`.

```text
data/recordings/<RUN_ID>/             # raw; Git ignored
results/<RUN_ID>/recording_manifest.json
results/<RUN_ID>/analysis/            # signed review + generated work; Git ignored
reports/<RUN_ID>/<TAG>/               # curated reports + referenced images; Git tracked
experiment_analysis/                  # offline analysis code
oscilloscope/                         # acquisition, scope-specific review and hardware
```

## Safety / disk-space policy

On the reference laptop, `df -h .` reported **25 GB available (95% used)**.
The migration **does not copy waveform data**, only the old `analysis/` tree.
Use a separate external backup for irreplaceable raw recordings before cleanup.
The `--apply` command never deletes or edits files under the old recording.
It hashes every raw file and every migrated analysis file. Do not remove the
old `analysis/` copy until the results are verified **and** an independent
backup is available.

The raw files are not made read-only automatically; other tooling may need to
read/revisit them. For important completed recordings, consider a filesystem
read-only backup after migration. Permissions alone do not replace backups.

## Migrate the approved 20-second recording

From the repository root on `refactor/separate-recordings-results`:

```bash
RUN_ID="20261124T114542660847Z_integrated_hw_10M_20s_resume_check"
MTB4_RUN="data/recordings/$RUN_ID"

# Examine what will be copied (zero writes):
python -m experiment_analysis.migrate_layout --run-dir "$MTB4_RUN"

# Copy ONLY its analysis/ directory to results/<RUN_ID>/analysis/.
# Verify hashes and record the raw-source hash manifest.
python -m experiment_analysis.migrate_layout --run-dir "$MTB4_RUN" --apply

# Read-only integrity recheck, including all raw files and signed review CSVs:
python -m experiment_analysis.migrate_layout --run-dir "$MTB4_RUN" --verify

# Do not delete any original content yet.
ls "results/$RUN_ID/analysis/record_00001/pulse_review/review_approval.json"
```

The migration fails if the destination already exists, rather than merging or
overwriting an earlier analysis. The manifest records raw and copied-analysis
SHA-256 hashes. The source-locked pulse review is copied byte-for-byte; no
redetection or manual reapproval is required. Current raw acquisition files
stay at their original paths.

## If migration reports "destination already exists"

This is a **fail-closed condition**, not permission to delete the destination.
The results tree may contain an earlier complete migration, recorder-generated
figures, or a separate raw-recording registration manifest.

1. Inspect `results/<RUN_ID>/recording_manifest.json` and
   `results/<RUN_ID>/analysis/` without modifying them.
2. If the manifest says `signed_review_was_copied_not_regenerated: true`
   and the signed files are in the new tree, run `migrate_layout --verify`.
   A successful verification means the migration need not be repeated.
3. If the manifest is absent or represents registration only, **preserve**
   the existing results directory by renaming it to a unique
   `results/<RUN_ID>.pre_migration_<timestamp>/`, then rerun `--apply`
   and `--verify`. This archives existing files without duplicating
   large raw waveforms or deleting signed reviews.
4. If verification raises an integrity or source-hash error, stop and
   investigate; do not overwrite either directory or regenerate approvals.

A registration-only manifest cannot substitute for a copied signed pulse
review; both the resolver and verification command now reject that case.

## Future clean recordings

New sessions written by `oscilloscope.record_integrated_matrix32` contain
only raw data and metadata in the recording directory. Their optional quick
plot is written into the external `results/` tree. The normal
`experiment_analysis.run_extended_analysis` command automatically registers
their raw SHA-256 inventory before analyzing them. A stand-alone setup command
is also available:

```bash
python -m experiment_analysis.register_recording --run-dir "$MTB4_RUN"
```

Registration never bypasses legacy human reviews: if the recording still has
an embedded `analysis/` directory, use the verified migration instead.
Signed pulse review happens separately and its approval must exist in the
external results directory before the extended scientific analyses can run.

## Analyze and publish

```bash
# Default output: results/<RUN_ID>/analysis/
python -u -m experiment_analysis.run_extended_analysis --run-dir "$MTB4_RUN"

# Optional custom results root:
python -u -m experiment_analysis.run_extended_analysis \
  --run-dir "$MTB4_RUN" --results-root "$(pwd)/results"

# Produce a Git-friendly snapshot of the TWO compiled reports plus their
# embedded figures. No CSV/waveforms/logs or signed approvals are included.
python -m experiment_analysis.publish_reports \
  --run-dir "$MTB4_RUN" --tag first-scientific-report

# Inspect it before committing:
du -sh "reports/$RUN_ID/first-scientific-report"
git status --short reports/
git add "reports/$RUN_ID/first-scientific-report"
git commit -m "Publish Matrix32 signed-recording scientific report"
git push origin refactor/separate-recordings-results
```

A different output root can also be selected for every stand-alone analysis:
`MTB4_RESULTS_ROOT=/other/disk/results python -m ... --run-dir "$MTB4_RUN"`.
Do not change the output root between analyses for a single run.

The publisher enforces a **35 MiB snapshot budget**, maximum **8 MiB per
embedded image**, validates every figure's source path, records hashes, and
refuses to replace an existing tag. Local supporting analysis links are
displayed as non-clickable evidence paths in the published copy; the original
editable reports and all their local links remain intact in `results/`.
Published report snapshots preserve the numeric claims and captions.

## Review and protection

The migrated signed pulse review is still checked against the original CH1
source hash before downstream analyses. After copying, scripts prefer
`results/<RUN_ID>/analysis/` and no longer write analyses under the recording.
For the managed `data/recordings/` tree, scripts refuse to continue against
an unmigrated legacy analysis directory rather than silently modifying it.

The old `oscilloscope.run_extended_analysis` invocation remains supported
through a small compatibility wrapper; new work should use
`experiment_analysis.run_extended_analysis`. Scope acquisition remains under
`oscilloscope/record_integrated_matrix32.py` and writes raw evidence only;
its optional post-capture overview plot is directed into `results/`.

## Regression tests

```bash
python -m unittest discover -s tests -p 'test_separate_recording_results.py' -v
python -m unittest discover -s tests -p 'test_extended_matrix32_analysis.py' -v
```

Never commit `results/` or `data/recordings/`; only deliberate snapshots in
`reports/` belong in Git.
