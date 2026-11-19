# E06 v1 periodic MCU diagnostics — 10-second status, read-only capture

**Status:** Draft unmerged firmware change in PR #14. The operator completed
a hardware smoke and 30-minute diagnostic recording on 2026-11-13 (results
below). Physical firmware commit readback, independent MTB4 source counts,
and calibrated MCU/host clock comparison remain unverified. The previously
hardware-validated bounded discovery remains the rollback version.

## Why

The prior 30-minute read-only acquisition
`E06_NOCONTACT_30M_20261113_095307` (flashed firmware `d9302cd`)
captured 124,108 taxel frames, 87,827 event frames, 358,184 event records
and 5,253,318 raw USB bytes; its archive integrity verified PASS.
It did NOT expose internal ISR/software-queue/event losses or prove
MTB4-to-Feather CAN source count. The Feather requests a nominal 10-ms
(100-Hz) skin transmission interval, while the host recorded ~68.95 Hz
averaged over 1,800 s. This difference is not itself a proven loss count.

## Wire protocol (v1, completely separate from CAN)

A normal existing USB CRC-framed packet with frame type `0x12` every
~10 seconds of MCU uptime. **This is not a CAN command or CAN ID.**
The standard frame envelope remains
`[length:u16 BE][type=0x12][payload][CRC32:u32 BE]`,
CRC32 over `type + payload`.

Payload length is exactly **58 bytes** and total frame length **65 bytes**:

| Offset | Width | Meaning |
|---|---:|---|
| 0 | 1 | Schema version = 1 |
| 1 | 1 | Counter count = 12 |
| 2 | 4 | Diagnostic snapshot sequence, uint32 BE, starts at 0 |
| 6 | 4 | MCU uptime in millis(), uint32 BE |
| 10 onward | 12 × 4 | Cumulative uint32 BE counters in order below |

Index and canonical Python field name:
1. `can_rx_observed`: every CAN ISR invocation (not necessarily every MTB4-source transmission)
2. `can_rx_queued`: valid ISR frames committed to the 32-slot queue
3. `can_rx_malformed`: frames rejected by ISR payload-length checks
4. `can_rx_queue_overflow`: frames dropped because the queue was full
5. `can_rx_dequeued`: frames taken from queue by foreground
6. `can_rx_sequence_missing`: missing ISR sequence numbers observed by foreground
7. `taxel_samples_completed`: complete matching two-part taxel samples
8. `event_records_enqueued`: successful event records added to local RAM buffer
9. `event_records_serialized`: event records passed to USB frame writer
10. `event_frames_serialized`: event frames passed to USB frame writer
11. `event_allocation_failures`: failed RAM reallocation calls
12. `event_records_dropped`: event records not enqueued due to allocation failure

All counts reset on MCU restart and wrap modulo 2^32. The status snapshot
is generated in the foreground after queued CAN messages are processed.
ISR counters are snapshotted in a short interrupt-disabled section;
USB write occurs **outside** that section.

First status is approximately 10 seconds after the first foreground loop;
then approximately every 10 seconds, with normal main-loop scheduling
jitter. The frame itself carries a sequence number to identify gaps.
At 65 bytes per 10 seconds, added wire traffic is ~11.7 kB per 30 min.

The existing GUI implementation already consumes CRC-valid unknown serial
frame types without desynchronizing; the `FalangeFrameDecoder` now
identifies `0x12` as `kind="diagnostics"`, retaining v1 counter names.
The raw recorder remains **read-only** and archives status frames in
`serial_frames.jsonl`, included in its normal byte hashes and replay.

## Stage A — Analyze the previous 30-minute archive without hardware

Run from the isolated diagnostics worktree after `workon tactile`:

```bash
python -m experiments.mcu_validation.acquisition.analyze_taxel_timing \
  ~/code/biointernect-edpr-e06-validated-discovery/experiments/mcu_validation/data/E06_NOCONTACT_30M_20261113_095307
```

Review median, p95/p99, maximum and >20/>30/>50/>100 ms gaps.
Taxel MCU timestamps mark ISR CAN receipt, **not** original MTB4
transmission; neither this analysis nor a 100-Hz setting proves source loss.

## Stage B — Operator flash after CI and review

Use a fresh worktree (not the validated-discovery worktree); never merge
PR #14 or flash the board without explicit operator approval.

```bash
workon tactile
cd ~/code/biointernect-edpr-e06
git fetch origin
git worktree add --detach ~/code/biointernect-edpr-e06-diagnostics \
  origin/fix/mcu-e06-periodic-diagnostics-20261113
cd ~/code/biointernect-edpr-e06-diagnostics/FalangeM4
git rev-parse --short HEAD
platformio run -e adafruit_feather_m4_can -t clean
platformio run -e adafruit_feather_m4_can
platformio run -e adafruit_feather_m4_can -t upload
```

Check clean build, upload verification, then green LED and short smoke
showing ordinary 0x02 taxels, 0x04 events, and at least one 0x12
diagnostic snapshot. Do not continue if the diagnostic frame is absent,
malformed, or changes the expected ordinary USB behavior. The old
`~/code/biointernect-edpr-e06-production-bounds` and
`~/code/biointernect-edpr-e06-validated-discovery` retain the
known-working binaries/sources for an approved rollback.

## Stage C — new 30-minute run (after positive smoke)

Close FalangeGUI and serial monitors. Maintain the established
no-contact fixture. Use a unique trial ID **not** the old one:

```bash
workon tactile
cd ~/code/biointernect-edpr-e06-diagnostics
TRIAL_ID="E06_DIAG_NOCONTACT_30M_$(date +%Y%m%d_%H%M%S)"
python -m experiments.mcu_validation.acquisition.serial_capture \
  --port /dev/ttyACM1 \
  --trial-id "$TRIAL_ID" \
  --seconds 1800 \
  --confirm-gui-closed

python -m experiments.mcu_validation.acquisition.verify_serial_capture \
  "experiments/mcu_validation/data/$TRIAL_ID"

python -m experiments.mcu_validation.acquisition.analyze_taxel_timing \
  "experiments/mcu_validation/data/$TRIAL_ID"

python -m experiments.mcu_validation.acquisition.analyze_diagnostics \
  "experiments/mcu_validation/data/$TRIAL_ID"
```

`analyze_diagnostics` compares only host frames enclosed by consecutive
diagnostic snapshots and checks status sequence gaps and MCU ISR counter
conservation. A result `PASS_MCU_TO_HOST_WINDOWS_ONLY` is **not**
a claim of end-to-end zero loss. First/last partial windows remain
unchecked, USB writes are counted as serialized rather than proven delivered,
independent MTB4 CAN source transmission totals are unavailable, and
host/MCU clocks are not synchronized. Keep `scientific_status=NOT_VERIFIED`.

## Operator results: baseline timing, 35-second smoke and instrumented 30 minutes (2026-11-13)

These results were supplied by the hardware operator as command output.
The original raw trial directories remain **local to the operator**
and were not uploaded for independent inspection here. The 0x12
telemetry proves the new diagnostics code was active; a specific
flashed binary commit hash has not been independently read back.

### Old recording: unchanged baseline timestamp analysis

Trial `E06_NOCONTACT_30M_20261113_095307` from the
validated-discovery firmware: **124,108** valid taxel frames,
**124,107** intervals. Raw MCU timestamp delta median/p95/p99:
**15/15/15 ms**; min/max **14/15 ms**. Counts:
15ms=**68,625**, 14ms=**55,482**, >20ms=**0**,
MCU clock reset/rollovers=**0**. Sum of MCU sample intervals
**1,806.123 seconds**, versus host first-to-last taxel receipt span
**1,799.995265574 seconds**. Source transmission timestamps are
not observed, so neither the apparent ~68.7Hz effective MCU cadence
nor the ~6.13s MCU/host span difference proves source loss or clock
drift.

### Instrumented 35-second hardware smoke

Trial `E06_DIAG_SMOKE_20261113_105352`: read-only capture
`CAPTURED` and independent raw archive integrity **PASS**.
**79,888 raw bytes**, **5,814 read chunks**, **3,815** CRC-valid
frames: **2,414 taxel**, **1,398 event**, **3 versioned diagnostics**
(`0x12`); **2,198 event records**. Zero CRC-mismatch candidates,
invalid length/payload frames, discarded bytes and unknown types;
one incomplete tail byte at timed stop.

The two fully covered 10-second windows passed MCU/host comparison,
with **0 missing status sequence IDs, 0 count mismatches, 0 ISR
conservation anomalies**, and every MCU-detected internal loss
indicator equal to **zero**. Across those windows,
`can_rx_observed=can_rx_queued=can_rx_dequeued` delta **2,750**,
completed taxel delta **1,375**, serialized event frame delta **808**
and record delta **1,281**.

### New 30-minute no-contact hardware capture

Trial `E06_DIAG_NOCONTACT_30M_20261113_105543`:
requested capture **1,800 seconds**, `capture_state=CAPTURED`,
`scientific_status=NOT_VERIFIED`. Received **4,152,680 raw USB
bytes** across **304,026 read chunks**, **191,075** CRC-valid
frames, including **124,111 taxel frames**, **66,783 event frames**
and **181 diagnostic frames**, with **137,937 event records**.
Parser reported **zero** CRC-mismatch candidates, invalid length
headers, invalid payload frames, discarded bytes, event ID anomalies
and unknown frame types; timed shutdown left **one unparsed tail
byte**.

