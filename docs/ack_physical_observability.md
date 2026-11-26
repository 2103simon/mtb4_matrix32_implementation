# Approved Matrix32 SF-ACK vs. physical pulse observability

> **Results-layout note:** Saved captures remain under `data/recordings/`.
> Run derived diagnostics against `results/<RUN_ID>/analysis/`, not inside
> the raw recording. The paths described below may include historical
> pre-refactor examples; use the
> [verified migration and cleanup guide](recording_results_separation.md)
> before reprocessing older data.

Use `oscilloscope/audit_ack_physical_observability.py` to produce a repeatable,
**offline-only** report relating Matrix32 successful SF acknowledgments to
operator-approved biphasic CH1 pulse intervals.

## Prerequisites

- A completed integrated scope recording with `metadata.json`,
  `epoch_clock_analysis.json`, and the continuous MCU and Matrix32 BLE command logs.
- The source-locked, human-signed pulse inventory under
  `analysis/record_00001/pulse_review/` (`review_approval.json`,
  `approved_onsets.csv`, `approved_intervals.csv`).
- For the independent steady-state frequency section, first produce the
  validated clock-model sensitivity analysis and frequency robustness audit.
- Python with NumPy and Matplotlib as used by the existing analysis environment.

The audit **verifies** the approval and source CH1 waveform hash and does not
rerun pulse detection, edit the signed files, reclassify waveform morphology,
command stimulation, or alter the original clock maps.

## Run in the checkout

From the repository root:

```bash
git fetch origin
git switch feature/live-oscilloscope-recording
git pull --ff-only origin feature/live-oscilloscope-recording

MTB4_RUN="data/recordings/20261124T114542660847Z_integrated_hw_10M_20s_resume_check"

python -u -m oscilloscope.audit_ack_physical_observability \
  --run-dir "$MTB4_RUN"
```

For an alternative assumed timing sensitivity margin (milliseconds):

```bash
python -u -m oscilloscope.audit_ack_physical_observability \
  --run-dir "$MTB4_RUN" \
  --margin-ms 3.5 \
  --sensitivity-margins-ms 0.5 1.8 3.5
```

The **1.8 ms default margin is a scenario assumption, not a calibrated
clock-offset bound**. Different scenarios may change the observable ACK
classification. A BLE ACK is protocol completion, not proof that Matrix32
has physically applied that command.

## Saved outputs

The script generates:

- `analysis/ack_physical_observability/final_report.md` — findings, interpretation
  and evidence-linked figures.
- `analysis/ack_physical_observability/sf_ack_observability.csv` — every
  successful SF ACK, including outside-scope ACKs, and its classification.
- `analysis/ack_physical_observability/approved_cycle_ack_context.csv` —
  approved physical periods alongside provisional ACK context.
- `analysis/ack_physical_observability/summary.json` and
  `offset_sensitivity.json` — machine-readable results.
- `analysis/ack_physical_observability/command_physical_timeline.png`,
  `ack_observability_categories.png`, `ack_matched_cycles.png`,
  `offset_sensitivity.png`, and `restart_01_detail.png` /
  `restart_02_detail.png` for the two longest actual onset gaps.
- `analysis/final_integrated_report.md` — entry point linking existing
  integrated/steady-state reports with the per-ACK report.

All these are *derived, recording-specific* results and remain with the
recording under `data/recordings/`; they are not committed to Git.

## Classification interpretation

`STABLE_MATCHING_PERIOD_OBSERVED` means at least one complete, approved
pulse-to-pulse interval was measured after the ACK plus a prior-cycle
settling guard and an assumed timing margin, before another control ACK.
It is **compatibility evidence**, not unique proof of that ACK's execution.

`NEXT_SF_ACK_BEFORE_NEXT_PULSE` is compatible with an intermediate frequency
being superseded. It does **not** prove Matrix32 discarded the internal
command. `ACK_WHILE_CURRENT_MUTED` is not physical frequency evidence;
SF400 in muted-scheduler mode must not be interpreted as 400 Hz output.
Other categories denote unresolved evidence or require separate review.

The physical intervals are measured solely from the approved CH1 negative
onset timestamps of **biphasic-confirmed** pulses. Long pauses are not
interpreted as low-frequency stimulation periods. MCU/ACK alignment is
provisional because the scope–Nano–PC map remains unqualified; no
sub-millisecond physical application-latency or electrical safety inference
is made.

## Tests

Hardware-free tests:

```bash
python -m unittest discover -s tests -p test_ack_physical_observability.py -v
```

To include the signed-data regression for the example recording:

```bash
MTB4_ACK_AUDIT_SAMPLE_RUN="$MTB4_RUN" \
  python -m unittest discover -s tests \
  -p test_ack_physical_observability.py -v
```

The example-data regression checks 418 approved biphasic pulses, 417 intervals,
266 successful SF ACKs (246 provisionally inside the scope window), the
source-locked review's unchanged bytes, and generated figures. It should not
be treated as a universal assertion for other recordings.

## Completeness of the full recording archive

The complete 24 November 2026 recording archive was compared byte for byte
with the consolidated 135-file analysis package: all **135** analysis files
were present and identical. The archive also retained original CH1/CH2
waveforms, MCU/BLE logs, Nano evidence and acquisition metadata.

