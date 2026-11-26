# Repository analysis and data-handling rules

These rules apply to human and automated contributors working on experimental
recordings, timing diagnostics, frequency analysis and reports.

## Recording integrity and disk use

- Treat `data/recordings/<run>/` as immutable after acquisition is finalized.
  Read waveforms, serial logs, metadata and capture-time clock evidence in
  place. Never copy raw recordings into analysis folders or reports.
- Write derived outputs to `results/<run>/analysis/` using the shared path
  helpers in `experiment_analysis/paths.py`. Keep final, reviewed reports
  under `reports/`. Do not commit raw data or routine generated results.
- Reuse existing derived evidence when its source identity, configuration
  and algorithm version match. Read only required channels and files;
  avoid unnecessary full-file reads, duplicate CSVs and redundant plots.
- Preserve human-approved pulse reviews and original clock evidence.
  Never overwrite them to make a new result pass. Use separate, clearly
  named output folders for scientifically distinct diagnostic analyses.
- Do not retain redundant intermediate versions by default. Before deleting
  older analysis, check dependencies and preserve signed approvals, provenance
  and reproducible evidence; do not automatically delete user data.
- Legacy-layout migration is an explicit, exceptional copy-and-verify of
  analysis files only, not of raw recordings. Use the existing migration
  utility rather than ad hoc copying. Never bypass integrity checks.

## Shared code and reproducibility

- Prefer importing and extending existing functions over duplicating
  algorithms. In particular, share Gray decoding and matching, clock
  prediction, cross-validation, USB uncertainty, pulse-review loading,
  command-state reconstruction and interval classification.
- Keep analysis entry points thin: orchestrate shared functions, provide
  configuration, and generate reports. Factor reusable behavior into
  existing appropriate modules; do not fork similar analyses with subtly
  different defaults or acceptance rules.
- Add synthetic no-hardware tests for new algorithms and regression tests
  for changed behavior. Preserve old defaults unless a change is explicitly
  justified and reviewed.
- Document inputs, units, clock domains, thresholds, configuration and
  assumptions; preserve source provenance and report coverage/exclusions.
  Compare methods on identical evidence. Do not select a clock model by
  maximizing agreement with Matrix32 frequencies.
- Keep physical scope-clock pulse periods separate from cross-clock event
  attribution. No extrapolation outside valid timing anchors without
  explicit justification. Unknown USB asymmetry, model errors and
  unverified physical command application must remain visible in conclusions.
- Do not revise approved reports or promote diagnostic methods to
  calibrated production mappings until evidence and limitations have been
  reviewed. Bench acquisitions require local operator verification and
  the existing dummy-load safety procedures.