Independent `verify_serial_capture` reported
**`integrity_status=PASS`**, revalidating **191,075** frames,
**4,152,680** raw bytes and **304,026** read chunks.

MCU timing analysis: **124,110** intervals, min/max **14/15ms**,
median/p95/p99 all **15ms**, counts **55,593 × 14ms** and
**68,517 × 15ms**, **zero intervals >20ms** and **zero clock
regressions/wraps**. MCU sample timestamp span
**1,806.057 seconds**; host first-to-last sample receipt span
**1,799.972756348 seconds**. The repeated ~6.08s
MCU-to-host span mismatch requires separate clock/buffer analysis;
do not claim calibrated clock drift on this evidence alone.

### Periodic diagnostic MCU/host comparisons

- **181** versioned `0x12` snapshots, sequence **11..191**
  inclusive, **180 consecutive windows** spanning cumulative
  MCU uptime `126,534..1,926,535 ms`; largest status interval
  **10,001 ms**.
- Analyzer status: **`PASS_MCU_TO_HOST_WINDOWS_ONLY`**;
  **0 missing diagnostic sequences**, **0 host/MCU mismatches**,
  **0 CAN ISR counter-conservation anomalies**.
- Covered-window MCU deltas:
  `can_rx_observed=can_rx_queued=can_rx_dequeued=247,388`;
  `taxel_samples_completed=123,694`;
  `event_records_enqueued=event_records_serialized=137,565`;
  `event_frames_serialized=66,562`.
- Every reported covered-window internal-loss increment was
  **zero**: CAN malformed, queue overflow, ISR sequence gaps,
  allocator failures and dropped event records.
- Exactly **two processed CAN frames per assembled taxel sample**
  across the covered windows (`247388 = 2 * 123694`).
  This establishes consistency of the documented two-part sample
  stream **at the MCU**, not independent MTB4 transmission totals.

### Acceptance and remaining boundaries

**PASS:** sustained 30-minute host archive integrity, status delivery,
per-window MCU-to-host taxel/event record matching, detected CAN ISR
queue/foreground event-loss counters, zero observed >20ms MCU
taxel receipt gaps, unchanged effective ~69Hz cadence.

**NOT VERIFIED:** total CAN frames independently transmitted by the
MTB4 (including hardware-level losses before ISR), explicit USB
`Serial.write` completion or delivery counters, true MTB4
effective rate vs requested 10ms parameter semantics, calibration
between the host and MCU clocks, and leading/trailing partial
capture windows. Different numbers of generated events between
no-contact trials do **not** establish dropped events; the
instrumented run shows event enqueued/serialized accounting
matching within covered windows.

Do not upgrade `scientific_status=NOT_VERIFIED` to PASS and do
not merge PR #14 without explicit operator approval.


## Offline clock-domain analysis of the instrumented 30-minute capture (2026-11-13)

This section records **operator-supplied JSON output** from the committed
read-only analyzer
`experiments/mcu_validation/acquisition/analyze_clock_domains.py`.
The raw archive remains on the operator's workstation, not uploaded for
independent analysis. This run did not access serial hardware or flash an MCU.

Trial `E06_DIAG_NOCONTACT_30M_20261113_105543`:

| Metric | Taxel timestamp endpoints | Diagnostic uptime endpoints |
|---|---:|---:|
| Samples | 124,111 | 181 |
| MCU elapsed time | 1,806.057 s | 1,800.001 s |
| Host read-chunk receipt time | 1,799.972756348 s | 1,793.937700033 s |
| MCU minus host elapsed time | 6.084243652 s | 6.063299967 s |
| Apparent MCU-fast ratio | +3,380.186523 ppm | +3,379.883241 ppm |
| MCU clock regressions | 0 | 0 |

The independent diagnostic fit on 181 statuses (sequence 11..191, no
missing statuses) produced `host_seconds_per_mcu_second=0.9966426542206914`,
equivalent to an **apparent MCU-fast +3,368.655521 ppm**; fit RMS residual
**8.989682 ms**, absolute-residual p95 **20.101763 ms**, residual range
**[-12.087078, +30.325006] ms**. The difference between taxel and
diagnostic *endpoint* ratios was **0.303282 ppm**. The difference between
taxel endpoint ratio and diagnostic *regression fit* ratio was
**11.531002 ppm**. Diagnostics MCU uptime minus preceding serialized
taxel CAN-ISR timestamp: median **8 ms**, p95 **14 ms**, max **15 ms**.

**Interpretation:** These two independent MCU timestamp streams, compared
with host read-chunk completion timestamps, show highly reproducible
apparent clock-domain rate disagreement near **0.338%**. This strengthens
the hypothesis of a common clock-rate discrepancy and makes short-lived,
random USB buffering an insufficient sole explanation. It is **not**
independent calibration of either clock: systematic changes in USB
buffering/scheduling and the host receipt-time attribution remain possible.
Neither the observed +3,369..3,380 ppm offset nor a linear fit proves
source sample loss, USB delivery latency, or actual MTB4 sampling rate.
Keep `clock_mapping=NOT_VERIFIED` and `scientific_status=NOT_VERIFIED`.

**MTB4 rate investigation (code review only):** The Feather requests
`SKIN_SET_BRD_CFG` (`0x4D`) with period byte `0x0A` = 10 ms. Upstream
Robotology CAN command decoding converts that byte from milliseconds
to microseconds, and an upstream skin implementation configures an OS
periodic timer with that `txperiod` then calls `ad7147_acquire()`
before packaging two CAN frames for each enabled triangle. The observed
14/15 ms intervals with no >20 ms gaps are consistent with an effective
~69 Hz CAN-ISR receipt cadence, but are not proof of a physical MTB4
source-transmission rate. The exact MTB4 application and hardware revision
running on the operator's board has **not** been established; do not
treat current upstream code as proven flashed firmware.

Upstream code references:
- `robotology/icub-firmware`:
  `emBODY/eBcode/arch-arm/embot/prot/can/embot_prot_can_analog_polling.cpp`
  (`Message_SKIN_SET_BRD_CFG::load`).
- `robotology/icub-firmware`:
  `emBODY/eBcode/arch-arm/embot/app/embot_app_application_theSkin.cpp`
  (`Impl::start`, `Impl::tick`).

**Next gate without changing firmware:** Identify the actual MTB4 board
application/hardware version already captured by Feather discovery (if
available in operator logs), then inspect that matching upstream
implementation's acquisition/scheduling path and feasible timer period.
Any clock calibration or CAN source instrumentation requires a separate
reviewed protocol and explicit operator approval; do not reflash or repeat
the 30-minute run on these findings alone.


## MTB4 source-side cadence investigation (offline review, 2026-11-13)

**Goal:** Explain why 10 ms requested MTB4 transmission scheduling is
accompanied by approximately 14/15 ms CAN-ISR taxel timestamps (~68.72 Hz
in MCU clock units; ~68.95 Hz by host receipt) without assuming lost
frames. This is a read-only review of upstream source, **not** physical
execution-time instrumentation or verification of the code running on
the operator's board.

### Upstream timing and dataflow evidence

1. The Feather issues `SKIN_SET_BRD_CFG` (`0x4D`) with `0x0A`
   (10 ms). The upstream decoder maps the byte to a time period in
   microseconds (`1000*candata.datainframe[1]`), and the skin
   application starts an OS timer using `boardconfig.txperiod`.
2. The timer action is `EventToThread`, not a queued sample or counting
   semaphore. In the upstream CMSIS-OS2 implementation
   `event_set()` calls `osThreadFlagsSet()`; `event_get()` calls
   `osThreadFlagsWait(..., osFlagsWaitAny, ...)`. Repeated notifications
   of the same flag can coalesce while its handling thread is busy.
   If per-acquisition work exceeds 10 ms, a continuous ~14.55 ms
   processing/receipt cadence is plausible, even though the timer
   requests 10 ms.
3. The upstream MTB4 `theSkin::Impl::tick` calls `ad7147_acquire()`,
   then generates two CAN frames for **enabled** triangles. The
   `ad7147_acquire()` implementation calls `ServiceAD7147Isr(0)`.
   On the MTB4 code path, that service loops across **four I2C
   device addresses**, requesting **12 16-bit registers per address**.
   The board's `ReadViaI2C()` implementation clocks five SDA lanes
   in parallel via software-controlled GPIO and explicit `Wait()`
   calls; `Wait()` calls `embot::hw::sys::delay(1)` regardless of its
   `cycles` argument. Software delay units are microseconds in the
   upstream `embot::core::relTime` API, but real run time also
   includes GPIO, software, scheduler, and CAN work.
   In this code path, disabling all but one transmitted triangle
   does not remove the four-address sensor acquisition.
4. The acquisition-code paths support a **task-overrun / coalesced
   timer-event hypothesis**, NOT an observed 14.55 ms task duration.
   Neither a static delay count nor the host CAN-ISR timestamps
   measure MTB4 task entry/exit. Source-side execution time and
   timer tick counts remain `NOT_VERIFIED`.
5. Board identity matters: upstream board-type IDs are **11 = mtb4,
   16 = mtb4w, 20 = mtb4c**. The Feather discovery code accepts all
   three and stores board type and firmware version in RAM
   (`MTB4.info.BRD/MAJ/MIN/AP`), but `dumpMTB4info()` is only
   called under `COMM_MODE == ASCII_MODE`; the current 10-second
   diagnostic frames do not include that identity. The operator
   has not provided an independently observed board type or firmware
   version. Do not claim that the inspected MTB4 revision is
   necessarily flashed on the physical device.

### Interpretation and next gates

