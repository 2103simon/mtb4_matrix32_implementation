# E06 Step 2 — Requested MTB4 period sweep (prepared, NOT flashed)

**Status:** Source-only experiment branch. No tested build artifact or live
firmware change is established here. No hardware authorization or device
communication has occurred as part of preparation. The operator must
explicitly approve any build selection, flash, CAN change, and physical
capture procedure after reviewing the code and rollback.

## Scientific goal

The Feather requests MTB4 triangle-0 data at `0x4D` / 10 ms and
observes only about 68.7 Hz (14–15 ms CAN ISR intervals), repeatably.
In the approved E06 Step 1 no-contact captures:

- A1: **68.6808 Hz**, 108,331 event records; A1's six 30 s windows
  span **68.670–68.692 Hz** while event output changes 6.89×.
- B: **68.7002 Hz**, zero event records.
- A2: **68.7048 Hz**, zero event records.
- All archives verified, with zero internal MCU receive/queue loss
  indicators in enclosed 10-second diagnostic windows.

**Confound:** A1's taxels progressively declined to zero by its last
2.5 seconds; B and A2 were all-zero throughout. Thus Step 1 disproves
neither fixed encoder CPU overhead nor possible sensor-value changes,
and parameter-effect reversibility is **NOT VERIFIED**. However, near
constant 68.7 Hz at widely varying *observed* event load makes event
output unlikely to be the rate limiting factor.

**Step 2 question:** Does the measured MTB4-to-Feather-ISR sample cadence
track changes in the transmission period requested by the Feather?

## Firmware design — production defaults preserved

Existing `FalangeM4/src/main.cpp` sends CAN `0x4D` with
`E06_MTB4_PERIOD_MS` as the requested source transmission interval.
For ordinary `adafruit_feather_m4_can` builds the macro **defaults
to 10**. The compile-time assertion rejects values other than
10, 12, 16 and 20 ms.

Exactly four additional PlatformIO build environments in
`FalangeM4/platformio.ini` inherit the existing production target:

| Environment | CAN `0x4D` requested period | Nominal ideal sample rate |
|---|---:|---:|
| `e06_skin_period_10ms` | 10 ms (`0x0A`) | 100.00 Hz |
| `e06_skin_period_12ms` | 12 ms (`0x0C`) | 83.33 Hz |
| `e06_skin_period_16ms` | 16 ms (`0x10`) | 62.50 Hz |
| `e06_skin_period_20ms` | 20 ms (`0x14`) | 50.00 Hz |

The firmware otherwise retains the exact same CAN speed, single
triangle, ISR queue, encoders, command interface, serial protocol
and 10-second `0x12` diagnostic schema. We **do not** change or flash
MTB4 firmware. However **flashing and restarting the Feather changes
the MTB4's live CAN-configured source period** during normal startup;
this is an active test, not a passive sensor observation.

## First: review and build *offline only*

Keep the operator's validated diagnostics worktree untouched.
In a separate worktree:

```bash
workon tactile
cd ~/code/biointernect-edpr-e06-diagnostics
git fetch origin
git worktree add --detach ~/code/biointernect-edpr-e06-period-sweep \
  origin/experiment/e06-mtb4-period-sweep-20261113

cd ~/code/biointernect-edpr-e06-period-sweep
git rev-parse HEAD
git diff --stat origin/fix/mcu-e06-periodic-diagnostics-20261113...HEAD

python -m pytest -q tests/firmware/test_e06_period_sweep_build_config.py

cd FalangeM4
for period in 10 12 16 20; do
  platformio run -e "e06_skin_period_${period}ms" || exit 1
  sha256sum ".pio/build/e06_skin_period_${period}ms/firmware.bin"
done
```

These commands do **not** upload to a microcontroller, open a serial
device, or issue CAN commands. Preserve full build output and
per-variant firmware checksums; do not infer a successful build until
each environment exits zero.

## Planned physical experiment — NOT approved by this document

