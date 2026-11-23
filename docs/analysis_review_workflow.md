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
