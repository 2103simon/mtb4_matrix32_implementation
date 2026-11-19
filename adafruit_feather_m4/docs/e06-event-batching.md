# E06 — K2 event-batch integrity (transport change)

**Status:** firmware transport fix implemented for independent review and operator
hardware verification. **Full E06 scientific status remains NOT_VERIFIED.**

## Root cause

Previously, `sortAndSendEvents()` packed a single
`FRAME_TYPE_EVENTS=0x04` frame with a fixed 256-byte payload. Once it
contained 63 event records, the code executed `break` and then cleared
the *entire* event buffer. All remaining events in that sample were
silently discarded. The original bytes cannot be reconstructed by the host.

## New production packetization

`FalangeM4/src/event_transport.cpp` is compiled as part of the normal
Feather M4 PlatformIO firmware, not a second test-only implementation.
`sortAndSendEvents()` in `src/main.cpp` passes its real event array to
this transport helper and only clears the buffer after the returned
serialized-record count equals the queued-record count.

For each completed taxel sample, the packetizer:

1. Sorts the events by unsigned millisecond distance from the
   **previous sample time**, preserving chronological order across a
   normal uint32 `millis()` rollover.
2. Starts a `0x04` payload with the 32-bit big-endian timestamp of the
   first event in that frame.
3. Packs `(source:uint8, channel:uint8, delta_ms:uint16 BE)` records
   until reaching **63 records** (`4 + 63*4 = 256` payload bytes), or
   until the next event would exceed the unsigned 16-bit timestamp offset.
4. Sends that frame through the **existing** CRC-protected `writeFrame()`
   function and continues from the *same next event* in a fresh frame.
5. Returns the total frame and serialized-record counts so the caller
   can preserve a queue rather than clearing it on an unexpected short
   serialization result.

No wire framing/CRC format, command ACK semantics, RA-I threshold logic or
SA-II frequency logic changed. A full event packet's **frame length** is
257 bytes including the type field, matching the existing MCU/host
`MAX_FRAME_LENGTH=257` limit.

The dynamic event queue previously overwrote its only pointer with a
failed `realloc` result, which could discard already queued events and
dereference `nullptr` later. It now uses a temporary allocation pointer,
preserves queued events on failure, and increments in-memory failure/drop
counters. **These counters are not exported to the host in this revision.**
Allocation can still fail, causing an explicitly counted internal dropped
new event; the streaming protocol offers no USB delivery ACK.

## Hardware-free evidence

`tests/firmware/test_event_batch_transport.py` compiles and executes
production `event_transport.cpp` with a host C++ compiler. Test payloads
are reconstructed into normal CRC-protected `0x04` frames and passed
through the existing `FalangeFrameDecoder` Python implementation.

The matrix includes 0, 1, 62, **63, 64**, 125, 126, 127, 504 and
**1,000** events with multiple channels and event types, identical
timestamps, unsorted input, fragmented USB reads, the 65535-ms offset
limit, and 32-bit clock rollover. The test asserts exact preservation of
the full event multiset (including duplicates), valid CRC and absence of
parser errors, with frames never exceeding 63 records.

A successful **Feather M4 compile-only** build and full hardware-free
regression suite are prerequisites for operator flashing. CI does not
flash hardware or use an Omega robot.

## E06 hardware prerequisite: MTB4 discovery bounds

Hardware A/B/A/B testing on 2026-11-13 identified an independent
pre-existing startup defect: an eight-byte **periodic skin** CAN
message from `0x4C0` was mistaken for a discovery response and copied
starting at `MTB4.bytes[1]` into a six-byte union, writing three bytes
beyond its capacity. Clean firmware with
the unsafe copy produced no valid taxel frames and 0/3 read-only
GET_CONFIG ACKs. The bounded-copy diagnostic produced 896 taxel frames,
3/3 ACKs and zero CRC/parser errors, then failure returned after
reverting the unsafe copy and communication recovered upon restoring
the bounded copy.

This production-fix branch retains the verified bounded receive-loop
semantics: consume all CAN reply bytes, store only the five metadata
fields that fit, and preserve the CAN-derived ID in index 0.
`tests/firmware/test_mtb4_discovery_bounds.py` compiles the **actual**
`configMTB4()` function with mocked hardware and guard bytes to
exercise the 8-byte reply, 4-byte bootloader reply, no reply, and
oversized payload. The test does not need a physical device.

The hardware A/B/A/B used **diagnostic** builds with WFI and LED
heartbeat disabled. This branch retains normal production WFI and
does not carry diagnostic startup checkpoints or heartbeat/ASCII
instrumentation. The **uninstrumented production image must still
pass an operator-controlled board smoke test**, including 3/3
GET_CONFIG ACKs, expected taxel throughput, and a clean decoder,
before the 30-minute E06 scientific acceptance attempt. The original
K2 loss-accounting limitations above remain open.