The **exclusive 14/15 ms intervals with zero >20 ms gaps**, and
complete two-frame CAN samples in the covered windows, are more
consistent with a fairly regular slower acquisition/receipt cadence
than random discrete loss from a perfectly periodic 10 ms source.
However, independent MTB4-transmitted frame counts and source-side
timer execution times are unavailable; **source losses are not ruled
out**, and the requested 100 Hz is not yet validated.

The previously observed +0.337% MCU/host apparent clock-rate difference
cannot, on its own, explain the ~45% ratio of 14.55 ms observed to
10 ms requested. Treat these as separate unresolved issues.

Recommended order, without touching the currently validated firmware:

- Identify the actual MTB4 board type and application version from
  **existing** operator startup/diagnostic records if possible.
- Review the matching board revision's acquisition and OS scheduling
  implementation, including sensor-read task execution-time constraints.
- Only if source-level evidence is insufficient, design an
  operator-authorized, bounded hardware measurement (e.g., MCU/source
  timestamping or external CAN observation) on a separate branch.
  Do not infer missing MTB4 transmissions from a nominal requested rate.

Upstream files inspected from `robotology/icub-firmware` (master,
code-index revision `4ee61bd04f3ae1280da7a49236849a6252cd6582`):
`emBODY/eBcode/arch-arm/embot/prot/can/embot_prot_can_analog_polling.cpp`,
`emBODY/eBcode/arch-arm/embot/app/embot_app_application_theSkin.cpp`,
`emBODY/eBcode/arch-arm/board/mtb4/application/src/main-appcan.cpp`,
`emBODY/eBcode/arch-arm/board/mtb4/application/src/others/AD7147.c`,
`emBODY/eBcode/arch-arm/board/mtb4/application/src/others/I2C_Multi_SDA.c`,
`emBODY/eBcode/arch-arm/embot/os/embot_os_Action.cpp`,
`emBODY/eBcode/arch-arm/embot/os/embot_os_rtos.cpp`,
`emBODY/eBcode/arch-arm/embot/os/embot_os_Thread.cpp`,
`emBODY/eBcode/arch-arm/embot/prot/can/embot_prot_can.h`.

**Acceptance remains:** `PASS_MCU_TO_HOST_WINDOWS_ONLY` for internal
accounting; broader `scientific_status=NOT_VERIFIED`.


## MTB4 discovery identity: offline log scan (read-only, 2026-11-13)

The confirmed binary firmware configuration
(`FalangeM4/include/firmware_mode.h`: `COMM_MODE BINARY_MODE`)
uses `configMTB4()` during startup to receive iCub CANLoader
discovery replies. A valid application reply uses CAN ID `0x7<node>0`
(nonzero node), exactly **five bytes**
`FF board_type major minor ap`. The existing `dumpMTB4info()`
would print `ID / Type / V / AP` in **hex**, but is called only
when `COMM_MODE == ASCII_MODE`. The current binary `0x12` periodic
diagnostic payload deliberately has no board identity, and the
`serial_frames.jsonl` read-only capture does not retain the
CANLoader reply. Neither the 30-minute taxel/event stream nor
the status counters can identify the MTB4 hardware variant.

The **offline-only** tool
`experiments/mcu_validation/acquisition/inspect_mtb4_discovery.py`
searches preexisting text startup logs and standard Linux candump text
(`can0 710#FF0B011900` or `can0 710 [5] FF 0B 01 19 00`).
It never opens a CAN/USB device, never requests discovery, never writes
files, and does not search the giant `serial_frames.jsonl` archives.
It recognizes only the board-type codes defined upstream:
**11 (0x0B) MTB4**, **16 (0x10) MTB4W**, **20 (0x14) MTB4C**.
Version field bytes in the ASCII `V: 1.19` output are hexadecimal,
so hexadecimal minor `19` denotes decimal 25; the scanner prints
both raw hex and decimal fields.

Operator command (from diagnostics worktree, after `git pull`):

```bash
python -m experiments.mcu_validation.acquisition.inspect_mtb4_discovery \
  ~/code/biointernect-edpr-e06-diagnostics \
  ~/code/biointernect-edpr-e06-validated-discovery \
  ~/code/biointernect-edpr-refactor
```

An absent worktree path is reported under `missing_roots`; all paths
are read-only. The scan includes .log/.txt/.out/.trace/.jsonl files
up to 8 MiB and skips source files, massive frame archives, links,
build dependencies and virtualenvs. Results include log path and line
number, *not* proof that the same board is connected now.

**Decision:** If a matching discovery record is found, compare its date,
CAN node ID and board-type/version with the actual experiment setup
before making a revision-specific upstream claim. If no record is found,
identity remains `NOT_VERIFIED`; do not guess from the 69 Hz cadence,
GUI log names, or firmware-source defaults. A new board query would
require separately discussed hardware access because opening serial
can reset the MCU.


### Operator offline log-scan result (2026-11-13)

The operator executed the committed `inspect_mtb4_discovery` tool on
three worktree paths. The returned JSON reported:

- `mode=READ_ONLY_OFFLINE`, `status=NOT_FOUND_IN_SCANNED_LOGS`.
- **4** matching log files scanned and **0** recognized MTB4
  discovery identities; no unreadable/oversize files and no scan limit hit.
- The path `~/code/biointernect-edpr-refactor` did **not** exist on the
  operator workstation, so it contributed no evidence.
- `current_physical_board_identity=NOT_VERIFIED` and
  `scientific_status=NOT_VERIFIED` correctly remain unchanged.

This excludes discoverable identities only from the scanned existing
logs, not from all possible historical records and not from the
physical board. In particular, binary-mode USB telemetry does not
carry the startup CAN discovery payload.

**Next non-invasive gate:** broaden the *offline* scan to `~/code`
(or another verified directory holding historical CAN/startup logs):

```bash
python -m experiments.mcu_validation.acquisition.inspect_mtb4_discovery ~/code
```

If no archived discovery identity is found, inspect available host CAN
interfaces with `ip -details link show type can` (and, if needed,
`ip -brief link`). These are read-only host interface queries, not
CAN discovery requests. A Feather appearing as `/dev/ttyACM*`
**does not** by itself provide a SocketCAN interface. Do not open the
Feather serial device, issue CAN commands, reset, reflash or change
MTB4 configuration just to retrieve identity without separately
reviewing the exact procedure and expected operational impact.


## Operator archived MTB4 identity evidence and version-pinned upstream review

**Operator-supplied offline results (2026-11-13):**
`inspect_mtb4_discovery ~/code` scanned **3,305** existing eligible
text logs, skipped **422** oversized files, had no unreadable files or
match-limit condition, and found **one** historical ASCII identity:

```text
source: ~/code/PAL_skin/adafruit_feather_m4_can/log/devttyACM0_2025_12_02.14.38.36.383.txt
line: 4
CAN node ID: 0xC (12)
board type: 0x0B = MTB4 (not MTB4W or MTB4C)
application version bytes: 0x01.0x16 = 1.22 (decimal minor = 22)
AP field: 0x00 (raw field; not evidence of a patch number)
```

This log is dated **2025-12-02** and is not a live readback for the
2026-11-13 E06 experiment. The operator also ran
`ip -details link show type can` (empty) and `ip -br link`, which
listed Ethernet/Wi-Fi/bridge/loopback interfaces but **no SocketCAN
device**. Thus no existing CAN network interface is available on the
operator's laptop for an independent CAN query; the Feather's
`/dev/ttyACM*` USB interface is not SocketCAN.

**Version-pinned source inspection:** Historical upstream Robotology
commit `093c73c14d219f86dcf5f9c8219f30c7a3c2ba54`
(2024-09-04) explicitly declares the *normal, non-TLR, non-fifth-I2C*
MTB4 application as `versionOfAPPLICATION {1, 22, 0}` in
`emBODY/eBcode/arch-arm/board/mtb4/application/src/main-appcan.cpp`.
This is a *plausible source-era match* for historical 1.22; version
bytes alone do not prove binary identity or compilation flags. In this
historical snapshot:

- `Message_SKIN_SET_BRD_CFG::load()` maps CAN period byte to
  `1000 * byte` microseconds, so `0x0A` requests 10 ms.
- `theSkin::Impl::start()` starts a periodic OS timer with
  `boardconfig.txperiod` and an `EventToThread` action.
- `main-appcan.cpp` handles `evt_SKINprocess` through
  `theskin.tick(outframes)`, sharing its event-processing thread with
  other CAN and sensor work.
- `theSkin::Impl::tick()` invokes `ad7147_acquire()` before generating
  two CAN frames for each **enabled transmission triangle**.
- `ServiceAD7147Isr()` still loops over **four** I2C device addresses,
  reading **12 16-bit register values** per address using the
  software-driven multisda driver (multiple SDA lanes parallel per
  addressed device). Thus selecting one transmitted triangle does not
  make the source execute only one triangle's sensor-read work.
- `I2C_Multi_SDA.c` implements `Wait(cycles)` as
  `embot::hw::sys::delay(1)`, with additional software GPIO overhead
  in the byte-transfer loops.
- The OS wrapper uses event flags (`osThreadFlagsSet/Wait` for
  CMSIS-OS2 and `osal_eventflag_set/getany` for OSAL). Repeated
  pending events with the same flag may coalesce: this can explain
  fewer than 100 **completed** acquisition cycles per second if each
  cycle exceeds the requested timer period, without inferring loss
  from MTB4-generated CAN frames.

