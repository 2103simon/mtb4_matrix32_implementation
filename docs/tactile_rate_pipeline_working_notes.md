# Tactile-to-Matrix32 latest-sample pipeline — working notes

Status: **increment (i) firmware implemented on branch, not yet built or hardware-tested**. Python decoder has not been updated.  
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

### Decision recorded 2026-11-18: NO MCU refractory cap

The user confirmed that all MCU-transmitted spike rates must be **unconstrained by refractory-period limits**. The Python stimulation layer will later enforce a configurable safe output-frequency limit.

- **SA-II:** `f_sa_hz = taxelVal[id] * saParams.slope` (`300 / 255` Hz per count by default; configurable to 1000 Hz max). No MCU refractory clipping.
- **RA-I:** let `delta = abs(taxelVal[id] - prevTaxelVal[id])`. For delta at/above threshold, `requested_spikes = floor(delta / threshold)` and advance `prevTaxelVal`; otherwise zero requested spikes and retain the old reference value. `f_ra_hz = requested_spikes * 1000 / dt_ms` with `dt_ms` the elapsed time since the preceding complete sample. First sample and zero `dt_ms` yield 0 Hz; threshold command clamps threshold to at least 1. **No MCU per-taxel refractory state, event placement, or clipping.** This is a threshold-derived requested spike density, not parity with prior refractory-filtered event output.
- The RA-I rate is transient and can fluctuate sharply across samples. Evaluate actual recordings before deciding on any smoothing/holding logic in Python.
- A `CMD_REF` command remains accepted for backwards compatibility but its value does **not** affect transmitted rates.

### Firmware implementation (commit `c0b6e18`)

Changed `adafruit_feather_m4/src/main.cpp`: keeps interrupt-driven CAN reception, two-part MTB4 sample assembly, CRC-framed command handling, and timestamping. Removes event generation, event buffering, dynamic allocation and sorting. Replaces taxel/event packets with exactly one **0x05** snapshot per complete sample. No physical stimulation is controlled by this firmware.

#### Wire format (implemented, not yet verified on hardware)

Outer frame: `length_u16_BE = 1 + payload_len`, `type_u8 = 0x05`, payload, `crc32_u32_BE` over type + payload.

| Offset | Field | Type | Size |
| --- | --- | --- | ---: |
| 0 | MCU complete-sample timestamp | uint32 BE, milliseconds | 4 |
| 4 | taxel[0..11] | 12 × uint8 | 12 |
| 16 | RA-I requested rate[0..11] | 12 × uint32 BE, centi-Hz | 48 |
| 64 | SA-II requested rate[0..11] | 12 × uint32 BE, centi-Hz | 48 |

Payload = **112 bytes**, total framed = **119 bytes/sample**. ~70 Hz yields ~**8,330 bytes/s**, below the ideal 115200 8N1 limit of 11,520 bytes/s, but USB serial behavior and actual throughput must be verified. Chose uint32 centi-Hz instead of uint16 deci-Hz to retain 0.01 Hz resolution and safely represent RA-I transients above 6553.5 Hz (for short sample intervals). Normal recordings may not require this much precision; optimize later based on measured load.

The Python `SerialManager` currently recognizes only `0x02`, `0x04` and `0x11`; this firmware's new `0x05` telemetry will **not be consumed** until increment (ii).

### Verification remaining

- [x] Firmware refactored and pushed to feature branch.
- [ ] PlatformIO compilation.
- [ ] Offline checks of first sample, dt=0, threshold changes, RA step changes, SA=0 / SA=255, high configured SA rate, and MCU millis wrap.
- [ ] Hardware capture verifying 0x05 frames, CRC, throughput and latency.
- [ ] Host parser and storage (increment ii).
- [ ] Safe Matrix32 CH1 command pipeline (increment iii).

## Python touchpoints, for later increments

- `communication/serial_manager.py`: currently decodes `0x02` and `0x04` to an **unbounded `queue.Queue()`** and handles MCU clock compensation. Add `0x05` decoding with a structured snapshot; preserve raw and compensated timestamps and monotonic PC receipt time. For real-time stimulation, expose an independently **overwriting latest snapshot** rather than relying on the recording queue.
- `read_mcu.py` and `recording/data_recorder.py`: presently record taxels/events separately; add rate snapshot recording, retain each sample for analysis. Avoid verbose per-sample console printing in fast path.
- Matrix32 modules `matrix32/device.py`, `matrix32/ble.py` and `nerve_stimulation/e_matrix32.py`: inspect API/safe frequency range and command completion semantics before building CH1 loop. A single update worker should coalesce superseded values, not queue every 70 Hz reading into a 44 Hz link.
- Consider a timestamp/age guard: on a stale or disconnected MCU/BLE link, **stop or revert to explicitly defined safe stimulation state** rather than continuing the last nonzero rate. Require user-supplied stimulation amplitude, pulse width and device safety limits; no stimulation has been tested here.

## Acceptance criteria for increment (i)

- Each complete valid CAN sample yields **exactly one** well-formed, CRC-valid snapshot (no event packets), with 12 raw + 12 RA + 12 SA values and MCU sample timestamp.
- All-zero pressures produce zero SA rates; RA rates follow threshold-count / sample-duration semantics **without** a refractory cap; zero elapsed time cannot divide by zero.
- High SA configured values and short-interval RA transients do not overflow the uint32 centi-Hz encoding.
- No dynamic spike buffers or sorting in rate-streaming path.
- Serial receiver can be updated without ambiguity about version, field order, units or frame length.
- Verified through offline/reference tests, PlatformIO build, then MCU+MTB4 hardware capture.

## Questions for next iteration

1. Verify the unconstrained RA-I request-rate definition against real recordings (it deliberately differs from old refractory-limited spike density).
2. Confirm whether 0.01 Hz precision and the larger 119-byte frames are warranted after throughput tests.
3. Define the Python stimulation refractory/frequency cap and stale-input behavior before any CH1 stimulation.

## Key source links

- [MCU firmware](../adafruit_feather_m4/src/main.cpp)
- [PlatformIO configuration](../adafruit_feather_m4/platformio.ini)
- [Python serial parser](../communication/serial_manager.py)
- [Python reader](../read_mcu.py)
- [Recorder](../recording/data_recorder.py)
- [Matrix32 driver](../matrix32/device.py)