## Follow-up: validate the CANLoader discovery reply before configuring skin

The 2026-11-13 investigation of the eight-byte `0x4C0` message
identified that CAN class `0x400` is **periodic skin telemetry**,
not a bootloader/discovery response. The original `configMTB4()`
broadcast (`0x70F`, payload `0xFF`) was already correct, but
its single `CAN.parsePacket()` blindly treated the first CAN frame
as discovery metadata. Thus an ordinary `0x4C0` frame could set the
board address and overwrite the six-byte metadata union.

This **separate unmerged follow-up branch** preserves the working
bounded-copy production branch and changes the acceptance policy:
after each correct `0x70F/0xFF` request, inspect CAN messages for
up to 500 ms; accept only class `0x700` replies addressed to the host,
nonzero source board address, matching `0xFF` discovery command,
length **5** for application or **4** for bootloader, and supported
MTB4 family board-type codes (11, 16, 20, as listed in
`robotology/icub-firmware-shared/can/canProtocolLib/iCubCanProto_types.h`).
Discard irrelevant CAN traffic without using it as metadata. Retry the
request up to three times. A bootloader reply marks warning and
prevents stream configuration; timeout marks error and also prevents
stream configuration. Only a validated five-byte application reply
may supply the board ID used by `startMTB4stream()`.

The original WFI idle path, RA-I/SA-II equations, CAN interrupt queue,
event batching and binary USB framing remain unchanged.
`tests/firmware/test_mtb4_discovery_bounds.py` compiles the
actual production `configMTB4()` against a mock CAN controller and
tests noise, wrong class/recipient/command/type/length, accepted
application responses, bootloader-only, timeout/retries and guarding.

**Hardware smoke gate PASSED** on the uninstrumented validated-discovery
image (see result below). The exact valid discovery reply bytes were
not independently captured, so detailed board-reply provenance and
physical negative-case validation remain open. Keep PR #12 as the
known-working bounded-copy rollback option until release approval.

## Validated discovery: physical Feather M4 smoke test PASS (2026-11-13)

An operator built and uploaded the uninstrumented validated-discovery
firmware from `d9302cd` in the dedicated
`~/code/biointernect-edpr-e06-validated-discovery` worktree using
`platformio run -e adafruit_feather_m4_can -t clean`, build and
upload. The local build reported 4,364 bytes RAM, 26,476 bytes flash,
and `Verify successful` after writing all 52 flash pages.

The operator observed a **steady green LED**. A subsequent 13-second
continuous USB CDC test found the Feather on `/dev/ttyACM1`
(Adafruit VID/PID 0x239A/0x80CD) and sent three **read-only**
GET_CONFIG commands at elapsed seconds 3, 6, and 9.

Observed: **131,476 USB bytes, 896 taxel frames, 898 event frames,
3/3 successful GET_CONFIG ACKs** (command 4, status 1, applied value 0),
zero CRC mismatch candidates, zero invalid length headers, and
zero discarded bytes. Approximate observed taxel rate:
896 / 13 = **68.9 Hz**.

This hardware smoke test demonstrates that the validated discovery
policy accepts a compatible real MTB4 response and allows subsequent
telemetry and bidirectional USB communication. It does not directly
archive the discovery reply's raw bytes; the bootloader, wrong-board,
and no-response branches remain **device-free tested only**. It is
not the 30-minute E06 scientific acceptance test and does not
establish source-to-host losslessness.

The previously hardware-validated bounded-copy production image in
draft PR #12 (`dafb72a`) remains available as a rollback reference.
No automatic merge or release is implied.

## Repeated RESET/startup validation: 3/3 PASS (2026-11-13)

Operator confirmed **three independent physical MCU RESET sequences**, not
three acquisitions under one startup. On each repetition the operator pressed
RESET, waited for a steady-green LED, ran the same 13-second USB probe, and
checked three successful read-only GET_CONFIG acknowledgments before resetting
again.

| Metric | RESET 1 | RESET 2 | RESET 3 |
| --- | ---: | ---: | ---: |
| Taxel frames | 896 | 897 | 896 |
| Event frames | 596 | 801 | 260 |
| USB bytes | 33,922 | 37,656 | 25,278 |
| GET_CONFIG ACKs | 3/3 | 3/3 | 3/3 |
| CRC mismatch candidates | 0 | 0 | 0 |
| Invalid length headers | 0 | 0 | 0 |
| Discarded bytes | 0 | 0 | 0 |
| LED after startup | steady green | steady green | steady green |

