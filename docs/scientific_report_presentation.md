# Matrix32 scientific report (authoritative presentation)

The main generated report is `results/<RUN_ID>/analysis/final_experiment_report.md`.
It is intentionally concise (~six figures) and emphasizes the executive
research-question scorecard, measurement windows, physical frequency accuracy,
rapid-transition timing sensitivity, protocol cadence, current-off diagnostics,
and evidence limitations. The full earlier technical consolidation is preserved as
`analysis/extended_analysis_technical_report.md`; the historical
`analysis/final_integrated_report.md` remains untouched if present.

Read [the separation/migration guide](recording_results_separation.md) first. After migration the reports live in `results/<RUN_ID>/analysis/`, not the raw recording folder. For versioned reference copies, publish them to Git via `experiment_analysis.publish_reports`.

## Rebuild from the signed recording

```bash
MTB4_RUN="data/recordings/20261124T114542660847Z_integrated_hw_10M_20s_resume_check"

# Regenerates all detailed analyses and the single authoritative report:
python -u -m experiment_analysis.run_extended_analysis --run-dir "$MTB4_RUN"

# Or regenerate the two consolidated reports without repeating detailed audits:
python -u -m experiment_analysis.build_final_experiment_report --run-dir "$MTB4_RUN"
```

The consolidated builder first verifies signed source evidence and
compatibility across analyses. If the pre-revision `final_experiment_report.md` exists, its technical
consolidation is copied **byte-for-byte** into
`extended_analysis_technical_report.md` on the first rebuild and never
overwritten thereafter. Existing archives named
`final_experiment_report_legacy.md` are **renamed automatically** on the
next rebuild, preserving every byte. If both filenames exist with different
contents, the build stops rather than overwriting either file. If the
prior technical report is absent, the builder generates the original
technical index before archiving it. It then writes the new scientific
report to `final_experiment_report.md`. It creates three presentation-only
figures under `analysis/presentation_figures/`, while reusing exactly three
figures from the approved detailed analyses. Supporting CSV and detailed
reports are never removed.

The signed waveform review, raw CH1/CH2 data, MCU and BLE logs, and clock
fits remain unchanged. No new electrical classification is performed.

The new six-figure narrative requires the pre-existing per-ACK observability
and frequency robustness audits. If either independent report is absent, the
builder retains the older technical report as the main output and a separate
technical archive, and records `SKIPPED_MISSING_OPTIONAL_PRIOR_AUDITS` in the manifest
rather than inventing evidence. Run the missing prior audit, then rebuild.

## Evidence qualifications

- Frequency accuracy is calculated only from the rows already identified as
  `WITHIN_EXPLORATORY_TOLERANCE` in
  `frequency_robustness_all_intervals.csv`, verified against the robustness
  summary; it is **not** a full-session execution rate.
- SF OK ACKs are direct protocol evidence, **not** a guarantee of electrically
  applied SF commands. Individual electrical application counts remain unknown.
- The five host-to-scope shift scenarios are sensitivity illustrations,
  **not calibrated confidence limits**.
- MCU/host logging duration and scope duration have different origins.
  The prompt/cue schedule is not independently verified tactile action.
- SF request intervals include deliberate quiet/muted periods; their dispersion
  is **not** itself BLE jitter. Request-to-ACK time is not electrical latency.
- Voltage across the verified resistive dummy load is not current/charge-balance
  or electrical-safety validation.

## Tests

```bash
python -m unittest discover -s tests -p 'test_scientific_report_presentation.py' -v

# Optional signed-recording regression, if an existing presentation demo is available:
MTB4_SCI_REPORT_RUN="$MTB4_RUN" \
python -m unittest discover -s tests -p 'test_scientific_report_presentation.py' -v
```

Detailed analyses, results and proofs remain available via the linked reports.
The new report is a presentation layer over the existing validated findings.


## November 26 investigation: revised condensed and detailed reports

The 2026-11-25 approved snapshot is immutable. Its unchanged measurement
window, host timing, and voltage morphology sections/figures remain the
source for the revised presentation; historical affine-map metrics are
explicitly labeled, not silently substituted or pooled. The revised
calculation uses the original reference and both replay A/B pairs. Replay
pools exclude the reference run. The new builder checks signed pulse
inventories, original scope/Nano and USB fingerprints, command traces,
replay identity, matching audit counts and the historical reference
provenance. It reuses existing analyses; no waveform copying or hardware.

