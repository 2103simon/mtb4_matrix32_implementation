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