**Updated hypothesis:** A workload-limited 1.22-era acquisition routine
and event-flag coalescing are consistent with near-continuous 14/15 ms
CAN-ISR sample intervals and host/MCU matched accounting. They do not
prove a 14.5 ms source task duration, source-side no-loss, or that the
2026 hardware still runs 1.22. The apparent +0.337% MCU-host clock
disagreement is separate and cannot explain the ~45% period difference.
Retain `scientific_status=NOT_VERIFIED`.

**Next best non-invasive evidence:** Read the original 2025 log's
startup context (without opening any hardware), for example:

```bash
sed -n '1,30p' ~/code/PAL_skin/adafruit_feather_m4_can/log/devttyACM0_2025_12_02.14.38.36.383.txt
```

This can establish whether the 2025 record came from the same
Feather/skin setup, but **cannot** verify the board physically connected
today. Do not issue a fresh CAN discovery, change timer configuration,
open serial, reset or reflash on the basis of an archived identity.

### Operator archival startup-context verification (2026-11-13)

The operator inspected the first 35 lines of the exact historical
log `~/code/PAL_skin/adafruit_feather_m4_can/log/devttyACM0_2025_12_02.14.38.36.383.txt`
and provided these relevant lines:

```text
[INFO]: Configuring BMA400...connected!
[INFO]: Starting CAN...connected!
[DEBUG]: 7C0 of size 5
[INFO]: MTB4 info > ID: C | Type: B | V: 1.16 | AP: 0
----- Config completed -----
CAN packet size: 8
...
Taxel: 8056
...
Taxel: 8059
```

**Interpretation:** The CAN ID `0x7C0` and five-byte application
reply are consistent with node `0xC` and the Feather's discovery
framing. `Type: B` is hexadecimal `0x0B`, i.e., standard MTB4.
The ASCII printer uses `Serial.print(..., HEX)`; therefore
`V: 1.16` means firmware version bytes `0x01.0x16` and
**decimal version 1.22** (not 1.16). The raw `AP: 0` field does
not on its own establish a patch revision. The subsequent tactile
`Taxel:` lines show that this was an operational old tactile setup,
not merely a discovery-only test. They are **not** proof of any
specific acquisition interval or 100 Hz achieved rate.

The BMA400 initialization line and ASCII debug traces establish that
this log came from the older PAL_skin Feather firmware; the current
E06 binary firmware does not print this startup information. There is
no verified evidence tying the physical MTB4 identified in 2025 to
the 2026 E06 capture, and the 2024 upstream 1.22 source is only a
plausible *firmware-version match*, not binary identity. Keep
`current_physical_board_identity=NOT_VERIFIED` and
`scientific_status=NOT_VERIFIED`.

**Next non-invasive investigation:** Inspect existing
`~/code/PAL_skin/adafruit_feather_m4_can` source and old recording
metadata for (i) the requested MTB4 configuration period and
(ii) any *independently timestamped* sensor CAN arrivals. Such
records could test whether the same 14/15 ms cadence was present
in 2025, but cannot independently establish current MTB4 firmware
identity. Avoid MCU serial access, CAN commands or flashing until
there is a separately reviewed hardware procedure.


### Historical PAL_skin 10 ms too-fast comment (operator source inspection, 2026-11-13)

The operator ran a **read-only grep** on the old, separately maintained
`~/code/PAL_skin/adafruit_feather_m4_can` worktree. At
`src/main.cpp:646` the old firmware defines `startMTB4stream()`,
and its board configuration section contains:

```cpp
// CAN.write(0x0A); // contains the tx rate: 10 ms.  [too fast]
```

The 10 ms write is **commented out** in that historical source snapshot.
This is direct historical evidence that the authors considered the
10 ms (100 Hz requested) configuration too fast, but **not** proof
of its failure mechanism or of which actual interval was selected.
The grep output did not include the active `CAN.write(...)` line
adjacent to the comment. Do **not** claim that the 2025 setup
requested 10 ms, nor infer an old sample cadence from the `Taxel:`
values in the archived startup log.

The next **offline-only** verification is to inspect lines 646..680 of
the operator's old `src/main.cpp`:

```bash
sed -n '646,680p' ~/code/PAL_skin/adafruit_feather_m4_can/src/main.cpp
```

Then distinguish (a) the active requested rate, (b) historical
comments, and (c) E06's separate 10 ms request. If the older setup
uses a slower period, that is consistent with earlier concerns about
100 Hz but does not establish measured MTB4 task execution time.
No hardware interaction or modifications are required. Retain
`scientific_status=NOT_VERIFIED`.


### Historical PAL_skin 26 ms configuration and reported package loss

**Source of evidence:** On 2026-11-13 the hardware operator supplied the
exact contents of lines 646..680 in their local, historically maintained
`~/code/PAL_skin/adafruit_feather_m4_can/src/main.cpp`. The
`startMTB4stream()` method first stops the skin service, then sends
CAN `0x4D` (`SKIN_SET_BRD_CFG`) with compensation byte `0x01`,
**active period `0x1A = 26 ms`**, and the no-load byte. These
alternatives are explicitly commented out in that historical file:

| CAN period byte | Requested timer period | Nominal rate | Historical source comment |
|---|---:|---:|---|
| `0x0A` | 10 ms | 100 Hz | "too fast" |
| `0x10` | 16 ms | 62.5 Hz | "too fast" |
| `0x18` | 24 ms | 41.67 Hz | "limit. still looses every ~5th package" |
| **`0x1A`** | **26 ms** | **38.46 Hz** | **"minimum rate we can use with consistent readings"** (active) |
| `0x20` | 32 ms | 31.25 Hz | "working" |

These are **operator-supplied historical source-code comments** rather
than a verified controlled performance measurement. The comments do
not document a packet-loss denominator, instrumentation, enabled
triangle count, CAN bitrate, or exact MTB4 workload; consequently
they do **not** prove source-side losses or establish a minimum
working transmission period for the current E06 configuration.

**E06 comparison:** Current `FalangeM4/src/main.cpp` requests
`0x0A = 10 ms`, explicitly disables all triangles and re-enables
only `TAXEL_ID = 0` for *transmission*. E06 observed approximately
14/15 ms CAN-ISR taxel timestamp intervals (~68.72 Hz in MCU time),
with no gaps >20 ms, and conserved completed two-CAN-frame samples
in all 180 internally bracketed MCU/host diagnostic windows.
That conservation begins at the Feather CAN ISR; it **cannot**
detect MTB4-generated samples or CAN frames that never reached it.

The historical `0x1A` operation is compatible with earlier concern
about a 100 Hz requested MTB4 rate. A difference in enabled
triangle count or in the bus/sensor task workload could explain
why the historical operator required 26 ms while the current
single-triangle configuration has continuous 14/15 ms arrivals.
This is a **hypothesis** to test, not an established mechanism.

**Next offline check:** The supplied historical snippet ends
midway through the "disable all triangles" command. Read its
remaining configuration to establish the number and identity of
triangles actually enabled:

```bash
sed -n '676,714p' ~/code/PAL_skin/adafruit_feather_m4_can/src/main.cpp
grep -nE 'FIRST_TAXEL_ID|LAST_TAXEL_ID|#define[[:space:]]+TAXEL_ID' \
  ~/code/PAL_skin/adafruit_feather_m4_can/src/main.cpp
```

No USB/CAN access, firmware change or new experiment is justified
from these comments alone; keep `scientific_status=NOT_VERIFIED`.


### Four-triangle historical configuration and source-side CAN TX overflow path

The operator supplied the remainder of the exact historical
`~/code/PAL_skin/adafruit_feather_m4_can/src/main.cpp`:
the old setup first disables triangles `FIRST_TAXEL_ID=0x00`
through `LAST_TAXEL_ID=0x0F`, then enables **0x04..0x07
inclusive (four triangles)** using CAN command `0x50`. Its
active requested period is `0x1A = 26 ms`. By contrast, the
current E06 configuration first disables all sixteen triangles,
then enables only `TAXEL_ID=0x00` at requested `0x0A = 10 ms`.

In the historically version-matched Robotology source snapshot
`093c73c14d219f86dcf5f9c8219f30c7a3c2ba54`,
`theSkin::Impl::tick()` generates **two CAN frames per enabled
triangle**: the old setup would generate **eight CAN frames per
completed source cycle**, versus **two** for the E06 one-triangle
configuration (assuming the same source-side firmware behavior).
This is a fourfold difference in frames per source cycle, not
necessarily fourfold I2C sensor-reading work or bus utilization.

**Additional source-side loss path (upstream implementation, not
observed on physical MTB4):** The version-pinned
`emBODY/eBcode/arch-arm/embot/app/skeleton/embot_app_skeleton_os_evthreadcan.cpp`
sets `canconfig.txcapacity` from the board event config, which is
`maxOUTcanframes=48` for this MTB4 application. The event handler
pushes every generated CAN frame via `embot::hw::can::put()`
and calls `embot::hw::can::transmit()`. In the same pinned
`emBODY/eBcode/arch-arm/embot/hw/embot_hw_can.cpp`,
`can::put()` **erases the oldest queued frame when TX queue
size equals configured capacity** before appending the new frame,
then returns `resOK`. Thus, *if* the physical source's software
CAN output queue fills, frames may be dropped without an
error return. We have no MTB4 queue-depth measurements or
source-side drop counters. This potential loss path is **upstream
of the Feather's CAN ISR**, so zero E06 internal-loss counters
cannot exclude it.