From the repo root on `analysis/timing-investigation`:

```bash
git pull --ff-only
python -m unittest discover -s tests -v

REF=20261124T114542660847Z_integrated_hw_10M_20s_resume_check
A1=20261126T090848918698Z_ble_ab_baseline
B1=20261126T091212099927Z_ble_ab_shared
A2=20261126T094712943788Z_ble_ab_baseline_20vdiv
B2=20261126T095023427339Z_ble_ab_shared_20vdiv

# Reuse completed diagnostics. Generate ONLY missing outputs.
(
  set -e
  for run in "$REF" "$A1" "$B1" "$A2" "$B2"; do
    raw="data/recordings/$run"
    analysis="results/$run/analysis"
    test -s "$analysis/timing_refinement/timing_refinement_summary.json" ||
      python -m oscilloscope.refine_timing_investigation --run-dir "$raw"
    test -s "$analysis/piecewise_command_analysis_integer/summary.json" ||
      python -m experiment_analysis.run_piecewise_command_analysis --run-dir "$raw"
    test -s "$analysis/refined_ack_observability_boundaries/summary.json" ||
      python -m experiment_analysis.audit_refined_ack_observability --run-dir "$raw"
    test -s "$analysis/cycle_boundary_observability_chronological/summary.json" ||
      python -m experiment_analysis.audit_cycle_boundary_observability --run-dir "$raw"
    test -s "$analysis/restart_observability/summary.json" ||
      python -m experiment_analysis.audit_restart_observability --run-dir "$raw"
  done
  test -s "results/$REF/analysis/command_chain_diagnostic/summary.json" ||
    python -m experiment_analysis.analyze_command_chain --run-dir "data/recordings/$REF"
  python -m experiment_analysis.build_revised_experiment_reports
)
```

The earlier A/B comparison and quiet-threshold charge diagnostics must also
be present (they were generated during the investigation). If absent, use
`experiment_analysis.compare_ble_replays --baseline ... --shared ...`
for each pair and `oscilloscope.analyze_transient_charge --run-dir ...
--resistance-ohm 100000 --quiet-limit-v 2.5` / `--quiet-limit-v 4.0`
for A2 and B2, preserving existing diagnostics. The original reference's
`extended_analysis_manifest.json` and reused original approved figures
must remain available. A stale or partial output is an error to investigate,
not a reason to delete or overwrite signed evidence.

Both reports are generated together into
`results/$REF/analysis/report_revision/`:

- `final_experiment_report.md` (condensed, six figures)
- `extended_analysis_technical_report.md` (full technical consolidation)
- `report_revision_metrics.json` (source-derived calculations and hashes)
- `report_revision_manifest.json` (generated files, retained figures,
  signed evidence and unsigned review status)

The builder refuses an existing revision folder. If another review build
is needed, preserve or explicitly rename the old folder first. It retains
links to existing reference figures rather than copying them into results.

Create a portable **unsigned** review package with the existing publisher:

```bash
python -m experiment_analysis.publish_reports \
  --run-dir "data/recordings/$REF" --revision \
  --reports-root "results/$REF/review_exports" --tag 2026-11-26-review

zip -r matrix32_report_review.zip \
  "results/$REF/review_exports/$REF/2026-11-26-review" \
  "results/$REF/analysis/report_revision/report_revision_metrics.json" \
  "results/$REF/analysis/report_revision/report_revision_manifest.json"
```

The review export normalizes figure paths and bundles only the reports and
their referenced images. The metrics and manifest are included only in the
local review ZIP. No unsigned revision can be published to the tracked
reports directory. After the operator approves both exact reports, publish
a new tag with the explicit `--revision --approved-by "operator"` option,
then review the Git diff and commit the new snapshot on the working branch.
Do not replace the old approved tag. Merge to main only after review.

All revised command observability remains conditional on the availability,
scheduler and timing-envelope models; the chronological extrema do not
prove a joint clock realization or internal electrical execution. No
electrical safety clearance is implied.
