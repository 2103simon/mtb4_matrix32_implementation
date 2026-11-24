# Approved Matrix32 SF-ACK vs. physical pulse observability

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