**Competing mechanisms:** (1) overlong source acquisition with
non-counting timer event coalescing, yielding fewer source
acquisitions; (2) source CAN TX queue overflow, yielding generated
frames that are not transmitted. The historical code comments
alone cannot establish which mechanism occurred, and the 2025
hardware record does not establish today's firmware build. Source
acquisition runtime and independently generated/transmitted CAN
counts remain `NOT_VERIFIED`.

**Next targeted offline check (no serial/CAN communication):**
confirm historical and current CAN bitrates, and inspect the old
program's CAN handling to assess host-side workload differences:

```bash
grep -nE 'CAN_BD_RATE|CAN.begin\(|CAN.onReceive|CAN.parsePacket|CAN.packetId' \
  ~/code/PAL_skin/adafruit_feather_m4_can/src/main.cpp
```

No safe conclusion yet supports changing E06 from 10 ms to 26 ms,
adding instrumentation to the physical MTB4, or claiming
end-to-end losslessness.


### Historical Feather CAN polling versus E06 ISR reception

**Operator-supplied inspection (2026-11-13):**

```text
historical ~/code/PAL_skin/adafruit_feather_m4_can/src/main.cpp:
  72: #define CAN_BD_RATE 1000000
  564: int packetSize = CAN.parsePacket();  [discovery]
  743: while (!CAN.begin(CAN_BD_RATE))
  870: int packetSize = CAN.parsePacket();  [runtime]
  880: uint8_t patch__id = CAN.packetId();
  no CAN.onReceive() match in the operator's grep
```

Current E06 `FalangeM4/src/main.cpp` defines
`CAN_BD_RATE 1000000`, uses `CAN.parsePacket()` **only during startup
CAN discovery**, then registers `CAN.onReceive(onCanReceive)` after
starting the MTB4 stream. The ISR copies frame ID, `millis()`
timestamp and payload into a bounded **32-slot software queue**;
foreground `loop()` drains it and runs taxel assembly/encoding. The
historical program instead contains `CAN.parsePacket()` in its
runtime path. Its archived ASCII log prints CAN messages, taxels
and BMA400 accelerometer values, so its host workload and logging
path are materially different from E06's binary recording.

The nominal CAN bitrate is thus **not** the distinguishing factor:
both use **1 Mbit/s**. For illustration, eight CAN frames per
historical requested 26 ms interval is ~308 frames/s; at a
rough **110–135 bits per 8-byte standard CAN frame**, this
amounts to ~34–42 kbit/s (3–4% of 1 Mbit/s) on an otherwise idle
bus. This is **not** a measured on-wire trace, and ignores other
nodes, retransmissions and scheduling. The nominal bus capacity
is unlikely to be the sole limiting factor; neither sender TX
queueing nor historic receiver polling/serial delays are excluded.

**Three distinct unresolved mechanisms:**

1. Source acquisition task >10 ms and non-counting timer-event
   coalescence (fewer generated source samples).
2. Source-side CAN software TX queue drop-oldest while over capacity
   (samples generated but frames never transmitted).
3. Historical Feather polling/sensor/ASCII workload preventing
   reception or processing of every CAN frame.

The old comments about "lost packages" **cannot be assigned to
MTB4 source-side loss** without source/receiver telemetry. Current
E06's conserved ISR-to-host counts do not establish pre-ISR loss.
Historical and current configurations differ in triangle count,
period, additional BMA400 workload, and receive/logging strategy.

**Next read-only check:** Inspect the old Feather's runtime
receive path and main loop, rather than guessing from grep lines:

```bash
sed -n '852,950p' ~/code/PAL_skin/adafruit_feather_m4_can/src/main.cpp
```

Focus on CAN polling frequency, blocking serial prints, BMA400
sampling, and whether incomplete or missed two-part CAN samples
were detected. Do not open a port, adjust bitrates, flash or
configure the MTB4 for this investigation.


### Historical receiver runtime throughput budget and sample-assembly hazard

**Operator source excerpt:** The operator inspected lines 852..950 of
`~/code/PAL_skin/adafruit_feather_m4_can/src/main.cpp` on
2026-11-13. Its `loop()` calls `processSerialCommands()`,
then invokes `CAN.parsePacket()` **once per loop**, processes at
most one returned CAN frame, and *unconditionally* invokes
`accelerometer.getSensorData()` after the CAN handling block.
An adjacent historical comment says the accelerometer read
"takes 2.46 ms"; the code also prints ASCII CAN packet-size
information on **every** loop iteration, plus packet ID
and accelerometer X/Y/Z when in ASCII mode (the archived
2025 console log shows these debug messages). No receiver-side
queue depth or packet gap accounting is presented in this
historical excerpt.

**Conditional lower-bound throughput calculation:** If the
2.46 ms accelerometer-read comment reflects actual execution time
and one CAN frame is processed per loop, the receiver's ideal
upper bound is `1000/2.46 = 406.5` parsed CAN frames/s,
*before* packet handling, serial output, and other overhead.
With four enabled triangles and two CAN frames per triangle,
eight frames are required per source acquisition:

| Requested MTB4 period | Eight-frame demand | Only accelerometer time for eight polling iterations |
|---|---:|---:|
| 10 ms | 800 frames/s | 19.68 ms |
| 16 ms | 500 frames/s | 19.68 ms |
| 24 ms | 333.3 frames/s | 19.68 ms |
| **26 ms** | **307.7 frames/s** | **19.68 ms** |
| 32 ms | 250 frames/s | 19.68 ms |

This predicts that 10 ms and 16 ms are unsustainable for
the *historical receiver* under the stated assumptions; 24 ms
leaves only ~4.32 ms total for all other work per eight-frame
cycle, whereas 26 ms leaves ~6.32 ms. The pattern aligns
qualitatively with historical comments about 24 ms instability
and 26/32 ms working, but the timing claim is **not measured in
these E06 captures**. CAN traffic can be bursty, and the effect
of USB buffering and CAN controller FIFO sizes is not measured.
No claim is made that the physical MTB4 itself requires >=26 ms.

**Historical assembly integrity limitation:** The old code uses
`index_offset = (patch__id - 196) * 12`, so CAN decimal IDs
196..199 map to triangles 4..7. On every parsed `0x40`
payload, it overwrites the first seven values of that
triangle; on `0xC0`, it overwrites the last five. It marks
`allTaxelsReceived=true` immediately when a `0xC0` half
from ID 199 (last triangle, 7) arrives, without confirming
that the other seven frames were received or belong to the
same source acquisition. A missing earlier frame can therefore
leave stale taxels in a seemingly complete 48-taxel sample.
This is an integrity risk; a specific corrupted historical
sample has **not** been demonstrated.

**Conclusion and scope:** The old polling/accelerometer/ASCII
path is a credible *alternative* explanation for historical
"lost packages" and the active 26 ms request. **It does not
explain E06's current ~14/15 ms CAN-ISR cadence**, since E06
uses interrupt reception with a bounded 32-frame queue, no
BMA400 read, and only one transmitted triangle. Upstream
MTB4 acquisition overruns and source CAN TX drops still need
direct discrimination. The preserved E06
`PASS_MCU_TO_HOST_WINDOWS_ONLY` does not count source-side
transmissions or pre-ISR losses. Remain
`scientific_status=NOT_VERIFIED`.

No firmware changes, port opens, MTB4 configuration commands or
hardware tests are authorized by this documentation update.


### Version 1.22 bit-level I2C budget, event path, and archived cadence test

**Source pinned to Robotology historical commit**
`093c73c14d219f86dcf5f9c8219f30c7a3c2ba54` (declares standard
MTB4 application 1.22). The following is a *deterministic static
source count*, not a source CPU-runtime measurement and not proof of
the binary flashed on today's physical board.

`ServiceAD7147Isr(0)` calls `ReadViaI2C()` once per each of
**four sequential AD7147 device addresses**. For each address,
`ReadViaI2C` calls `StartI2CMaster` twice, sends four bytes
(device write address, register address high, register address low,
device read address), receives **24 bytes** (12 two-byte results
across parallel SDA lines), then calls `StopI2CMaster` once.

The successful full-read path's explicit calls to `Wait()`
(`I2C_Multi_SDA.c`) are:

| Function | `Wait()` calls per invocation | Invocations/address | Calls/address |
|---|---:|---:|---:|
| `StartI2CMaster` | 5 | 2 | 10 |
| `SendByteI2CMaster` | 42 (8 bit loops + acknowledgment handling) | 4 | 168 |
| `ReceiveByteI2CMaster` | 26 (8 × 3 bit-loop waits + 2 ACK/NACK waits) | 24 | 624 |
| `StopI2CMaster` | 3 | 1 | 3 |
| **Total** | | | **805** |

Thus **4 × 805 = 3,220 explicit `Wait()` calls per
complete four-address acquisition**. The helper uses
`embot::hw::sys::delay(1)` regardless of its `cycles`
argument: it requests roughly one microsecond per call, or
**3.220 ms of nominal requested delays** in aggregate.
It does **not** establish a 3.220 ms measured elapsed time or
a rigorous lower bound. The delay is a calibrated busy-wait
based on `SystemCoreClock` and assembler instruction loops,
and each bit operation incurs additional software and GPIO
overhead. The 2024 board multisda BSP configures **five**
parallel SDA GPIO inputs, and the receiver samples each input
per bit. In particular, the 24 received bytes per address
entail **4 × 24 × 8 × 5 = 3,840 explicit
`HAL_GPIO_ReadPin` calls** per complete acquisition,
plus output-pin transitions, GPIO mode/pull changes and
other work. These operations are not timed by the source
inspection.

