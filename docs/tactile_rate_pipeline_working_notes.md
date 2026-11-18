# Tactile-to-Matrix32 latest-sample pipeline — working notes

Status: **investigation / design only**. No firmware or Python behavior changed yet.  
Branch: `feature/tactile-rate-streaming`  
Last reviewed: 2026-11-18

## Goal

Replace time-stamped MCU spike-event telemetry with **per-taxel raw values and RA-I / SA-II rate estimates**. Python will consume and record these snapshots, and subsequently set Matrix32 **CH1** stimulation frequency from a user-selected taxel. Optimize for **freshest available tactile sample**, not lossless playback of old stimulation commands.

Given (observed by project): MTB4 tactile updates ~70 Hz; Matrix32 parameter updates ~44 Hz. These are observations, not verified firmware-imposed limits. Do **not** solve rate mismatch by accumulating a FIFO of stimulation commands; use a latest-value/coalescing mechanism at the output stage.

## Incremental plan

- [x] Inspect MCU acquisition, encoding, framing, and Python receiver.
- [ ] **(i)** MCU emits raw taxels + per-taxel RA-I and SA-II rates, without generating/sorting/transmitting events.
- [ ] **(ii)** Python parses, stores, and records these snapshots; add a latest-sample interface for Matrix32.
- [ ] **(iii)** User chooses taxel (0–11) and rate type; Python safely configures Matrix32 CH1 at the newest available frequency.
- [ ] Measure update age, drops, frequency mapping, and stimulation latency on hardware.

## Current MCU path (verified in code)

**`adafruit_feather_m4/src/main.cpp`**

1. `onCanReceive()` (around line 673) copies each CAN frame and arrival `millis()` to an interrupt-safe ring queue of 32 frames; the ISR does not encode or transmit.
2. `assembleTaxelSample()` (around line 1014) assembles `0x40` (7 inverted values) + `0xC0` (5 inverted values) from the same CAN ID into **12 unsigned 8-bit taxels**. Incomplete first halves are superseded. The loop also clears pending halves on CAN sequence gaps.
3. `loop()` (around line 1050) on each complete sample uses the **second CAN frame arrival time** as `currentTime`, then calls:
   ```cpp
   transmitTaxelData(currentTime);
   mechanoreceptorEncoding(currentTime);
   sortAndSendEvents();
   updateTime();
   ```
4. `transmitTaxelData()` (around line 330) sends a CRC-framed **0x02** packet: `uint32 timestamp_ms (BE) + uint8 taxel[12]`.
5. `mechanoreceptorEncoding()` (around line 635) calls `RaEncoding()` and `SaEncoding()` for each taxel (alternate SA encoder is commented out).
6. `addEvent()`, `sortAndSendEvents()` (around lines 352–438) maintain a dynamically resized event array, sort spikes and emit **0x04** event frames. This work can be removed from the new streaming path once validated.

Framing in `writeFrame()`: `uint16 length_BE (= 1 + payload_size)`, `uint8 type`, payload, `uint32 CRC32_BE` over `type + payload`. Binary mode is selected by default. CAN bitrate is configured to 1 Mbps. USB serial begins with `Serial.begin(115200)`; host `config/config.json` also specifies 115200.

### SA-II: straightforward desired frequency, but check semantics

Current code near `SaEncoding()` line 513:

```cpp
float local_frequ = (float)taxelVal[id] * saParams.slope;
// saParams.slope defaults to 300.f / 255.f
uint32_t dtSaEvents = applyRefractoryPeriod(
    (uint32_t)((1.0f / local_frequ) * 1E3));
```

For the proposed **requested rate**, `f_sa_requested_hz = taxel * saParams.slope`; return **0 for taxel=0** to avoid division by zero. Existing maximum is 300 Hz at taxel 255 (configurable with `CMD_SA_FREQ`). Note: actual legacy emitted spike spacing uses a **3 ms refractory floor** and **integer-truncated milliseconds**, so the realized rate is not necessarily exactly `local_frequ`. Decide whether new telemetry represents (A) requested continuous rate (recommended for direct stimulation) or (B) legacy refractory/quantization-limited effective rate (`1000/dtSaEvents`). Document this choice in firmware/API and test it.

