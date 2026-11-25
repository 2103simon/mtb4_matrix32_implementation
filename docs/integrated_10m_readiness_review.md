# Integrated 10M baseline readiness review — 2026-11-24

## Decision

This section records the pre-acquisition source review. A subsequently
supplied 2026-11-24 baseline contains one 10M-sample CH1+CH2 record, observed
`10HZ_HW_TCA_BUFFERED` INFO, 203 buffered frames with zero reported loss,
200 matched complete scope Gray frames, and source-locked operator approval
of 1,011 CH1 onsets. The original CH1 hash and review hashes were verified
against that archive. This supplies the previously missing acquisition/INFO
evidence; it does not substitute for the unresolved dependency-enabled full
test run, compiler memory report or independent electrical checks below.
Run-specific analysis results remain with the recording rather than this
source-readiness document. See [approved baseline analysis](approved_baseline_analysis.md)
for the reproducible post-acquisition workflow.

The selected Nano hardware-timer implementation is retained. The code audit found
host-side reliability and evidence-preservation defects and fixes them without
changing the Nano waveform, timing source, fixed RAM buffer, raw recordings or
operator-approved electrical onset timestamps.

This review does **not** certify the bench ready to stimulate. A fresh PlatformIO
compile/upload, hardware INFO response, complete dependency-enabled test run,
simultaneous CH1+CH2 acquisition capability, and the operator's electrical checks
remain required. No hardware was opened and no stimulation or upload was launched
during the review.

## Repository state checked