**10 ms hypothesis test:** For sensor acquisition *alone*
to exceed the requested 10 ms, the time spent outside the
nominal 3.220 ms of requested busy-waits would need to
exceed ~6.780 ms (or the busy-waits would need to last longer
than nominal). Reaching 14.55 ms elapsed would require
~11.330 ms of other time if those waits were exact.
These are algebraic **what-if thresholds**, not measured
non-delay overhead or proof that source runtime is 14.55 ms.
Observed 14/15 ms Feather CAN-ISR intervals also include
source CAN assembly/transmission, event scheduling and
timestamp quantization.

**Timer-path additional non-counting stages:** The upstream
`theSkin::Impl::start` uses a periodic OS timer with
`EventToThread` action. Its RTOS timer callback
(`embot_os_Timer.cpp`) submits the same Timer pointer to
`theTimerManager`, whose default message queue capacity is
**8** (`embot_os_theTimerManager.h`). The manager calls
`setMessage(..., reltimeWaitNone)` without checking its
result (`embot_os_theTimerManager.cpp`); a full queue could
therefore silently fail to accept an expiry. `Timer::execute`
then executes the event action; its receiver is a non-counting
event flag (`osThreadFlagsSet/osThreadFlagsWait` in CMSIS-OS2
or corresponding OSAL functions), so pending repeated same-bit
notifications can coalesce. Both are **candidate** paths
where a nominal 10 ms requested timer expiry does not produce
a distinct completed source acquisition. Timer expiry count,
manager queue depth, event coalescing, task duration and
source CAN frame generation are **NOT_VERIFIED** on hardware.

**Empirical check without hardware:** Historical PAL_skin
ASCII logs contain `Taxel: <ms>` lines generated from
`currentTime=millis()` in the old Feather after reading
triangle 7's second CAN half. The read-only analyzer
`experiments/mcu_validation/acquisition/analyze_pal_skin_ascii_timing.py`
calculates the histogram and summary statistics of these
printed receiver-timestamp intervals, including <10 ms
bursts, 26 ± 1 ms arrivals and gaps >52 ms. It recognizes
MCU uint32 time wraps and excludes apparent resets from
cross-boundary interval statistics. Device-free tests cover
these cases, unknown lines and archive immutability.

```bash
cd ~/code/biointernect-edpr-e06-diagnostics
workon tactile
git fetch origin
git pull --ff-only origin fix/mcu-e06-periodic-diagnostics-20261113
python -m experiments.mcu_validation.acquisition.analyze_pal_skin_ascii_timing \
  ~/code/PAL_skin/adafruit_feather_m4_can/log/devttyACM0_2025_12_02.14.38.36.383.txt
```

**Caution:** The 2025 PAL_skin Feather's `Taxel:` message
was triggered by the second half of the *last* configured
triangle without checking all eight frames were present
and fresh. Consequently these printed intervals reflect
*historical Feather processing/completion times*, **not**
source 1.22 acquisition duration or physical independent
CAN transmission rate. A 26 ms average in that log would
not calibrate E06's present 14/15 ms cadence. Conversely,
short intervals or long gaps would be evidence of
polling/queuing/serial artifacts, not source loss proof.
This analysis does not identify the currently connected
MTB4 and does not change `scientific_status=NOT_VERIFIED`.

### Archived PAL_skin timing result and 16 ms periodicity clue

**Operator 2026-11-13 result:** Ran the committed read-only
`analyze_pal_skin_ascii_timing.py` on the 2025-12-02 log.
Exactly **20** historical `Taxel: <millis>` records were present,
producing **19** consecutive receiver-side intervals, with
**4,229 ms** between the first (8,056 ms) and last (12,285 ms)
printed receiver timestamps. There were no invalid timestamps,
detected MCU clock regressions, or wraparounds.

| Metric | Observed historical Feather-side value |
|---|---:|
| Requested old source configuration | 26 ms; not a live readback |
| Mean interval | 222.579 ms |
| Median interval | 177 ms |
| p95 / p99 | 915 ms / 915 ms |
| Minimum / maximum | 3 ms / 915 ms |
| Intervals within 26 ± 1 ms | 0/19 |
| Intervals > 52 ms | 18/19 |
| Printed-receiver completion rate | ~4.493 Hz (19 intervals / 4.229 s) |

The discrete histogram from the operator:

```text
3ms×1, 112ms×1, 144ms×2, 145ms×3, 162ms×1,
176ms×1, 177ms×3, 209ms×1, 241ms×2,
273ms×1, 321ms×1, 322ms×1, 915ms×1
```

**New offline observation:** **All 19 intervals** are within
**0–3 ms of a multiple of 16 ms**, including 144/145 (9×16),
176/177 (11×16), 241 (15×16+1), and 321/322 (20×16+1/2).
This is a *candidate ~16 ms periodicity or pacing signature*.
It is **not proof** of an exact scheduling clock, unique
fundamental period, USB behavior, or MTB4 sampling rate:
this is a small and selected set of only 20 printed
completions. The 3 ms interval and 915 ms gap additionally
show that the historical receiver's printed completions
cannot be assumed to follow the requested 26 ms source period.

**Important interpretation:** Since the historical Feather
only prints after a triangle-7 second-half CAN frame and does
not validate that all four triangles are fresh, this
**4.49 Hz is a rate of printed receiver completions**, not
a measurement of independent MTB4 acquisitions or a justified
packet-loss percentage. The historical source was different
from E06 (four triangles, polling CAN, BMA400, ASCII printing).
This data more strongly motivates an offline inspection of
the rest of the historical `loop()` for deliberate pacing
or blocking calls. It **does not explain the current E06
14/15 ms CAN-ISR intervals**, for which source-side timing
and physical firmware identity remain `NOT_VERIFIED`.

**Static source-accounting correction:** A second inspection
of the version-pinned `I2C_Multi_SDA.c` finds
`ReceiveByteI2CMaster()` executes **26** `Wait()`
calls per byte: 3 per bit × 8 + 2 ACK/NACK waits, not 25.
This corrects the original 3,124 estimate to **3,220**
explicit nominal one-microsecond delay requests per
four-device acquisition. The count includes a full
successful read without extra retries, but remains a static
source count, not a measured wall time. Neither this
correction nor the 16 ms pattern validates the proposed
14.5 ms source execution duration.

Next operator action is a **read-only inspection** of
historical code, with no MCU or CAN access:

```bash
sed -n '940,1020p' ~/code/PAL_skin/adafruit_feather_m4_can/src/main.cpp
```

Check specifically for `delay(...)`, serial writes/flush,
additional sensor work, loop pacing, and any conditions on
`Taxel:` output; avoid attributing the 16 ms structure
to a cause until that code is seen.


### Historical PAL_skin loop-end delay confirms narrow receiver margin

**Operator-provided follow-up, 2026-11-13:** The old Feather
`~/code/PAL_skin/adafruit_feather_m4_can/src/main.cpp`
(lines 940–end of `loop()`) shows an unconditional
`delayMicroseconds(380)` after BMA400 output, in both
ASCII and binary modes:

```cpp
#if (COMM_MODE == ASCII_MODE)
  Serial.print(" Y = ");
  Serial.print(Y.number, 3);
  Serial.print(" Z = ");
  Serial.println(Z.number, 3);
#else
  Serial.write('A');
  sendCurrentTime(millis());
  sendFloat(X);
  sendFloat(Y);
  sendFloat(Z);
#endif

// delay 0.3 ms
delayMicroseconds(380);
```

This revises the earlier **conditional** receiver throughput
budget. If the historical source comment that
`accelerometer.getSensorData()` takes 2.46 ms is accurate,
then each polling iteration consumes at least approximately
`2.46 + 0.38 = 2.84 ms` **before counting all other work**.
One `CAN.parsePacket()` is performed per iteration, so the
idealized ceiling would be ~352.1 parsed CAN frames/s. At
eight frames per four-triangle source acquisition, the
receiver needs at least **22.72 ms** for the eight loop
iterations, again under this timing assumption.

| Requested source period | Idealized receiver budget after 22.72 ms |
|---|---:|
| 10 ms | -12.72 ms (unsustainable) |
| 16 ms | -6.72 ms (unsustainable) |
| 24 ms | +1.28 ms |
| **26 ms** | **+3.28 ms** |
| 32 ms | +9.28 ms |

The 24/26 ms boundary now aligns closely with the old
programmer's comments, supporting an **old receiver-side
processing bottleneck** as a credible explanation of the
historical need for a 26 ms configuration. This is not a
measured lower bound because the accelerometer timing comes
from an unverified old comment, and one-frame polling,
CAN hardware buffering, verbose ASCII serial writing,
and USB scheduling can alter actual throughput. There is
**no evidence** that the MTB4 itself had a 26 ms hard
acquisition limit.

The extra 380 us delay does **not by itself account**
for the ~16 ms quantization visible among the historical
printed `Taxel:` intervals. USB and blocking ASCII
printing are possible contributors, but 20 printed
samples are insufficient for reliable causal attribution.
Do not use the old 4.49 Hz reported-sample rate as a
measurement of the physical MTB4 sampling frequency.

**Pivot back to current E06:** Its Feather uses CAN
interrupt reception, one transmitted triangle,
and no BMA400 polling workload. The old historical
receiver bottleneck therefore does not explain E06's
stable 14/15 ms incoming CAN-ISR timestamps despite a
10 ms requested source period. Future discriminating tests
must observe source-side task entry/completion and CAN
enqueue/transmit counters, or a truly independent external
CAN observer. Such tests require a separately approved
hardware procedure and/or MTB4 firmware change. Preserve
`scientific_status=NOT_VERIFIED` and the current
lossless-after-Feather-ISR acceptance boundary.


