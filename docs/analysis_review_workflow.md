# Analysis design and manual pulse-review gate (experimental branch)

This is the implementation starting point for reviewing N separate 20-second
CH1+CH2 scope records and one continuous MCU/Matrix32 host session. No
experimental result is automatically promoted to physically verified status.

[Integrated acquisition](integrated_matrix32_scope_recording.md) ·
[Clock uncertainty](clock_mapping_and_time_domains.md) ·
[Electrical load and references](electrical_bench_setup.md) ·
[Oscilloscope commands](../oscilloscope/README.md)

## 1. Nano CH2 Gray: physical evidence and limitations

[`oscilloscope/decode_gray_timecode.py`](../oscilloscope/decode_gray_timecode.py)
already detects threshold-crossing **complete high pulses**, then requires
a 4-ms START, each present 1-ms data-bit high in its correct 5-ms slot, and a
7-ms END at +90 ms. It rejects malformed/incomplete frames. Tests cover
Gray round-trips, 16-bit wraparound, missing frames, and CSV decoding.

**New audit field:** `record.decoded_gray.edge_audit` records every decoded
frame's START, individual 1-bit highs, and END, with rise/width timing
residuals; any complete thresholded high pulse that cannot be assigned
to a decoded frame is retained in `unassigned_pulses`. A fully matched
thresholded inventory is useful quality evidence; it cannot prove a pulse
whose analog edge never crossed the threshold, or recover half pulses at
scope record boundaries. Check the **full counter** against the Nano serial
FRAME messages and per-record clock-fit quality independently.

Three 20-second Gray-only hardware acquisitions previously reported ~199
valid frames each in saved experiment metadata. This supports feasibility
of complete-frame decoding but is **not** independent verification of every
analog edge, START/END and bit pulse in a new CH2 capture. A new physical
CSV plus the new audit is required for that stronger claim.

## 2. Matrix32 CH1: source-locked negative pulse detection