After the operator explicitly authorizes a specific Feather-only flash
and verifies the rollback image, port identity, no-contact setup and
GUI closure, use a **separate named recording for each condition**.
**Operator-approved time reduction (2026-11-13):** The earlier 10 ms A1/B/A2 archives provide a strong 180-second baseline; omit the initial redundant 10 ms sweep condition. Run the experimental sequence **`12 → 16 → 20 → 10 ms`**, with the last 10 ms capture serving as a restored-period control after firmware swaps. Reconfigure the MTB4 *only* by the reviewed normal Feather startup path. Each condition is **120 seconds** of ordinary read-only capture after a known-good startup and the correct firmware image is confirmed. The operator reports all four variants compiled; this is operator-provided status, not independently verified in CI. Restore the separately validated production 10 ms firmware image after the experimental comparison, not merely the compile-time 10 ms test variant, if it differs from the known-good version.

Avoid automated chains that can advance to another flash when a prior
condition failed. Review each trial before advancing. Do not leave a
test firmware image or modified 20 ms MTB4 source setting as a
new production baseline; restore the previously validated normal
10 ms image/settings under a separately reviewed procedure.

For every trial, preserve a unique `trial_id` with actual period,
firmware source SHA, PlatformIO environment, `firmware.bin` SHA256,
capture host and time, command ACK history (if any), no-contact
condition and startup observations. Run the same established:
`verify_serial_capture`, `analyze_taxel_timing`, and
`analyze_diagnostics`. Each must show intact archive and
`PASS_MCU_TO_HOST_WINDOWS_ONLY` with no internal reception
loss indicators. Also record sample-value distributions, so the
all-zero-taxel drift seen in Step 1 cannot be mistaken for an
encoder effect.

**Abort/rollback:** Any anomalous boot/discovery, wrong CAN board
address, missing diagnostics or taxels, CRC-invalid frames,
unexpected loss counter change, irregular frame pairs,
or unexplained rate behavior should halt the sweep and preserve
all artifacts. Do not automatically flash the next variant.

## Interpreting the measured curve

The empirical observed rate is computed from **Feather CAN-ISR
timestamps**, not independent sensor acquisition timestamps.
The MCU clock and host clock should be treated separately.

- If 10 and 12 ms both yield ~14.55 ms observed intervals, then
  16 and 20 ms yield ~16 and 20 ms, this supports a
  **source processing/scheduling saturation floor** around 14.5 ms.
- If *all* observed periods scale proportional to requests
  (e.g., 10→14.55, 12→17.46, 16→23.28, 20→29.10 ms),
  prioritize **period conversion, timer clock configuration or
  scheduler time base**, rather than a fixed acquisition workload.
- If 10 ms alone yields ~14.55 and 12 ms produces a distinct
  intermediate cadence, do not force a binary interpretation.
  Examine the exact sequence/histogram and event-task coalescing.
- If partial CAN pairs or variable large gaps appear, investigate
  source TX queue/discarding and receiver integrity before
  interpreting average rates.

Matching nominal periods at long requested intervals does **not**
prove the source acquires every requested sample at shorter periods.
Conversely, regular 14/15 ms arrival timestamps do not prove the
MTB4 source independently acquired at that interval or count all
source-side CAN frames. Maintain `scientific_status=NOT_VERIFIED`
for independent MTB4 source timing until source-side instrumentation
or independent CAN observation exists.


## 12 ms measurement — operator 2026-11-13

Trial `E06_SWEEP_20261113_155401_12MS`, 120 s
`serial_capture`. Operator reports the 12 ms Feather
variant was flashed after compiling all variants; the
physical uploaded image SHA, correct CAN configuration
request on wire and independent MTB4 applied period
remain **NOT_VERIFIED**.

- `serial_capture`: `CAPTURED`, 8,062 taxel frames,
  5,552 event frames, 16,628 event records,
  11 diagnostic frames, 313,791 raw bytes, 20,210
  read chunks.