Totals: **2,689 taxel frames; nine successful read-only ACKs; no detected
framing/CRC/discard issues**. This closes the *normal-operation repeated-startup
smoke gate* for validated discovery on the physical Feather M4/MTB4.
Failure-mode hardware checks (no board/wrong response/bootloader) and the
30-minute E06 acquisition/loss-accounting acceptance remain open. This
does not establish end-to-end source-to-host losslessness.

## Follow-up: physical no-contact E06 trial (after authorized flash)

Do not overwrite earlier E03/E04 captures or infer they used this
firmware change. Close FalangeGUI and all serial monitors: the single-owner
read-only capture deliberately **does not write configuration commands**.
Maintain the existing applied configuration unless separately verified.

From the approved isolated E06 fix branch in the `tactile` environment,
after operator-confirmed board flash, use a unique trial ID:

```bash
workon tactile
python -m experiments.mcu_validation.acquisition.serial_capture \
  --port /dev/ttyACM0 \
  --trial-id E06_NOCONTACT_30M_001 \
  --seconds 1800 \
  --confirm-gui-closed

python -m experiments.mcu_validation.acquisition.verify_serial_capture \
  experiments/mcu_validation/data/E06_NOCONTACT_30M_001
```

Archive the original raw data and hashes. Record firmware branch/revision,
operator-observed flash status, trial/port, device power sequence and
any non-contact test fixture. The raw capture can verify USB stream
integrity and frame counts but **cannot distinguish source CAN losses**
from slower sensor sampling without MCU/source counters.

Do not intentionally cause large physical contact-force spikes merely to
exercise K2: the deterministic native stress tests cover >63 records
without robot or physical loading.

## E06 30-minute no-contact raw USB capture: integrity PASS (2026-11-13)

The operator captured a unique 1,800-second read-only USB session from the
previously flashed, hardware-validated `d9302cd` firmware
(validated CANLoader discovery and bounded MTB4 metadata copying). The
capture ran from the isolated validated-discovery worktree with the
`tactile` virtual environment, without sending MCU commands.

- Trial: `E06_NOCONTACT_30M_20261113_095307`
- Local archive:
  `experiments/mcu_validation/data/E06_NOCONTACT_30M_20261113_095307/`
  beneath the operator's validated-discovery worktree.
- Acquisition result: `capture_state=CAPTURED`,
  `scientific_status=NOT_VERIFIED`; requested duration **1,800 s**.
- Raw received USB bytes: **5,253,318**, in **297,666** serial read chunks.
- Valid CRC-decoded frames: **211,935**, comprising **124,108 taxel**
  frames and **87,827 event** frames; **358,184 event records**.
- Decoder anomalies: zero CRC-mismatch candidates, invalid length
  headers, invalid payload frames, unknown frame types, event-ID
  anomalies, discarded bytes, and incomplete-header resyncs.
- `unparsed_tail_bytes=1` at acquisition stop, consistent with
  stopping between streaming frame bytes. Exact frame accounting:
  `124108*23 + 87827*11 + 358184*4 + 1 = 5253318` bytes.
- Independent `verify_serial_capture` result:
  **`integrity_status=PASS`**, **211,935** frames,
  **5,253,318** raw bytes, **297,666** read chunks and **124,108**
  taxel frames independently verified.
- Mean host-observed taxel rate across the requested duration:
  **approximately 68.95 Hz**; this mean alone cannot exclude
  intermittent losses or variations in source sampling.
- `command_frames=0`, `ack_frames=0`: expected for a read-only
  capture with no configuration commands.

**Interpretation:** The 30-minute host-side raw archive and
CRC/framing/decoder integrity checks PASS. The one trailing byte is
not counted as a valid frame and is consistent with the fixed stop
boundary. The archive is **local to the operator** and has not been
uploaded to GitHub for independent inspection in this review.
These results do **not** establish end-to-end zero loss:
`can_source_packet_loss=NOT_VERIFIED`,
`clock_mapping=NOT_VERIFIED`, and
`scientific_status=NOT_VERIFIED`. Independent sensor/CAN-source
counters and other end-to-end transport evidence are still needed.

## Unresolved scientific gate

Passing the native packetizer, CRC decoder and a 30-minute host capture
does **not** prove end-to-end 0-loss operation. The E06 acceptance matrix
also requires independent accounting of CAN reception/overflow,
malformed frames, sample-assembly rejection, source-side transmitted
frames, any event allocation failures, USB delivery and genuine source
packet loss. The current MCU state contains several counters
(`canRxOverflowCount`, `canRxMalformedCount`,
`eventAllocationFailureCount`, `eventAllocationDroppedCount`,
`eventRecordsSerializedCount`) but they are **not yet externally
observable** in an authenticated live snapshot.

For full scientific qualification, design a separate versioned diagnostic
command/frame with explicit host support and test it, then run sustained
repeatable physical acquisition and report verified counter differences.
Do not silently relabel the scientific E06 result as PASS.