The related [matrix32_nerve_stimulation project](https://github.com/2103simon/matrix32_nerve_stimulation/tree/main)
has an intensively exercised **S02-v5 negative-voltage onset detector**.
Its current reviewed implementation is on
[`cleanup/analysis-consolidation`](https://github.com/2103simon/matrix32_nerve_stimulation/tree/cleanup/analysis-consolidation).

Copied unchanged to this repo:
- [`analysis_primitives/negative_events.py`](../analysis_primitives/negative_events.py):
  median/MAD baseline, negative excursion gate, sustained crossing,
  200-us prequiet occupancy, deadtime, morphology fallback, accepted,
  suppressed and rejected categories.
- [`analysis_primitives/baseline.py`](../analysis_primitives/baseline.py):
  robust median/MAD scale.
- [`analysis_primitives/record_start_rejection.py`](../analysis_primitives/record_start_rejection.py):
  distinguishes record-start boundary rejects.
- [`analysis_primitives/suppressed_crossing_association.py`](../analysis_primitives/suppressed_crossing_association.py):
  classifies suppressed crossings against accepted onsets.
- [`analysis_primitives/period_classification.py`](../analysis_primitives/period_classification.py):
  exact source version of the S06 fixed-period-state classifier. **Not
  automatically applied** to changing 2–400-Hz live frequency requests,
  because its original two fixed state centers are experiment-specific.

The new offline [`review_pulse_detection.py`](../oscilloscope/review_pulse_detection.py)
runs the identical onset detector at **80%, 100%, and 120%** of the robust
negative threshold. The primary 100% run uses **200 us** preceding
quiet, **150 us** phase deadtime and **0.75 ms** minimum separation
(30% of the shortest 2.5 ms period at 400 Hz), matching the original
parameter *policy* but adapted to a variable 2–400-Hz range.

The legacy integrated report's **positive-edge 2 V detector** remains in
place for comparison until actual CH1 waveforms are reviewed. The new
negative-onset inventory is **separate** and does not silently alter old
measurements or silently substitute one detector for another.

The current 1,000,000-sample/20-second acquisition has **20 us/sample**.
This gives ~5 samples over a 100-us phase, unlike the finer sampling
of some reference analyses. Expect explicit short-pulse resolution and
threshold-sensitivity warnings and inspect raw zooms.

### Run the detector review (offline only)

```bash
python -m unittest discover -s tests -p 'test_gray_timecode.py' -v
python -m unittest discover -s tests -p 'test_pulse_review.py' -v

python -u oscilloscope/review_pulse_detection.py \
  --run-dir data/recordings/<INTEGRATED_RUN>

# Analyze one 20-second record only
python -u oscilloscope/review_pulse_detection.py \
  --run-dir data/recordings/<INTEGRATED_RUN> --record 2
```

Each `analysis/record_0000N/pulse_review/` contains:

| File | Evidence |
| --- | --- |
| `audit.json` | baseline, detector settings, counts, flags, explicitly **unapproved** status |
| `accepted_onsets.csv` | all source-detector pulse candidates with times, voltages and quality statuses |
| `rejected_onsets.csv` | every rejected crossing |
| `suppressed_onsets.csv` | every within-deadtime/too-close crossing |
| `periods_unverified.csv` | each consecutive accepted-onset interval and its inverse period; **not confirmed frequency** |
| `candidate_overview.png` | min/max envelope and all accepted/rejected/suppressed onset locations |
| `raw_zoom_*.png` | full un-decimated samples near record start, middle, end and selected anomalies |

**Manual gate:** The detector never sets `complete_pulse_detection_approved`
to true. Inspect misdetected, missing, clipped, boundary, threshold-sensitive
and morphological cases and establish the detector performance before using
its intervals to claim a physical rate. A graphical overview is **not**
sufficient to certify every individual pulse in a long acquisition.

## 3. From validated onsets to physical pulse rate

For *consecutive, genuinely distinct, validated pulse onsets* at times
`t_i` and `t_(i+1)` **within the same scope record**, the observed
period is `dt_i = t_(i+1)-t_i`, and the candidate local frequency is
`f_i = 1/dt_i`. The interval's temporal support is the entire
`[t_i,t_(i+1)]`, not an instantaneous sample at one endpoint. If one
pulse is missed, its interval is artificially long and `f_i` artificially
low; such intervals need review flags rather than blind smoothing.

Report the **full per-interval table**, distribution, and representative
time-based plots. Time-window pulse-count rates (e.g. 1-second windows)
can complement this view but are too coarse to resolve rapidly changing
ACK states by themselves. Never compute a cross-gap interval.

The present `periods_unverified.csv` supplies these exact intervals
for review. Which intervals qualify to be compared with ACK states,
how to handle change boundaries, lag uncertainty and state dwell
requirements are **not yet decided**.

## 4. MCU arrival and Matrix32 ACK telemetry

The existing session analyzer
[`oscilloscope/analyze_integrated_session.py`](../oscilloscope/analyze_integrated_session.py)
counts continuous MCU samples **once**, and per-record MCU/BLE coverage
only under the local provisional clock map.

It now additionally calculates:
- MCU median/p10/p90 host-arrival intervals and arrival rate, nonpositive
  timestamp steps, and long interarrival intervals (>2× median).
- Full-stream SF request count, successful `sf_ack` count (`status=OK`),
  failed SF event count, non-OK ACK count and successful ACK frequencies.
- Successful SF ACK rate over the full MCU recording span, rate over the
  first-to-last SF ACK span, median/p90 inter-ACK spacing.
- Per-scope-record successful ACK count and rate only within that
  record's **provisionally mapped host window**.

These are host event statistics. A BLE ACK confirms a protocol response
but **not physical application time**. Counted requests and acknowledgments
are not proven one-to-one without a message identity.

## 5. What it means for an ACKed SF to be physically observable

**Not implemented as a yes/no verdict yet.** Rapid ACKed state changes
may last only a fraction of an output period; some states are **not
identifiable from physical cycles** even with perfect detection. A single
interval can cross an output transition. The observed interval also spans
a *finite duration* and is not the stimulator's instantaneous internal
frequency. Nano/USB cross-clock alignment has uncalibrated offset.

We should distinguish at least:

- **Observed compatible period(s)**: one or more *reviewed* physical
  intervals compatible with a state under a declared tolerance/lag rule.
- **No opportunity to observe**: the ACK state was too short, in a
  scope gap, near a record boundary, or outside a valid alignment.
- **Review required**: skipped/rejected/suppressed onsets, ambiguous state,
  missing or unqualified clock map, or multiple compatible frequencies.
- **Potential contradiction**: enough eligible physical periods contradict
  an ACK state, subject to declared lag/uncertainty bounds.
- **Physically unacknowledged frequency candidate**: a reviewed physical
  rate unsupported by neighboring successful ACK states, *not* proof
  of uncommanded output without excluding inherited/transitional states,
  timing uncertainty and undetected commands.

Before deciding a pass/fail classifier, agree on the **maximum permissible
ACK→physical apply delay/uncertainty**, the **minimum number of complete
cycles and minimum dwell time**, **frequency tolerance**, treatment of
transition periods, and whether detector review approval is per record
or per interval.

No physical pulse detection or clock accuracy has been validated by
software-only tests in this repository.

## 6. Source-locked S01/S02-style operator review reports

For each `analysis/record_0000N/pulse_review/`, the offline review script
now produces `report.md`, `metrics.json`, and `figures/` containing
the S02 min/max full-CH1 waveform with candidate crosses, an S01 original
raw-sample pulse zoom, a two-panel **individual plot for every rejected and
suppressed transition**, and individual raw plots of each relaxed-quiet
accepted candidate, plus threshold-sensitive candidate zooms. The exact reference implementations consulted are the
[S02-A frequency report generator](https://github.com/2103simon/matrix32_nerve_stimulation/blob/cleanup/analysis-consolidation/experiments/analyze_s02_a_frequency.py),
[S02 rejected-event close-up plotting](https://github.com/2103simon/matrix32_nerve_stimulation/blob/cleanup/analysis-consolidation/experiments/s02_rejection_review.py),
[S02 source-locked signoff verifier](https://github.com/2103simon/matrix32_nerve_stimulation/blob/cleanup/analysis-consolidation/experiments/s02_onset_review.py),
and [S01 width-sweep report/plots](https://github.com/2103simon/matrix32_nerve_stimulation/blob/cleanup/analysis-consolidation/experiments/analyze_s01_a_width_sweep.py).
The plotting helpers
[`plotting/envelopes.py`](../plotting/envelopes.py),
[`plotting/waveforms.py`](../plotting/waveforms.py),
[`plotting/style.py`](../plotting/style.py) and
[`analysis_primitives/s02_rejection_review.py`](../analysis_primitives/s02_rejection_review.py)
were copied unchanged from `matrix32_nerve_stimulation` branch
`cleanup/analysis-consolidation`. Dynamic recordings use the S02 plotting
helper, but **not** the original fixed-frequency missing-slot repair
classifier, because the commanded rate changes during our recording.

The report also generates an *editable* `review_decisions.csv` with
original IDs `P00001...` (accepted), `R00001...` (rejected),
`S00001...` (suppressed), time/sample, original detector status,
`decision`, and `note`. The operator should label each R/S as
**INCLUDE** (genuine physical pulse) or **EXCLUDE** (secondary/rejected
feature), with a brief evidence note. They may mark any P as EXCLUDE
(false positive) with a note. Blanks remain unresolved. For genuinely
missing pulses absent from the detector's event inventory, enter an
`M...` identifier, observed `time_s`, `INCLUDE` or `EXCLUDE`, and
raw waveform evidence in `manual_additions.csv`. Every included M event
requires an evidence note. The `threshold_sensitive_onsets.csv` file and
linked figures identify alternative-threshold-only Q candidates; if such
a pulse is genuine and absent from P/R/S, add an M entry with the Q figure
in its note. Neither Q nor M is auto-accepted.

For rapid review, leave correct P rows blank and **explicitly approve all
unchanged P rows** at sign-off using `--confirm-accepted`. R and S rows
always require individual decisions. If any detection is still uncertain,
do not sign off that record.

```bash
# 1. Generate figures, inventory and editable review tables. Hardware OFF.
python -u oscilloscope/review_pulse_detection.py \
  --run-dir data/recordings/<RUN>

# 2. Inspect analysis/record_00001/pulse_review/report.md and all raw
#    close-up plots. Edit review_decisions.csv (and optional additions).

# 3. Approve ONLY after checking all candidates and the entire recording.
python -u oscilloscope/apply_pulse_review.py \
  --run-dir data/recordings/<RUN> --record 1 \
  --reviewer "operator" --confirm-accepted --confirm-complete

# 4. The existing integrated analyzer now exposes a separate
#    reviewed_negative_onsets status only for source-locked approved records.
python -u oscilloscope/analyze_integrated_matrix32.py \
  --run-dir data/recordings/<RUN>
```

The approval is **hash-locked** to the raw CH1 waveform, generated event
tables, and the editable operator decisions. Any stale/changed file
invalidates the approval and the downstream loader fails closed. It
produces `approved_onsets.csv`, `approved_intervals.csv` and a
signed `review_approval.json`; original detection files are not edited.
A human-approved record with zero observed onsets documents an
observation at this instrument's sensitivity, **not** a guarantee of
zero electrical output. The session report separately counts only
approved records; other records remain **unknown**, not zero.

## 7. Frequency observability and cycle-boundary constraints

As demonstrated in the reference Matrix32 study, live SF updates apply
at cycle boundaries, not instantaneously on ACK. A current cycle may
finish after a new command is ACKed. When recording starts we do not
know the phase within the currently running Matrix32 cycle.

Our initial criterion for **physically observed frequency** is **two
consecutive, complete, operator-approved pulse intervals** compatible
with the same rate, within one scope acquisition. This is three
successive verified pulse onsets and two full onset-to-onset periods,
not two isolated pulses. The interval time support and any clock
uncertainty must be considered; no interval spanning an ACK transition,
an unobserved scope gap, or an uncertain transition is enough to claim
application.

Classification should remain:

1. **Physically observed (provisional attribution)** when two complete
   reviewed intervals match an ACK-compatible state under a specified
   lag/settling rule and a suitable per-record clock map.
2. **Not observable under available evidence** when dwell is too short,
   during acquisition gaps, near a transition/record boundary, or no
   qualified clock synchronization exists.
3. **Potentially inconsistent** if adequate reviewed intervals exist but
   contradict the expected state after accounting for transition and
   clock uncertainty.

The exact maximum ACK-to-physical-application lag has NOT been
measured. **A scheduler can wait until the end of its previous cycle**,
so a fixed 5-ms settling guard is not a universal guarantee,
especially when the previous rate was 2 Hz (nominal 500-ms cycle).
We should not treat an ACK as an electrical application timestamp or
label a state "skipped" without a model of prior-cycle timing and
sufficient post-transition observations.

For requested tactile rate zero, the active controller now ACKs **SC1=0**
*before* changing SF to **400 Hz**. This may shorten **subsequent**
scheduler periods while current is zero and improve later responsiveness,
but **cannot make the already-running preceding cycle finish faster**.
The muted SF=400 ACK is transport evidence, not evidence that 400-Hz
physical pulses were produced. As before, software STOP/SC ACKs are not
proof of physical output OFF; electrical verification remains necessary.