### RA-I: event-count-derived rate is not simply an instantaneous continuous frequency

Current `RaEncoding()` around lines 452–502:

- `abs_change = abs(current_taxel - raParams.prevTaxelVal[id])` (default threshold = **3**).
- If `abs_change >= threshold`, `requested_events = floor(abs_change / threshold)`.
- Earliest eligible spike = `max(lastTime + 1, raParams.lastEventTime[id] + refPeriod)` with default `refPeriod=3 ms`.
- If eligible: `max_events = floor((currentTime - first_allowed) / refPeriod) + 1`; `nb_events = min(requested_events, max_events)`.
- Place `nb_events` time-stamped spikes within the current sample window; update last-event time to the last placed event; **only update `prevTaxelVal` when the threshold condition is satisfied** (even if no spike fits).

A first parity-preserving rate estimator could return:

```cpp
uint32_t dt_ms = currentTime - lastTime;  // unsigned millis rollover handling
float f_ra_hz = (dt_ms > 0) ? (1000.0f * nb_events / dt_ms) : 0.0f;
```

**But** it needs an explicit policy for state evolution: we can compute `nb_events` without materializing spikes, and update the legacy `lastEventTime` to `currentTime` whenever `nb_events > 0` (because the current algorithm's final placed event is at `currentTime`). Preserve `prevTaxelVal` threshold behavior and the refractory condition initially to permit comparison with the old implementation.

**Open modeling decision:** sample-window event density is a transient **window-average**. It can be zero on alternate samples and reach e.g. 70 Hz for one event across ~14 ms, even when the mechanical signal remains changed. It is not the same as a desired continuous stimulation frequency. Avoid blindly holding or smoothing until the intended RA stimulation behavior is defined. Consider an alternative magnitude/slope-based RA rate later, after comparing against legacy spike counts.

**Edge cases to test:** first sample (currently `lastTime=0`), zero `dt_ms`, `millis()` wrap-around, threshold=0 (command currently allows 0 despite division by threshold!), saturating event counts, refractory constraints, falling and rising values, unchanged values, and whether preserving suppressed changes is desirable.

## Recommended v1 telemetry design (proposed; not implemented)

Use **one atomic snapshot frame per tactile sample** instead of two independent taxel and spike streams, so Python cannot accidentally pair rates from one sample with taxels from another.

| Field | Type | Bytes | Meaning |
| --- | --- | ---: | --- |
| `timestamp_ms` | uint32 BE | 4 | MCU arrival timestamp of completed CAN sample |
| `taxel[12]` | uint8 | 12 | raw tactile values, indices 0–11 |
| `ra_rate_centi_hz[12]` | uint16 BE | 24 | RA-I window rate ×100, rounded/saturated |
| `sa_rate_centi_hz[12]` | uint16 BE | 24 | SA-II requested rate ×100, rounded/saturated |

Tentative frame type **0x05**, total **64-byte payload**, **71 bytes on wire** including existing 7-byte framing overhead; 70 samples/s ≈ **4,970 bytes/s** excluding USB transport overhead. This fits within the nominal 115200 8N1 serial payload budget (~11,520 bytes/s), but actual USB CDC, print traffic, PC scheduling and frame rate must be measured. Using uint16 ×100 means 0–655.35 Hz; **current `CMD_SA_FREQ` accepts up to 1000 Hz**, so this proposal would saturate such configuration. **Resolve before implementing**: either choose integer Hz uint16, fixed-point ×10 uint16 (up to 6553.5 Hz), or uint32 ×100. **Preferred v1: uint16 deci-Hz (×10)** for up to 6553.5 Hz with 0.1 Hz precision; same 64-byte payload. Frame version/type and exact scaling should be frozen together with tests. New firmware should emit only the new snapshot type in rate-streaming mode. Retain old type constants/compatibility temporarily if needed.

Add a `sequence` counter later if timestamp alone does not allow robust loss measurement. Host decoder should enforce exact payload length, verify CRC, and reject unknown/scaling-mismatched protocol versions.

## Suggested firmware changes (not yet made)

1. **Introduce pure rate calculations** `calculateRaRate(...)`, `calculateSaRate(...)` (or per-sample `computeRates()`), returning one value per taxel and updating RA state. Separate rate semantics from serial encoding.
2. In SA-II, output computed desired Hz (0 when raw value is 0); explicitly decide whether refractory clipping belongs in rate output.
3. In RA-I, factor legacy `requested_events` / `first_allowed` / `max_events` logic into a count calculation; convert count / elapsed sample duration to Hz. Preserve per-taxel state behavior for parity tests. Clamp RA threshold to **at least 1** in command handling.
4. **Replace** `transmitTaxelData(); mechanoreceptorEncoding(); sortAndSendEvents();` in `loop()` with compute + single `transmitTactileRates()` call, followed by `updateTime()`. Keep CAN receive/assembly unchanged.
5. Avoid event-array `realloc`, `qsort`, and time-stamp backfilling on the rate path. Confirm no debug text is mixed with framed binary stream after startup.
6. Add host-side reference tests for rates, byte order, scaling, and CRC; build PlatformIO firmware and validate actual output with hardware.

## Python touchpoints, for later increments

- `communication/serial_manager.py`: currently decodes `0x02` and `0x04` to an **unbounded `queue.Queue()`** and handles MCU clock compensation. Add `0x05` decoding with a structured snapshot; preserve raw and compensated timestamps and monotonic PC receipt time. For real-time stimulation, expose an independently **overwriting latest snapshot** rather than relying on the recording queue.
- `read_mcu.py` and `recording/data_recorder.py`: presently record taxels/events separately; add rate snapshot recording, retain each sample for analysis. Avoid verbose per-sample console printing in fast path.
- Matrix32 modules `matrix32/device.py`, `matrix32/ble.py` and `nerve_stimulation/e_matrix32.py`: inspect API/safe frequency range and command completion semantics before building CH1 loop. A single update worker should coalesce superseded values, not queue every 70 Hz reading into a 44 Hz link.
- Consider a timestamp/age guard: on a stale or disconnected MCU/BLE link, **stop or revert to explicitly defined safe stimulation state** rather than continuing the last nonzero rate. Require user-supplied stimulation amplitude, pulse width and device safety limits; no stimulation has been tested here.

## Acceptance criteria for increment (i)

- Each complete valid CAN sample yields **exactly one** well-formed, CRC-valid snapshot (no event packets), with 12 raw + 12 RA + 12 SA values and MCU sample timestamp.
- All-zero pressures produce zero SA rates; RA rates respect intended threshold and refractory semantics; zero elapsed time cannot divide by zero.
- High SA configured values do not silently overflow/saturate wire encoding.
- No dynamic spike buffers or sorting in rate-streaming path.
- Serial receiver can be updated without ambiguity about version, field order, units or frame length.
- Verified through offline/reference tests, PlatformIO build, then MCU+MTB4 hardware capture.

## Questions for next iteration

1. Should output SA-II rate reflect **requested** `taxel * slope` or the refractory-limited/quantized realized frequency? Recommended: requested rate.
2. Should RA-I mean **legacy event density per sample** (best for parity), or a newly designed continuous stimulation-rate mapping? Recommended: begin with parity and plot results.
3. Confirm **uint16 deci-Hz** encoding and single `0x05` snapshot frame.
4. Should old frame `0x02` remain available behind a compile-time mode for transition, or be replaced outright?

## Key source links

- [MCU firmware](../adafruit_feather_m4/src/main.cpp)
- [PlatformIO configuration](../adafruit_feather_m4/platformio.ini)
- [Python serial parser](../communication/serial_manager.py)
- [Python reader](../read_mcu.py)
- [Recorder](../recording/data_recorder.py)
- [Matrix32 driver](../matrix32/device.py)
