# Time-domain architecture: Nano Gray code, scope, MCU and PC

This guide documents the actual implementation in
[`arduino_nano/src/main.cpp`](../arduino_nano/src/main.cpp),
[`oscilloscope/align_gray_records.py`](../oscilloscope/align_gray_records.py),
[`oscilloscope/analyze_gray_epoch_experiment.py`](../oscilloscope/analyze_gray_epoch_experiment.py),
and [`communication/clock_sync.py`](../communication/clock_sync.py).
It explains what is **observed**, **estimated** and still **uncalibrated**.

## Why three clocks are necessary

1. **Tektronix scope sample time**: relative seconds along an individual captured waveform. Scope CH1 contains the Matrix32 physical output; CH2 contains Nano digital markers. Both channels in a *single acquisition* share the scope sampling axis.
2. **Arduino Nano Every `micros()`**: an independent, rolling 32-bit microsecond counter. The firmware prints time-tagged marker frame IDs and supplies request-receipt/response timestamps.
3. **Linux PC `time.monotonic_ns()`**: used for serial arrival timestamps, MCU snapshots and Matrix32 command/ACK observations. It is not UTC wall time. Some files also contain creation timestamps in UTC for naming and audit purposes, not for nanosecond synchronization.

The Feather has a further **millisecond MCU timestamp** carried in tactile telemetry. The host's `clock_compensation` and drift-monitor modules provide separate MCU/PC diagnostics. These are **not** the same oscillator or estimator as the Nano↔PC clock fit.

## Nano frame construction

The Nano Every firmware emits one frame per **100 ms (10 Hz)** on D7:

| Relative position | Marker |
| --- | --- |
| 0–4 ms | START high |
| 4–7 ms | guard low |
| 7–87 ms | sixteen MSB-first Gray data slots; each slot 5 ms, HIGH for 1 ms only when that bit is 1 |
| 87–90 ms | guard low |
| 90–97 ms | END high |
| 97–100 ms | idle low |

The Gray value is derived from the low sixteen bits of the full frame counter: `gray16 = counter16 XOR (counter16 >> 1)`. After each physical **START rising edge** the firmware calls `emitFrameLog(micros())`, so the USB log provides:

```text
FRAME <full_counter> GRAY <hex_value> EDGE_US <nano_micros> OVERRUNS <count>
```

The D7 edge happens just **before** the firmware samples `micros()` for `EDGE_US`. Scheduling and GPIO/timestamp call overhead are not hardware capture-calibrated; `EDGE_US` is a nearby Nano clock observation, not a direct oscilloscope timestamp. The `OVERRUNS` field detects some scheduler lateness/missed frames, but a zero overrun count does not certify zero microsecond jitter.

The USB ASCII `FRAME` message may arrive later than the edge because of USB packet buffering and OS scheduling. **Its host receive time is not used as the physical marker time.**

## Stage 1 — map one scope acquisition into Nano time

The decoder [`decode_gray_timecode.py`](../oscilloscope/decode_gray_timecode.py)
examines the CH2 waveform and recovers each complete frame's *low-16-bit* counter and its scope START time. The USB log supplies the corresponding **full** frame counter and Nano edge time. The alignment code:

1. Validates each Gray value against the counter; rejects missing, duplicated, reversed, or ambiguous matches.
2. Unwraps the Nano 32-bit `micros()` rollover (~71.6 minutes), rejecting nonmonotonic clock behavior.
3. Matches each CH2 frame START against its corresponding USB `EDGE_US`.
4. Fits a centered least-squares affine model separately for **each scope record**:

```text
nano_time_seconds = a * scope_time_seconds + b
```

Approximately 199 complete 100 ms frames have been observed in 20-second single-record trials. This fit estimates the *relative clock rate* and a *local origin*; it does not yet connect the oscilloscope to PC monotonic time.

Output: `gray_alignment.json` with local `clock_fit`, matched-frame count, edge-fit RMS and maximum residual. The original default qualification threshold is **100 µs RMS**; relaxing it for diagnostics does not turn an unqualified result into calibrated physical timing.

## Stage 2 — map Nano time into PC monotonic time

Rather than assume that a `FRAME` USB receive time is immediate, the host sends actively matched **four-timestamp SYNC** requests. The current preferred transport is **CRC32-protected binary SYNC v1**:

| Symbol | Clock | Observation |
| --- | --- | --- |
| `t1` | PC monotonic ns | Just before PC sends SYNC request |
| `t2` | Nano `micros()` | When Nano finishes receiving the request |
| `t3` | Nano `micros()` | Immediately before it queues the response |
| `t4` | PC monotonic ns | After PC receives and validates the response |

The firmware's receive and transmit timestamps do **not** remove buffering or know the exact one-way USB transit time. The Nano binary request is 12 bytes and response 20 bytes, with `A5 5A` framing, version/type/sequence fields, little-endian Nano timestamps and IEEE CRC32 over the message body. The legacy ASCII `SYNC <seq>` remains supported for comparative experiments.