- `verify_serial_capture`: `PASS`, 13,625 verified
  frames, 0 command ACK confirmations (none requested),
  raw bytes and chunk accounting verified. Transport
  parser has **65 discarded bytes** and **65 invalid-length
  candidates**, although zero CRC mismatch candidates
  and one residual trailing byte; the discarded prefix
  has **not been proven to be startup text**, so do not
  call the entire capture byte-clean.
- `analyze_taxel_timing`: 8,061 valid MCU
  CAN-ISR time intervals, **68.7499466956 Hz**,
  mean **14.5454658231 ms**, median/p95/p99 15 ms,
  **14 ms × 3,661**, **15 ms × 4,398**,
  **0 ms × 1**, **27 ms × 1**. One gap >20 ms;
  no resets or timestamp wraps. Host first-to-last taxel
  116.832311536 s versus MCU valid sum 117.251 s;
  these clock domains are not independently calibrated,
  and a 120 s capture window does not require 120 s
  between the first and last decoded taxel.
- `analyze_diagnostics`: `PASS_MCU_TO_HOST_WINDOWS_ONLY`,
  11 diagnostic frames, 10/10 reconciled windows,
  no missing diagnostic sequences. Covered counters
  `can_rx_observed=can_rx_queued=can_rx_dequeued=13,750`,
  `taxel_samples_completed=6,875`; zero CAN
  malformed, ISR queue overflow or missing sequences,
  event allocation failures or dropped records.

**Provisional outcome:** Increasing the requested
period from 10 to 12 ms did not slow arrival cadence:
measured **68.75 Hz vs 68.68–68.70 Hz** in the
validated 10 ms baselines, far below nominal 83.33 Hz
for a 12 ms request. This is consistent with, but does
**not prove**, a ~14.5 ms source-task floor.

**Gate before 16 ms flash:** The 65 discarded bytes
and 0/27 ms outlier timestamps violate the ideal
byte-clean/all-14–15-ms run seen at 10 ms.
Perform a read-only investigation of whether discarded
bytes are exclusively a startup prefix and whether the
0/27 ms intervals are adjacent at startup or indicate
a midrun disturbance. Archive integrity PASS verifies
raw-byte preservation/replay; it does not establish the
discarded bytes were innocuous.
Do not treat the anomalies as confirmed sensor-side loss
or as harmless artifacts without inspecting the saved
raw bytes and surrounding frames.


### 12 ms archive: startup byte and timestamp anomalies explained

Operator performed the proposed read-only binary inspection of
`E06_SWEEP_20261113_155401_12MS`.

- The first binary frame starts at raw offset **65**.
  The first 65 bytes are ASCII Feather startup output:
  `[INFO]: Starting CAN...connected!\r\n`
  followed by `----- Config completed -----\r\n`.
  The exact 65 discarded bytes and 65 invalid-length header
  candidates are therefore explained by the initial ASCII
  text prefix, not by a missing or corrupted binary payload
  in the remainder of this capture.
- The two anomalous consecutive taxel intervals are
  **indices 1 and 2**, at raw offsets 88 and 111:
  sample 0 and sample 1 share MCU `millis()=6524`
  (0 ms interval, both all-zero taxels); sample 2 has
  `millis()=6551` (27 ms interval) with
  values `[1,0,0,0,0,0,0,3,1,2,0,0]`.
  These are initial-startup observations only, not evidence
  of any midrun interruption or sustained loss.
- The remaining **8,059** intervals are exclusively
  **14 ms (3,661)** or **15 ms (4,398)**.
  Zero drift/reset indicators, ten diagnostic windows
  without internal loss, and raw archive integrity
  `PASS` support treating the **12 ms run as valid
  steady-state evidence**. Because the first two
  intervals represent initial startup dynamics,
  the full-capture average 68.7499466956 Hz is
  presented with this qualification.