## Step 1 approved: no-contact A1–B–A2 encoder-load comparison

**Approved by operator 2026-11-13:** Test whether lowering
Feather event output affects incoming CAN taxel cadence without
changing the MTB4 skin configuration or flashing either device.
Use the existing `serial_configured_capture.py` single-owner
session: wait for validated taxel telemetry, send **three**
parameter commands, require exact accepted applied-value ACKs,
then record the post-ACK measurement. Each serial open **may
reset the Feather**, so treat each segment as a distinct
fresh session. An ACK is not independent firmware readback.

The current Feather receives CAN in an ISR and timestamps there,
before foreground taxel encoding. Its board transmission
request remains `0x0A = 10 ms` for all three conditions.

| Trial | RA threshold | Refractory ms | SA max Hz | Measurement |
|---|---:|---:|---:|---:|
| A1 baseline | 3 | 3 | 300 | 180 s |
| B low-event | 255 | 100 | 10 | 180 s |
| A2 baseline repeat | 3 | 3 | 300 | 180 s |

These values **do not disable the encoder**: the
per-taxel RA and SA code still runs. No contact, same mounting,
same environmental conditions, GUI closed, and no serial
monitor during sessions. Log the actual serial port:
use a verified `/dev/serial/by-id/...` path if available;
otherwise select the observed `/dev/ttyACM*` port.
Do not assume the correct port from earlier sessions.

**Operator-owned live hardware procedure** (only execute when
GUI is closed and serial port was verified):

```bash
cd ~/code/biointernect-edpr-e06-diagnostics
workon tactile
git fetch origin
git pull --ff-only origin fix/mcu-e06-periodic-diagnostics-20261113

ls -l /dev/serial/by-id/ 2>/dev/null || true
ls -l /dev/ttyACM* 2>/dev/null || true
# Replace this example after confirming device identity.
PORT=/dev/ttyACM1
RUN_ID=$(date +%Y%m%d_%H%M%S)

capture() {
  python -m experiments.mcu_validation.acquisition.serial_configured_capture \
    --port "$PORT" \
    --trial-id "E06_ENCODER_${RUN_ID}_$1" \
    --seconds 180 \
    --ra-threshold "$2" \
    --refractory-ms "$3" \
    --sa-max-frequency-hz "$4" \
    --confirm-gui-closed \
    --confirm-send-parameters
}

# Run sequentially and stop immediately on a nonzero exit or ACK mismatch.
capture A1 3 3 300 &&
capture B 255 100 10 &&
capture A2 3 3 300
```

**Per-trial offline verification** after the three acquisitions
(or after any abort; inspect completed sessions separately):

```bash
for phase in A1 B A2; do
  trial="experiments/mcu_validation/data/E06_ENCODER_${RUN_ID}_${phase}"
  test -d "$trial" || continue
  python -m experiments.mcu_validation.acquisition.verify_serial_capture "$trial"
  python -m experiments.mcu_validation.acquisition.analyze_taxel_timing "$trial"
  python -m experiments.mcu_validation.acquisition.analyze_diagnostics "$trial"
done
```

**Preconditions / abort:** Operator must verify the selected port
and confirm exclusive serial ownership, stable connection and
no-contact fixture. Do not execute if operating a robot or
other connected systems could be affected. On an unexpected
startup reset, missing telemetry, ACK mismatch, malformed
diagnostic CRC, internal MCU receive-queue loss, or anomalous
drop in sampling, **stop**, preserve the trial archive and
do not proceed automatically. Keep the current known-working
firmware; do not flash or change MTB4 timer values in Step 1.
The subsequent period sweep is a separate decision after
reviewing Step 1 data.

**Acceptance / inference:** Verify all raw capture digests;
the command handshake must report
`ACK_CONFIRMED_AND_CAPTURED` with the requested/applied
values, and sufficient intact `0x12` status packets to
compare enclosed windows (`PASS_MCU_TO_HOST_WINDOWS_ONLY`).
Compare A1/B/A2 number of event records/s and
`mean_rate_from_mcu_intervals_hz`, 14/15 ms interval
histograms, p95/p99 and >20 ms gaps. Lower event volume
combined with unchanged ~69 Hz ISR-derived taxel cadence
and conserved MCU-to-host windows would **disfavor a
foreground event-generation bottleneck**.
It would not prove absence of MTB4 source-side loss,
nor exclude a rare CAN peripheral drop before the ISR.
If event volume does not materially fall, B is not
a successful low-load manipulation. If the rate shifts
materially, verify repeatability and check status
counters before inferring any causal effect.
Retain `scientific_status=NOT_VERIFIED` for end-to-end
source acquisition until independent source evidence exists.


### A1 baseline result: 2026-11-13 15:22:02

The operator completed approved A1 at default event parameters
`ra_threshold=3`, `refractory_ms=3`, `sa_max_frequency_hz=300`,
with all three ACKs confirming the requested values. The archive is
`E06_ENCODER_20261113_152202_A1`, captured over 180 seconds on the
working Feather, **without** any test flash or MTB4 configuration
change.

All three post-capture analyzers were run by the operator:

- `serial_configured_capture`: `CAPTURED`,
  `ACK_CONFIRMED_AND_CAPTURED` (3 matching ACKs),
  12,410 post-ACK taxel frames; full archive parser: 12,411 taxel
  frames, 10,508 event frames, 108,331 event records,
  18 diagnostic frames, 0 CRC mismatch candidates and
  0 discarded bytes; 835,569 raw bytes; one trailing unparsed byte
  at capture cutoff.
- `verify_serial_capture`: `integrity_status=PASS`,
  all 22,940 parsed frames and 48,043 read chunks verified;
  3 ACK confirmations independently replayed.
- `analyze_taxel_timing`: 12,410 intervals; mean
  **68.6807865361 Hz**, mean **14.5601128122 ms**,
  median/p95/p99 **15 ms**, min **14**, max **15**.
  Exact histogram: 14 ms × 5,459 and 15 ms × 6,951;
  **zero intervals >20 ms**. Host taxel span
  179.991065614 s; MCU interval sum 180.691 s; the clocks
  are uncalibrated and must not be combined as identical.
- `analyze_diagnostics`: `PASS_MCU_TO_HOST_WINDOWS_ONLY`,
  18 delivered status packets, **17 compared windows**,
  zero missed diagnostic sequences, zero MCU↔host counter
  mismatches. Within covered windows the counters
  `can_rx_observed=can_rx_queued=can_rx_dequeued=23,352`,
  `taxel_samples_completed=11,676`,
  `event_records_enqueued=event_records_serialized=97,036`;
  zero CAN malformed / queue overflows / sequence misses,
  event allocation failures / drops.
- Diagnostic sequence 1616→1633 and MCU uptime
  16,176,535→16,346,535 ms: the MCU had run approximately
  4.5 hours at the start, so **this session did not reset
  the MCU** on opening the port (serial opens still
  *can* reset the board in other circumstances).

**Scientific reading:** The A1 baseline confirms a stable
~68.68 Hz ISR-derived tactile cadence under appreciable
foreground output (~602 event records/second averaged over
the 180-second capture). This alone does not isolate cause.
The next approved condition B uses RA=255, REF=100 ms,
SA max=10 Hz for 180 s, with one-owner ACK verification,
then the same three analyses. Require event-record
reduction before treating B as a successful low-load
intervention. A2 at 3/3/300 follows **only after B is
verified**, for reversibility. `can_source_packet_loss`
and `scientific_status` stay `NOT_VERIFIED`.


### B low-event result: 2026-11-13

The operator completed the approved B session with
`ra_threshold=255`, `refractory_ms=100`, and
`sa_max_frequency_hz=10`, all three accepted in a
same-session command ACK handshake.

- Trial: `E06_ENCODER_20261113_152202_B`; `CAPTURED`,
  `ACK_CONFIRMED_AND_CAPTURED`; 180 s post-ACK recording,
  12,412 post-ACK taxel frames, 12,413 total archive taxel frames.
- Full parser: **0 event records, 0 event frames**, 18 diagnostic
  frames, 3 command ACK frames, 0 CRC mismatches,
  0 discarded bytes, 1 unparsed trailing byte at capture cutoff.
  286,703 raw USB bytes across 25,419 reads.
- `verify_serial_capture`: `PASS`; 12,434
  parsed frames verified, 3 ACKs verified, raw bytes verified.
- `analyze_taxel_timing`: 12,412 valid MCU receipt intervals;
  **68.7002197389 Hz**, mean **14.5559941992 ms**;
  median/p95/p99 = 15 ms, min = 14 ms, max = 15 ms.
  Histogram: 14 ms × 5,511; 15 ms × 6,901.
  **Zero intervals >20 ms**. Host taxel span 179.999827228 s
  and MCU valid interval sum 180.669 s are distinct,
  uncalibrated clock domains.
- `analyze_diagnostics`: `PASS_MCU_TO_HOST_WINDOWS_ONLY`,
  18 status frames and **17/17** enclosed windows reconciled,
  no missing status sequence, no mismatches, and zero
  CAN malformed, overflows or sequence misses.
  Enclosed counter deltas: `can_rx_observed=can_rx_queued=
  can_rx_dequeued=23,358`, `taxel_samples_completed=11,679`,
  `event_records_enqueued=event_records_serialized=0`,
  `event_frames_serialized=0`. No allocation failures or
  dropped records.