In an integrated acquisition the runner gathers **30 independent exchanges before arming the scope and 30 after acquisition**, using only those associated with that record's `clock_epoch`. The clock analysis estimates:

```text
pc_monotonic_ns = c * nano_micros + d
```

A fit is computed from those local exchanges, with residual diagnostics and causal offset interval checks conditional on the estimated slope. No offset is independently calibrated: path asymmetry and host USB scheduling remain uncertain.

The difference between **clock drift in ppm** and **fit RMS in microseconds** is important. A slope estimate of -800 ppm is not an 800 µs timing error; a 200 µs RMS is not an upper bound on absolute offset.

## Stage 3 — compose the maps

For a CH1 pulse at scope time `t_scope`:

```text
t_nano ≈ a*t_scope+b
t_pc   ≈ c*t_nano+d
       ≈ c*(a*t_scope+b)+d
```

This permits comparison with host timestamps from the Feather serial reader and Matrix32 BLE command logger. A **BLE SF acknowledgment** is still a PC-observed protocol event: it does not prove when a new SF took effect at the physical output. Absolute pulse-to-ACK latency is **provisional**, not verified, even when the least-squares fits have small RMS residuals.

For measuring **pulse-to-pulse intervals solely on scope CH1**, no cross-device clock mapping is necessary. Similarly, MCU update cadence can be analyzed on its own timestamp domain without asserting synchronization to CH1.

## Independent recordings, continuous logs and gaps

A 20-second scope waveform is a **finite acquisition**, not a continuous stream across a sequence of experiment pauses. Its local sample times must be treated separately from all other scope records. In the earlier Gray-only epoch experiment, **three separate 20 s scope records with 60 s intended pauses** were analyzed using three independent pre/post Nano SYNC epochs.

The integrated Matrix32 capture runner currently performs **one scope acquisition per invocation**. Its Feather serial and BLE logs can start **before** the scope and continue **after** it. The integrated analyzer is designed to:

- Validate each scope record independently (CH1 pulses, duration, frequency windows, local Nano fit).
- Use the matching *record index*, never the previous or next record's Nano→PC fit, to estimate that record's host coverage.
- Restrict MCU and BLE timestamps to the overlapping estimated host interval for **per-record** cross-device analysis.
- Retain and plot the **full** independent MCU stream separately and count full BLE events **once**.
- Report unobserved gaps explicitly, without interpolating waveforms or forming a pulse period between the end of one scope record and the beginning of another.
- Mark overlap **unknown** if a valid local scope-to-host fit is missing, rather than presenting zero MCU events as a measurement.

At present the [integrated analysis entry point](../oscilloscope/analyze_integrated_matrix32.py) requires integrated CH1+CH2 metadata and a single continuous MCU rate file for the session. The older Gray-only runs use the Gray-specific analyzers instead; they are not silently converted into Matrix32 recordings.

## Which observations can we trust?

| Question | Evidence / limitation |
| --- | --- |
| Was a physical pulse visible? | Direct CH1 waveform above a documented threshold; check differential probe, clipping and noise |
| What was the physical pulse interval? | Scope CH1 crossing-to-crossing time within *one* record |
| Which Gray frame is this? | Decoded CH2 counter matched against Nano full frame USB log |
| How well do scope and Nano time track locally? | Per-record affine Gray fit and edge residuals |
| How well does Nano time track PC time locally? | Four-timestamp binary SYNC slope, offset interval and fit diagnostics |
| Did Matrix32 acknowledge SF? | Timestamped BLE command/ACK JSONL (software protocol evidence) |
| Did physical pulses follow ACK-indicated SF? | Provisional dynamic per-cycle consistency check, with transition/mute/unknown intervals excluded |
| Is PC↔physical-output latency calibrated to 200 µs? | **No**: USB one-way asymmetry and ACK-vs-physical timing are not independently calibrated |
| Were pulses present while the scope was not sampling? | **Unknown** from oscilloscope data; the experiment cannot reconstruct missing waveforms |

## Files, commands and further references

```bash
# Gray-only three-record experiment: dry-run first
python oscilloscope/test_gray_epoch_pause.py
# Real hardware, scope and Nano only:
python oscilloscope/test_gray_epoch_pause.py --run

# Offline: independent local fits for an existing Gray run
python oscilloscope/analyze_gray_epoch_experiment.py \
  data/recordings/<GRAY_RUN>

# Integrated Matrix32 session analysis: one report per scope record
python oscilloscope/analyze_integrated_matrix32.py \
  --run-dir data/recordings/<INTEGRATED_RUN>
```

Also see [Gray timecode detailed experiment](gray_timecode_scope_experiment.md), [Nano firmware guide](../arduino_nano/README.md), [integrated bench procedure](integrated_matrix32_scope_recording.md), [Nano clock tests](../tests/test_clock_sync.py), and [scope/Nano matcher tests](../tests/test_gray_alignment.py).