**Decision:** Continue the approved 120-second step-2
sweep with a **16 ms requested period**, using the
separately built `e06_skin_period_16ms` Feather
variant. Verify port identity and ordinary boot behavior
after upload, then preserve the unique archive and run
all three established checks. Do not chain subsequent
flashes without examining results. Firmware and
physical MTB4 live period changes require the
operator's explicit hardware procedure; no board
action was taken by this documentation commit.


## 16 ms measurement — operator 2026-11-13

User supplied upload log and full analyses for trial
`E06_SWEEP_20261113_155401_16MS`.

**Flash verification:** PlatformIO `e06_skin_period_16ms`
using `sam-ba`, `/dev/ttyACM1` auto-detected;
26,880-byte `firmware.bin` uploaded and successfully
verified against flash by `bossac`. This supports a
successful Feather upload; independent on-wire CAN
parameter readback and physical MTB4 installed firmware
identity are not verified.

- Capture `CAPTURED` over requested 120 s:
  **7,346** taxel frames, 5,258 event frames,
  **9,939 event records**, 11 status frames;
  267,333 raw serial bytes, 16,550 read chunks.
  Zero CRC mismatch candidates, zero invalid
  decoded payloads; 65 discarded bytes / invalid
  length header candidates (same count as the
  verified **startup ASCII prefix** of 12 ms, but
  16 ms prefix's identity not independently inspected).
- `verify_serial_capture`: **PASS**, 12,615
  decoded frames and all raw bytes re-verified.
- `analyze_taxel_timing`: 7,345 valid intervals,
  **62.2816538344 Hz**, mean **16.0560925800 ms**,
  median 16 ms, p95/p99 17 ms. Histogram:
  16 ms × **6,924**, 17 ms × **416**,
  15 ms × **3**, 0 ms × 1, 31 ms × 1.
  Zero counter wraps or resets. First-to-last
  taxel host span 117.518501613 s, MCU interval
  sum 117.932 s (unaligned clock domains).
- `analyze_diagnostics`: **PASS_MCU_TO_HOST_WINDOWS_ONLY**
  with **10/10** enclosed windows reconciled, no
  missing sequence, no counter mismatch. Enclosed
  counters: `can_rx_observed=can_rx_queued=
  can_rx_dequeued=12,456`,
  `taxel_samples_completed=6,228`,
  `event_records_enqueued=serialized=7,510`,
  0 CAN malformed, queue overflow, missing ISR
  sequence, event allocation failure or drop.

**Interpretation:** In contrast to the 10 and 12 ms
requests yielding ~68.7 Hz (14/15 ms), the **16 ms**
request yields **~62.28 Hz**, close to the nominal
62.50 Hz, with 99.93% (7,340/7,345) of intervals
at 16 or 17 ms. This is strong support for a
processing/scheduling saturation near 14.5 ms
rather than a proportional period conversion
error, although a source timer/wake-up mechanism
has not been independently measured.

**Pending minor anomaly localization:** The 0 and
31 ms intervals may again be startup transients
and 65 discarded bytes may be the same startup
ASCII prefix seen at 12 ms; their precise position
and contents have **not** yet been independently
checked for the 16 ms archive. Do not claim they
have been located until verified. No sustained
internal MCU loss appears in the ten covered windows.

**Next approved experiment:** One 120-second **20 ms**
Feather variant capture, followed by a 120-second
**10 ms** restoration/control, after checking
operator rollback availability and normal boot
behavior at each step. Do not merge or reflash MTB4
firmware. Source-side generation and loss remain
`scientific_status=NOT_VERIFIED`.


## 20 ms measurement — operator 2026-11-13

Operator ran the approved 120-second `e06_skin_period_20ms`
Feather variant; source-image hash and CAN request on wire were
not independently observed. Trial:
`E06_SWEEP_20261113_155401_20MS`.

- `serial_capture`: `CAPTURED`, **5,949** taxel frames,
  **4,711** event frames, **9,531** event records, 11
  diagnostic frames, 227,553 serial bytes and 18,293 read
  chunks. No CRC mismatch candidates, no invalid payload
  frames, 65 discarded bytes and 65 invalid-length header
  candidates, one incomplete trailing byte.
- `verify_serial_capture`: `integrity_status=PASS`,
  **10,671** frames and all original byte/chunk accounting
  verified. The initial 65-byte ASCII prefix explanation
  confirmed at 12 and 16 ms **has not yet been independently
  repeated on this specific 20 ms archive**.
- `analyze_taxel_timing`: **5,948** intervals,
  **49.8149109730 Hz**, mean **20.0743106927 ms**,
  median 20 ms, p95/p99 21 ms, min 19 ms, max 35 ms,
  intervals: **20 ms × 5,516**, **21 ms × 429**,
  **19 ms × 2**, **35 ms × 1**. Thus the bulk
  (**5,947/5,948**) fall at 19, 20 or 21 ms;
  the 35 ms outlier's position is **not yet localized**.
  `interval_count_above_20ms=430` includes the expected
  21 ms quantization intervals, not necessarily dropped
  samples. No MCU clock wrap/reset. First-last host taxel
  span 118.974675685 s, MCU interval sum 119.402 s;
  the clocks are independent/un-calibrated.
- `analyze_diagnostics`: `PASS_MCU_TO_HOST_WINDOWS_ONLY`,
  11 status frames and **10/10** reconciled windows,
  no missing status sequences or mismatches.
  Covered counter deltas:
  `can_rx_observed=can_rx_queued=can_rx_dequeued=9,964`,
  `taxel_samples_completed=4,982`,
  `event_records_enqueued=serialized=7,968`,
  `event_frames_serialized=3,974`;
  zero CAN malformed, receive queue overflow,
  missing sequence, event allocation failures or drops.

**Period sweep evidence through 20 ms** (all numbers are
Feather MCU CAN ISR receipt rates, not independently timed
source acquisitions):

| Requested period | Nominal Hz | Observed Hz | Mean receive interval |
|---|---:|---:|---:|
| 10 ms, Step 1 A1 | 100.00 | 68.6808 | 14.5601 ms |
| 12 ms, Step 2 | 83.33 | 68.7499 | 14.5455 ms |
| 16 ms, Step 2 | 62.50 | 62.2817 | 16.0561 ms |
| 20 ms, Step 2 | 50.00 | 49.8149 | 20.0743 ms |

Results **strongly support a saturation/floor around
14.5 ms**: requests faster than this do not increase
the observed rate, whereas longer requests result in
near-nominal 16 and 20 ms sample intervals. This disfavors
a uniformly proportional timer-scaling error, but is
**not** independent measurement of MTB4 ADC/I2C runtime,
scheduler wakeups or CAN generation counts. Do not claim
the exact bottleneck has been isolated.

**Final required control / restoration:** Under operator
control, flash the validated **10 ms** Feather variant
(or prior known-good production image), verify normal
startup and selected port, then record 120 seconds with
unique `_10MS_RESTORE` trial ID and run integrity,
taxel timing and diagnostics. Confirm ~68.7 Hz and
no internal losses, and ensure the board is left with
the intended 10 ms runtime CAN period. Stop on failed
upload or unexpected boot/telemetry and restore the
known-working firmware. No further physical action
was performed by this documentation change.

`scientific_status=NOT_VERIFIED` for independent
MTB4 source acquisition/transmission timing.


## 10 ms restoration control — operator 2026-11-13

The user completed the last 120 s experiment, using
`e06_skin_period_10ms` (user-confirmed run; binary SHA
and on-wire MTB4 `0x4D` payload were not independently
captured). Archive:
`E06_SWEEP_20261113_155401_10MS_RESTORE`.

- `serial_capture`: `CAPTURED`, **7,915** taxel frames,
  **4,086** event frames, **6,275** event records, 11
  diagnostic frames, 252,872 captured raw bytes and
  22,458 read chunks. Decoder reports 65 discarded bytes
  and 65 invalid length candidates, consistent with the
  confirmed 65-byte serial startup prefixes on the earlier
  period-sweep trials; prefix contents in this archive have
  **not** been independently inspected. No CRC mismatch
  candidates or malformed decoded payloads; 1 byte of
  incomplete trailing frame at capture cutoff.
- `verify_serial_capture`: `integrity_status=PASS`,
  **12,012** frames, all read chunks and raw bytes
  independently rechecked.
- `analyze_taxel_timing`: **7,914** valid CAN-reception
  timestamp intervals, **68.6955314052 Hz**, mean
  **14.5569876169 ms**, median/p95/p99 15 ms,
  14 ms × **3,516**, 15 ms × **4,397**,
  and **one 25 ms** interval (position not yet localized).
  Thus **7,913/7,914** intervals are 14/15 ms.
  Zero MCU clock resets/wraps. MCU first-last valid
  interval sum 115.204 s vs host first-last taxel
  span 114.784928575 s (clock domains uncalibrated).
- `analyze_diagnostics`: `PASS_MCU_TO_HOST_WINDOWS_ONLY`,
  **10/10** enclosed windows reconciled over 11
  status packets, no missing sequences or counter
  mismatches, `can_rx_observed=can_rx_queued=
  can_rx_dequeued=13,740`, `taxel_samples_completed=6,870`,
  `event_records_enqueued=serialized=5,173`,
  `event_frames_serialized=3,534` in covered windows.
  All five internal loss indicators zero: CAN malformed,
  receive queue overflows, sequence missing, event
  allocation failures and event record drops.

**Sweep summary (operator's real-world measurements):**

| Requested MTB4 period | Nominal rate | Measured Feather CAN-reception rate | Mean MCU interval |
|---|---:|---:|---:|
| 10 ms baseline (A1) | 100 Hz | 68.6807865 Hz | 14.5601128 ms |
| 12 ms | 83.33 Hz | 68.7499467 Hz | 14.5454658 ms |
| 16 ms | 62.50 Hz | 62.2816538 Hz | 16.0560926 ms |
| 20 ms | 50.00 Hz | 49.8149110 Hz | 20.0743107 ms |
| 10 ms restoration | 100 Hz | 68.6955314 Hz | 14.5569876 ms |

The final 10 ms rate differs from the first A1 10 ms
baseline by **+0.0147448690 Hz (~0.0215%)**, confirming
that the 10/12/16/20/10 requested-period response is
**reversible to measurement precision**. Every archive's
record-integrity verifier passed and every 10-s enclosed
diagnostic accounting window passed without identified
Feather ISR/queue/event loss. Startup text and one or
two initial interval transients were identified for the
12 and 16 ms runs; the **35 ms** interval in 20 ms and
**25 ms** interval in the final 10 ms archive remain
unlocalized, so do not describe these archives as
completely outlier-free.

**Evidence-based interpretation:** The observed arrival
cycle responds approximately as
`T_observed ≈ max(T_requested, 14.5 ms)` over the
tested points, with minor overhead/clock quantization.
This strongly favors an MTB4 source-side
acquisition/scheduling bottleneck or related
non-overlapped periodic task over a simple
constant timer-frequency scaling error. It does
**not** independently establish a 14.5 ms
sensor conversion duration, a particular source
function bottleneck, one-transmission-per-source-trigger,
or absence of source-side packet coalescing/loss.
The earlier static source audit found thousands of
busy-wait I2C bit-bang operations, timer/event
coalescing and a potential source TX queue,
but none of their runtimes has been directly measured.

**Decision:** Approved time-constrained Step 2
recordings complete; avoid further physical
tests without a fresh decision. Keep PR #15
a draft experimental branch separate from
production. Ensure actual hardware is returned
to the known-good **production** Feather image
and 10 ms MTB4 live configuration before normal
use; the `e06_skin_period_10ms` test build
returns the same period request but may not be
the identical production binary. Independent
source timing and losses remain
`scientific_status=NOT_VERIFIED`.
