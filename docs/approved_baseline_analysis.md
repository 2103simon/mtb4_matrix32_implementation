# Analyze an approved integrated baseline

Use one acquired record's approved negative CH1 onsets to measure physical
intervals, then assess which comparisons with MCU requests and Matrix32 ACKs
remain interpretable under the saved clock uncertainty. Keep the original
recording, review approval and clock mapping intact.

This workflow follows the [integrated recording guide](integrated_matrix32_scope_recording.md).
The canonical Nano environment remains `nano_every_10hz_hardware_timer`.

## Required evidence

The run directory must contain both original waveform CSVs, `metadata.json`,
`nano_serial.jsonl`, `epoch_clock_analysis.json`, the complete `mcu_matrix32/`
trace, and the source-locked `pulse_review/` approval and onset tables. The
standard integrated analyzer must already have generated
`approved_frequency_tracking.csv` and `approved_frequency_summary.json` for
the selected record.

The approval verifier checks the original CH1 SHA-256 and the hashes of the
review files. An approval made with `--confirm-accepted` can legitimately
retain blank accepted decision cells in the original decision CSV. Replacing
that CSV with a later edited copy invalidates the recorded approval hash.
Store additional waveform observations in a separate note.

## Audit frequency comparisons with the original affine map

```bash
MTB4_RUN="data/recordings/<INTEGRATED_RUN>"

python -m oscilloscope.audit_affine_frequency_tracking \
  --run-dir "$MTB4_RUN" \
  --output-dir "${MTB4_RUN}_affine_audit" \
  --record 1
```

The output directory must be outside the source run, including when symbolic
links are resolved. Repeating this command replaces this supplemental audit's
outputs in that destination; it never replaces the approved run's inputs.

The audit uses the **existing** scope-to-host affine map and preserves its
qualification status. It does not select a different clock model, fit an
offset to the physical pulse frequencies, relax the 100 µs clock diagnostic,
or require the optional clock-model cross-validation report.

The declared comparisons are:

1. Replay the original midpoint-map classification for every approved interval.
2. Restrict cross-clock comparisons to the first-to-last matched Gray frame
   START support, retaining boundary intervals as flagged physical observations.
3. Evaluate the entire conditional USB offset interval, relative to the fitted
   midpoint offset. Check every ACK/MCU transition within the envelope.
4. Repeat with additional ±0.5 ms and ±1 ms placement margins and minimum
   settling guards of 5, 20 and 50 ms. The actual ACK guard is the greater of
   the selected minimum and the preceding active ACK period. An unknown or
   muted preceding cycle is ineligible for this comparison.

The additional margins and guards are sensitivity hypotheses. They are not
calibrated timing bounds or device acceptance specifications. The existing
frequency screen remains `max(8 Hz, 15% of the ACK-indicated frequency)`;
the report also gives the actual frequency/period differences without claiming
that this screen proves calibrated accuracy.

Every approved interval remains in each scenario's table, including long
pauses. The reciprocal of an interval spanning a mute or restart is not a
demonstrated steady pulse-train frequency. A comparison that crosses an ACK
transition or fails the prior-cycle guard is unclassified, not automatically
a failed pulse or failed command.

### Supplemental outputs

| File | Contents |
| --- | --- |
| `frequency_offset_hypotheses_summary.json` | Source hashes, original clock status, assumptions, classification counts and error summaries |
| `frequency_offset_hypotheses_summary.csv` | Compact comparison of all declared hypotheses |
| `frequency_offset_hypotheses_intervals.csv` | Every approved interval in every hypothesis, with classification and available errors |
| `frequency_offset_hypotheses.png` | Eligibility counts and original-map period differences |
| `sf_request_source_audit.csv` | SF request/ACK pairing, originating MCU values and host-native protocol durations |
| `gap_command_context_provisional.csv` | Commands around the longest within-record interval |

SF commands while current is commanded zero are scheduler state changes, not
evidence of physical stimulation at that frequency. The standard report's
active SF state count therefore differs from the raw SF ACK count. The
supplemental audit reports raw event counts separately.

## Inspect CH2 physical Gray timing

```bash
python -m oscilloscope.audit_gray_pulse_timing --run-dir "$MTB4_RUN"
```

This reads original CH2 samples and creates additional Gray geometry reports
under the run's `analysis/` directory. It verifies pulse roles against the
saved Gray frames and Nano log, measures START/DATA/END widths and frame
spacing, and leaves the original clock fits and CH1 approval unchanged.

For an observed `10HZ_HW_TCA_BUFFERED` INFO mode, the report correctly
describes D7 as hardware PWM and `EDGE_US` as a `micros()` reading in the
frame-start ISR. Hardware edge generation does not make that reading a
hardware-latched electrical timestamp. Unknown/historical INFO modes are
reported without assuming which firmware generated them.

## Interpret mute and restart separately

Use both clocks explicitly:

- CH1 onset-to-onset intervals use scope-local time and remain direct physical
  measurements under the accepted onset inventory.
- Source-sample, request, write and ACK durations from one host boot use PC
  monotonic time. They describe software/transport behavior.
- A mapped ACK-to-pulse separation uses the provisional cross-clock map.
  Report that assumption and assess its offset sensitivity before discussing
  response timing. An ACK is not an electrical application timestamp.

A large restart separation after restoring a very low positive SF can be
consistent with a long pending device cycle. A controlled follow-up can vary
the first nonzero request after a release while retaining the same Nano,
scope settings, current and phase width. Do not infer device firmware apply
semantics from one sequence or modify a clock fit to remove the separation.

For guided manual input, add
`--operator-prompts config/operator_prompts_resume_20s.json` to the integrated
recording command. The optional terminal cues provide the hold/release/gentle/
firmer sequence and record their delivery times. See the
[timed operator prompt instructions](integrated_matrix32_scope_recording.md#optional-timed-operator-prompts)
for the editable seconds-to-text dictionary and timing limits. Classify resume
conditions using the first positive MCU request actually recorded, since a
firmer manual press can still pass through a low initial request.

An accepted event with incomplete phase morphology stays in the approved
onset inventory. Keep a separate original-sample zoom and morphology note;
acceptance for onset timing does not establish phase-amplitude or charge
balance equivalence.

### Host guard before restoring current

The controller rereads the latest MCU sample after awaiting the positive SF
acknowledgment and before requesting SC restoration. A fresh zero or sub-2 Hz
request keeps SC commanded zero and restores the existing muted SF400 policy.
Missing/stale input raises through the existing shutdown path. The zero is
reprocessed even when a serial batch gives it the same arrival timestamp as
the older positive sample. Current-restore provenance names the fresh sample.

This guards the host decision before the SC transaction. It cannot cancel a
write already in progress if the input changes later, synchronize that write
to a physical pulse phase, or remove a pending device cycle. Tests exercise
the actual controller functions with deterministic fake device/MCU inputs;
they do not substitute for hardware or full shutdown integration tests.

## Storage and reproducibility

Commit the reusable scripts, tests and this workflow guide. Keep run-specific
CSV/JSON files, plots, waveform extracts and narrative results with the
recording archive; `/data/*` is excluded from Git. Additional results do not
require copying a new decision sheet into an already approved run.