The source audit starts from `feature/live-oscilloscope-recording` at
[`97da5c39ed672429d5204cde9759aeb7144e251d`](https://github.com/2103simon/mtb4_matrix32_implementation/commit/97da5c39ed672429d5204cde9759aeb7144e251d).
All 151 materialized source/configuration/test files were verified against their
Git blob hashes before edits. Historical image files and recording data were not
changed.

The handover's PR state is stale: [PR #2](https://github.com/2103simon/mtb4_matrix32_implementation/pull/2)
was already merged at `acb1e457d00cba7ce61b29871ad6635beb14cce4`, producing
`main` commit `dd394720f5f8ca669acdb0c18300730f7df8a3f6`. The feature branch has
eight later commits, including the canonical PlatformIO environment restoration
and corrected operating documentation. Those later changes were not in PR #2.
No submitted PR reviews or check runs for the merge commit were returned by the
repository APIs during this inspection; merge status alone is not verification.

The existing `experiment/integrated-10m-analysis` points to
`559f4ee9f8858dd38349fa1193c25cf8107a9dcb`, an ancestor 472 commits behind the
handover feature commit. It lacks the oscilloscope implementation. Do not use it
as the prepared baseline. Preserve it until the intended post-review branch state
is resolved; this review does not move it or merge into main.

## Firmware findings

[`arduino_nano/platformio.ini`](../arduino_nano/platformio.ini) has exactly one
environment and default: `nano_every_10hz_hardware_timer`.
[`arduino_nano/src/main.cpp`](../arduino_nano/src/main.cpp) uses D7/PA1/TCA0-WO1,
single-slope PWM, DIV64 and the selected 100 ms Gray frame geometry. Physical
transitions are hardware-generated; the ISR prepares the next cell.

Each frame START causes an actual `micros()` reading in the ISR. That observation
is neither a frame-index-derived timestamp nor a hardware-latched edge timestamp.
The fixed 256 × 12-byte array consumes 3,072 bytes for rows alone. Total SRAM,
stack margin and flash still require the real compiler/linker output.

The ISR is the buffer writer. BEGIN resets count/loss while interrupts are
disabled; END disables buffering before counters are read; DUMP only runs with
buffering disabled. Full storage increments the loss counter without replacing
earlier rows. Routine FRAME messages are absent during buffering. No firmware
source or PlatformIO configuration change was needed for these findings.

## Corrections made

| Defect | Resulting behavior |
| --- | --- |
| START acknowledgment failure escaped Nano shutdown | START now executes inside the STOP-protected block; ambiguous START is not retried |
| Acquisition or BEGIN failure left buffering active | Explicit END cleanup runs before outer shutdown, retaining the primary error |
| END/DUMP validation failure hid which command completed | ACKs are retained immediately; acknowledged END is not repeated |
| Firmware validation accepted matching prefixes | Exact INFO field matching is required |
| 30/60 s integrated plans exceeded the 256-frame horizon | Oversized plans fail before devices open; the buffer polling budget includes BEGIN overhead |
| Dump checking accepted any strings with the right count | Payload, Gray/counter, ordering, loss and no-streaming checks now validate the evidence |
| Empty or mismatched CH2 inventories were only a console warning later | Explicit per-record/session frame-evidence fields prevent clock alignment while retaining direct CH1 evidence |
| CSV validation retained millions of dictionaries | Streaming validation keeps memory bounded |
| Binary decoding expanded every code into Python integers | Iteration preserves original codes with substantially smaller memory use |
| Sample rate, duration and scaling were insufficiently cross-checked | Readbacks and preambles are verified and probe context retained |
| Detector reruns could overwrite source-hashed review evidence | Reruns refuse before loading/writing when review or approval artifacts exist |
| A new session report could assert facts about historical Record 5 | Notes use actual record inventories and show unavailable frequency as n/a |
| Controller metadata described the obsolete SF=2 zero-input strategy | The manifest derives its SF value from the existing controller constant (400 Hz) |
| Bench/test name collisions prevented full unittest discovery | Package imports and test lookup precedence are corrected |
| Legacy analyzer tests expected removed automatic-onset APIs | Tests follow the current approved-negative-onset interface; deprecated detection is not restored |

The implementation and tests are linked from
[`record_integrated_matrix32.py`](../oscilloscope/record_integrated_matrix32.py),
[`record_scope.py`](../oscilloscope/record_scope.py),
[`tek_mdo_usb.py`](../oscilloscope/tek_mdo_usb.py),
[`test_gray_timecode.py`](../oscilloscope/test_gray_timecode.py), and
[`review_pulse_detection.py`](../oscilloscope/review_pulse_detection.py).

## Validation and practical limits

The exact required commands were run on the proposed code revision:

| Check | Result |
| --- | --- |
| Integrated recorder suite | 25 tests passed |
| Full unittest discovery | 198 test entries: 195 passed, 3 module import errors |
| 10M / 20 s dry run | Passed; one record, both channels, devices unopened, stimulation unarmed |
| Python compileall for scope/tests/controller | Passed |
| Git whitespace validation | Passed |

The three full-suite errors are `test_connect_matrix32` (missing `bleak`),
`test_tactile_rate_frames` (missing `serial`), and `test_tactile_stimulation`
(missing `serial`). They are unresolved environment gates, not passing tests.
The current environment lacks pyserial and bleak, so these modules cannot pass
their import gate here.
Supported package-download attempts returned no available distributions. No
production stubs, artificial skips or fabricated hardware results are used.

The environment also has no PlatformIO, AVR compiler or PlatformIO package cache;
a supported PlatformIO package-download attempt returned no available
distributions. Source inspection and protocol tests do not substitute for
`pio run -e nano_every_10hz_hardware_timer` and a real board upload.

A hardware-free, full-size synthetic transfer exercise preserved 10,000,000
synthetic sample codes, wrote a 240,509,857-byte CSV, and passed streaming timing
and scaling validation. The nominal window was 20 s; first-to-last span was
19.999997999999998 s and mean interval 2 microseconds. Peak process RSS was
38,856 KiB. Transfer/write took 13.14 s and the complete exercise 26.86 s in this
environment. Temporary synthetic data was removed. These are software scaling
results, not instrument throughput or physical acquisition evidence.

The added scope checks use a format-independent numerical compatibility bound
plus floating-point arithmetic allowance. They do not certify oscillator,
voltage, probe or absolute timing accuracy. A regression rejects a 22 s waveform
for a requested 10M/20 s record even when its readbacks use abbreviated notation.

## Gates before the next armed baseline

1. Install the project's real dependencies and pass both the targeted integrated
   recorder tests and `python -m unittest discover -s tests -p 'test_*.py' -v`.
2. On the bench host, compile and upload the canonical PlatformIO environment,
   inspect total SRAM/flash use and verify the live INFO fields.
3. Complete the operator's [documented electrical verification](electrical_bench_setup.md):
   dummy-load branches and ratings, compatible grounding/earth reference, probe
   arrangement, accessible physical disconnect, and no subjects/electrodes.
4. Verify scope readback and physical capability for one continuous 20 s record
   containing 10,000,000 original samples on each of CH1 and CH2. A software
   readback check and the earlier CH2-only run do not establish this capability.
5. Preserve both waveforms, zero-loss validated Nano frames, continuous tactile
   telemetry and Matrix32 command/ACK logs. Check clipping, physical polarity,
   probe attenuation, frame-evidence qualification and actual output-off state.
6. Review and approve source-locked negative CH1 onsets before reporting physical
   pulse intervals/frequency. Keep direct electrical timing separate from
   provisional clock maps and uncalibrated BLE/USB timing.

The [operating guide](integrated_matrix32_scope_recording.md) retains the exact
20 s / 10M dry-run and operator-run commands. No additional clock reference or
Nano timing redesign is proposed.