A historical `pulse_review/report.md` and its archived predecessor contain
a link to `../integrated_overview.png`, which older analysis code did not
produce. This is **one missing derived plot**, not missing raw data or a
missing signed detector/approval file.

The repaired, offline-only figure generator is now included:

```bash
MTB4_RUN="data/recordings/20261124T114542660847Z_integrated_hw_10M_20s_resume_check"

python -u -m oscilloscope.plot_integrated_overview \
  --run-dir "$MTB4_RUN" --record 1
```

The resulting figure is:

`results/<RUN_ID>/analysis/record_00001/integrated_overview.png`

It restores the link in both historical reports without rewriting those
reports or any signed evidence. The figure includes the full original CH1
signal as per-bin **min/max extrema**, approved negative onset markers,
selected-taxel pressure, MCU requested frequency, and approved physical
inverse periods alongside provisionally time-mapped successful SF ACKs.

The generator checks the signed review evidence and hashes the **complete
original CH1 CSV** while scanning it to build the min/max display; memory
use is bounded by the display-bin count. Muted-scheduler SF400 ACKs are
explicitly counted and omitted from the shared physical-frequency axis, not
interpreted as 400-Hz physical stimulation. The cross-clock mapping is
marked **provisional/unqualified** and cannot establish electrical command
application latency.

To run the five hardware-free regression tests:

```bash
python -m unittest discover -s tests -p 'test_plot_integrated_overview.py' -v
```

Neither this figure generator nor the per-ACK audit changes the Nano
firmware, raw acquisition, original clock fit, signed onset inventory,
operator decisions, or approval hashes.


## Cycle-boundary and chronological attribution (exploratory)

For the four signed 2026-11-26 replay recordings, run:

```bash
python -m experiment_analysis.audit_cycle_boundary_observability --run-dir "$MTB4_RUN"
```

This reuses the verified pulse inventory, Gray anchors, USB timing evidence
and timing refinement. The previous `cycle_boundary_observability` output
is preserved. New outputs are written once to
`results/<RUN_ID>/analysis/cycle_boundary_observability_chronological/`.

- The scheduler hypothesis selects the latest available SF at the approved
  negative onset for the following period. The interval from BLE write begin
  to embedded ACK receipt models availability, not a calibrated application
  time bound. Commands during the period do not invalidate its witness.
- All frequency-independent candidates are preserved at each boundary.
  The chronological calculation assigns one matching identity to each
  matching assessed cycle, with nondecreasing SF transaction identities.
  It returns exact minimum/maximum distinct target counts, and the identities
  present on every path or on at least one path. Initial START and muted
  scheduler commands are not counted as target ACKs.
- An incompatible assessed cycle is explicitly listed; a path through the
  remaining matching cycles is conditional on that subset. No path means
  no chronological solution under these assumptions, not device failure.
- The independent clock envelopes are tested at 1x, 2x and 3x Gray margins,
  with frequency tolerances max(1 Hz, 2.5%), max(0.5 Hz, 1%) and
  max(0.25 Hz, 0.5%). Do not select clock offsets or tolerances to optimize
  agreement. Path feasibility across per-boundary envelopes does not
  establish a jointly feasible clock realization.
- The target denominator remains successful live target SF ACKs inside
  supported Gray time (using trace ACK membership for comparison with earlier
  audits). The per-target CSVs expose local evidence, chronological membership,
  candidate period IDs, minimum frequency error, current context, next-SF
  timestamps, and possible or strict timing brackets for replacement.
  Strict brackets constrain both command windows between observed boundaries;
  possible brackets use overlapping uncertainty windows. Neither proves
  supersession, and absence of a witness does not imply non-execution.

The summary reports all nine combinations of timing scale and frequency
tolerance. Detailed command investigation uses the original tolerance at
each scale. Existing cycle CSVs and signed approvals are not rewritten or
copied. The chronological result remains an exploratory model-conditioned
estimate, not an electrical safety or device execution qualification.


## Mute/resume diagnostic (exploratory)

```bash
python -m experiment_analysis.audit_restart_observability --run-dir "$MTB4_RUN"
```

Output: `results/<RUN_ID>/analysis/restart_observability/`. The diagnostic
shares the signed pulse, clock envelope, transaction and cycle-boundary
functions above. It distinguishes actual acknowledged SC restoration from
positive SF restore attempts that were replaced while current remained
commanded zero. The first observed onset candidates run from the restore
write to the first definitely post-ACK onset. Each is conditional on SC
restore availability at that boundary; earlier or residual output cannot
be ruled out. A candidate first-to-second pulse period may overlap the
associated SC restore transaction, but must not overlap any subsequent
SC write, STOP, timing-support edge or long pause. New SF transactions
during the period do not invalidate its initial frequency candidate.

The same frequency tolerance and 1x/2x/3x timing scales are used. CSVs
inventory SC restorations, first-cycle candidates and all restoration SF
ACKs (including cancelled attempts). The summary compares prior local
matches with newly recovered local evidence, counting each restore SF once.
These local union counts do not update the chronological bounds, because
possible-first-pulse alternatives need not all be realizable together.
No internal application time, electrical OFF, or successful physical
restart is proved by the SC ACK or timing model. All signed evidence and
prior outputs remain unchanged.