- Uptime sequence is consistent with no MCU reset
  between A1 and B: A1 last diagnostic at 16,346,535 ms;
  B first diagnostic at 16,526,535 ms (later).
  This is supporting evidence of uptime continuity and
  does not establish immutable hardware identity.

**A1 versus B:** event records 108,331 -> **0** (-100%);
event frames 10,508 -> **0**; raw bytes
835,569 -> 286,703; mean ISR-derived rate
68.6807865361 -> 68.7002197389 Hz,
difference **+0.0194332027 Hz (~0.0283%)**.
The 14/15 ms timestamp interval range remains identical.
The B event-output suppression is **verified**, and the
CAN-ISR cadence remains unchanged within that tiny difference.
This strongly disfavors event generation / event serialization
being the rate-limiting cause, but the encoder functions still
run and non-event execution cost is not independently
excluded. This is not proof that every source acquisition
was received; source-side timestamps and transmitted frame
counts remain `NOT_VERIFIED`.

**Approved next step: A2** with parameters 3/3/300, same
180-second capture and verification. A2 both checks
reversibility and restores normal operating parameters.
Stop rather than proceed to the distinct MTB4 period
sweep if A2 fails or loses internal accounting.


### A2 normal-parameter restoration did not restore event output

**Operator-completed approved A2 on 2026-11-13:**
`E06_ENCODER_20261113_152202_A2`.
Three same-session accepted-value ACKs confirm
`ra_threshold=3`, `refractory_ms=3`, and
`sa_max_frequency_hz=300`; `CAPTURED` and
`ACK_CONFIRMED_AND_CAPTURED`. Raw capture verifier:
`integrity_status=PASS`, 12,435 valid parsed frames,
25,210 read chunks, 286,768 raw bytes, three verified
ACKs, 12,413 taxel frames. Decoder had zero CRC mismatch
candidates and one trailing unparsed byte at capture cutoff.

- `analyze_taxel_timing`: 12,412 intervals,
  mean **68.7047830973 Hz**, mean **14.5550273928 ms**,
  median/p95/p99 15 ms, min/max 14/15 ms, 14 ms × 5,523
  and 15 ms × 6,889, zero gaps >20 ms.
- `analyze_diagnostics`: 19 status frames, **18/18**
  enclosed windows `PASS_MCU_TO_HOST_WINDOWS_ONLY`;
  zero missing diagnostics, MCU-host mismatches, malformed
  CAN frames, queue overflows, sequence gaps, event
  allocation failures or dropped records. Counter deltas
  `can_rx_observed=can_rx_queued=can_rx_dequeued=24,734`,
  `taxel_samples_completed=12,367`, **event records
  enqueued=serialized=0** and event frames serialized=0.
- **Unexpected:** Despite restored A1 nominal encoder
  parameters, the host recorded **zero event frames and
  zero event records** in A2, and the MCU internal counters
  independently verify zero generated/enqueued events.
  Thus the **A-B-A reversibility criterion FAILED**.
  A2's command ACKs confirm reported parameter application,
  not independent readback or guarantee that the encoder's
  input conditions remained unchanged.
- Current A1/B/A2 mean MCU timestamp-derived rates are
  68.6807865361 / 68.7002197389 / 68.7047830973 Hz.
  All three archives retain exclusively 14/15 ms intervals
  and zero >20 ms gaps. This strongly establishes a stable
  source-to-Feather-ISR receipt cadence **regardless of
  observed event output**, but failure of A2 to recover
  events is an unresolved confound, and it is not proof
  of a causal effect solely from encoder parameter changes.
- A2 diagnostic uptime increases from 16,846,535 to
  17,026,535 ms, subsequent to B's last status at
  16,696,535 ms, consistent with continuous uptime
  rather than a device reset.

**Mandatory next step — do NOT proceed to the MTB4 period
sweep yet:** Compare the **decoded 12-taxel values** across
all three immutable archives. In particular, use
`serial_frames.jsonl` objects with `kind="taxel"` and
`taxels_0_no_pressure_to_255` to measure (a) all-zero
sample fraction, (b) per-taxel nonzero fraction/range,
(c) temporal trend and (d) whether sensors changed when
B started. The encoder's `SaEncoding()` returns early
for `taxelVal[id] == 0`; `RaEncoding()` emits only on
differences above the configured threshold. If B and A2
inputs have become all zero and stable, zero events may
be expected even after restoration; if values are nonzero
during A2, investigate encoder state/timestamp logic
and command propagation. Do not conclude yet that an
encoder logic defect is proven.

The period sweep is a **distinct later, operator-approved
test requiring separate Feather-only build/flash review**.
No new hardware action, CAN request or firmware modification
was performed as part of this documentation update.


### A1/B/A2 taxel-value comparison resolves zero-event confound

The operator ran the approved **read-only** archive comparison
on `E06_ENCODER_20261113_152202_{A1,B,A2}`.

| Observation | A1 | B | A2 |
|---|---:|---:|---:|
| Taxel samples | 12,411 | 12,413 | 12,413 |
| Samples containing any nonzero taxel | 11,001 (88.64%) | 0 | 0 |
| First sample | [80,101,92,58,14,119,109,89,61,61,77,3] | 12 × 0 | 12 × 0 |
| Last sample | 12 × 0 | 12 × 0 | 12 × 0 |
| Event records | 108,331 | 0 | 0 |
| ISR-derived rate (Hz) | 68.6808 | 68.7002 | 68.7048 |

Across **all twelve taxels**, B and A2 have min=max=mean=0;
A1 has positive means and maxima, but its **last** row
is already all-zero. Thus the input change occurred
**before B's altered parameters were sent**. The earlier
claim that B's parameters *caused* event suppression is
**NOT VERIFIED**; it was confounded by independently changing
taxel inputs.

Current `FalangeM4/src/encoding.cpp` explains why
A2 need not generate events even though all three commands
were ACKed at the restored defaults: `SaEncoding()`
returns immediately for `taxelVal[id] == 0` and
`RaEncoding()` emits only on threshold crossings, so
constant zero input yields no ordinary events. Therefore
the former A2 "reversibility failure" is **not evidence
of an encoder software fault**. It also **does not
verify event-load reversibility**, because the input stimulus
was absent in both B and A2.

The three distinct captures still show practically identical
14/15 ms MCU CAN-ISR receipt intervals and ~68.7 Hz
regardless of the *observed* event load, which disfavors
an event-output bottleneck. However, a controlled parameter
effect has **not** been established; the archive must not
be described as a successful causal parameter intervention.

**Next non-invasive discriminator:** Analyze the immutable
A1 archive for the precise last nonzero sample timestamp,
length of the terminal all-zero run, timing-rate histograms
and event counts in separate **early nonzero** and **late
zero** segments. Examine source-board startup/calibration,
physical configuration and pressure semantics only if
the transition needs further explanation. The values
alone do not establish a fault or a recalibration event.

No firmware or CAN changes were made for this analysis.
`scientific_status=NOT_VERIFIED` remains unchanged.


### A1 within-trial 30-second cadence versus event load

Operator read-only archive postprocessing (2026-11-13)
localized A1's **last nonzero** taxel sample to index
12,235 at host elapsed 177.454 s, with values
`[0,0,0,1,0,0,0,0,0,0,0,0]`. From index 12,236,
host elapsed 177.468 s, the last **175** A1 taxel
samples are all zero. B and A2 start with all-zero
taxel samples and remain all zero.

A1's 30-second windows, computed using each window's
**MCU timestamps** and host time only for segmentation:

| Host elapsed window | Taxel samples | Any-nonzero samples | Event records | MCU CAN-ISR-derived rate |
|---|---:|---:|---:|---:|
| 0–30 s | 2,069 | 2,069 | 33,740 | 68.670 Hz |
| 30–60 s | 2,068 | 2,068 | 28,955 | 68.678 Hz |
| 60–90 s | 2,069 | 2,020 | 19,829 | 68.682 Hz |
| 90–120 s | 2,068 | 1,749 | 8,786 | 68.683 Hz |
| 120–150 s | 2,069 | 1,680 | 12,122 | 68.679 Hz |
| 150–180 s | 2,068 | 1,415 | 4,899 | 68.692 Hz |

These exactly sum to **108,331 event records** in A1.
Over those windows the measured event count fell
from 33,740 to 4,899 (6.89×), whereas the sampled
rate remained in the extremely narrow range
**68.670–68.692 Hz**. This is additional
*within-configuration* evidence against foreground
event output being the limiting cause of source-to-ISR
sample cadence; no parameter change occurred during
the A1 windows.

**Step 1 decision:** The parameter A–B–A procedure
and all three raw captures passed their transport /
diagnostic acceptance checks. But A2's **event output
did not reverse** because the taxel values were zero;
therefore causal *parameter-effect reversibility* is
`NOT_VERIFIED`. The within-A1 workload variation and
stable arrival rate strongly disfavor output load as
the explanation for ~68.7 Hz, and support preparing
the already operator-approved **Step 2 source-period
sweep**. Physical tests/flash remain gated on
explicit review/authorization. This is an
interpretation of received CAN frames; MTB4
source-side acquisition timing and possible losses
remain `scientific_status=NOT_VERIFIED`.

Separate open question: all taxels trending to zero
could reflect settling, calibration, compensation or
source-data issues; no mechanism was verified from
the archives. Do not assert an encoder failure or
a specific sensor fault. Track independently from
the period-sweep analysis.
