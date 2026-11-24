# Nano Every Gray timecode: separate oscilloscope acquisitions and local clock fits

> **Historical timing-study notebook, not a list of current flashable
> Nano builds.** The canonical firmware is now the single
> `arduino_nano/platformio.ini` environment `nano_every_10hz_hardware_timer`, with
> TCA0-driven 10Hz Gray D7 output and buffered frame timestamps.
> Older build names below are preserved solely to interpret archived
> experimental measurements; their firmware profiles were removed from
> the current Nano source and cannot be selected for uploading.
> For the next integrated baseline use
> [integrated Matrix32 scope recording](integrated_matrix32_scope_recording.md).


**Current scope-only procedure** for a Tektronix MDO4104B-6 and Arduino **Nano Every**. This page describes the **implemented workflow**; it does not attempt a new review of the earlier Gray benchmark findings. Detailed derivations and uncertainty definitions are in [clock mapping and time domains](clock_mapping_and_time_domains.md).

[Project README](../README.md) · [Nano firmware README](../arduino_nano/README.md) · [Scope scripts](../oscilloscope/README.md) · [Full clock-domain guide](clock_mapping_and_time_domains.md)

> **No Matrix32 output is used for this experiment.** Scope CH2 observes **Nano D7**, and an independent USB port carries Nano FRAME logs and clock SYNC responses. Never attach the Nano output or scope ground to an unapproved Matrix32 stimulation return. The physical wiring and ground-reference constraints for a separate integrated CH1 setup are documented in [electrical bench setup](electrical_bench_setup.md).

## Aim

The experiment provides a **physical timecode and independent software timestamp** from one Nano clock, then estimates how a **single scope record's** time axis relates to the Nano and host-PC monotonic clocks. Repeating finite scope acquisitions with pauses tests whether local clock fits can be formed separately; it does **not** provide gap-free acquisition or calibrated one-way USB timing.

## Hardware and firmware

- **Board:** Arduino Nano Every (ATmega4809), PlatformIO `[env:nano_every]`.
- **Output:** Nano D7 → oscilloscope **CH2** tip; confirm appropriate low-voltage reference/ground according to scope configuration.
- **USB:** Nano independent CDC serial port → Linux PC, 115200 baud; role-aware discovery in [`communication/serial_ports.py`](../communication/serial_ports.py).
- **Scope:** Tektronix MDO4104B-6 via USBTMC with CH2 **2 V/div**, rising trigger **+1 V**, 0% trigger position (left edge), delay OFF.
- **No stimulator:** Matrix32 and Feather are not connected or operated by the Gray-only acquisition script.

Build and flash, after inspecting the correct Nano upload port:

```bash
cd arduino_nano
pio run -e nano_every
pio run -e nano_every -t upload --upload-port /dev/ttyACM1
cd ..
```

The port above is illustrative and may change on reconnect. Close any serial monitor before launching Python so only one process owns the Nano USB stream. For complete firmware details see [`arduino_nano/README.md`](../arduino_nano/README.md).

## Framed Gray timecode on D7

Once the PC sends `START`, the Nano emits one 100 ms frame (**10 Hz**):

| Frame element | Position |
| --- | --- |
| Start marker, HIGH | 0–4 ms |
| Guard, LOW | 4–7 ms |
| Sixteen Gray bits (MSB first) | 7–87 ms; 5 ms per slot, 1 ms HIGH for bit 1 |
| Guard, LOW | 87–90 ms |
| End marker, HIGH | 90–97 ms |
| Idle, LOW | 97–100 ms |

The Gray value is `G = frame16 XOR (frame16 >> 1)`; the waveform represents the **low 16 bits** of a monotonic full frame counter. The Nano also emits one ASCII USB log entry per frame:

```text
FRAME <full_counter> GRAY <hex_gray_word> EDGE_US <nano_micros> OVERRUNS <count>
```

`EDGE_US` is sampled **just after** commanding the D7 START rising edge. It is distinct from the later host **USB message receipt** time, which can be delayed by buffering and OS scheduling. The `OVERRUNS` counter reports detected scheduling lateness.

The decoder [`decode_gray_timecode.py`](../oscilloscope/decode_gray_timecode.py) reads CH2 samples, identifies frame starts and bits, and recovers the encoded 16-bit counter. The alignment code [`align_gray_records.py`](../oscilloscope/align_gray_records.py) matches decoded frames to Nano USB logs and fits a local **scope → Nano** time mapping. Missing/ambiguous frames, wraps and monotonicity are diagnosed rather than treated as continuous data.

## USB clock synchronization

Each **binary CRC32 SYNC v1** exchange records four timestamps:

| Symbol | Clock | Measurement |
| --- | --- | --- |
| `t1` | PC `monotonic_ns` | Request send |
| `t2` | Nano `micros()` | Request reception |
| `t3` | Nano `micros()` | Response preparation |
| `t4` | PC `monotonic_ns` | Response reception |

The transport sends a 12-byte request and receives a 20-byte reply; `A5 5A` magic, type/version/sequence and CRC32 protect transport integrity. CRC32 does **not** measure or remove asymmetric USB delay. The source also offers a text `SYNC` transport for controlled diagnostic comparison, but **binary CRC32 is the default**.

Each finite acquisition gets its **own 30 pre- and 30 post-capture SYNC measurements**, giving a local affine Nano→PC slope/offset estimate. The two stages are composed:

```text
nano_time ≈ a * scope_time + b
PC_monotonic_time ≈ c * nano_time + d
```

This is a **provisional cross-device mapping**, not calibrated absolute latency. Scope-to-Nano edge RMS and Nano-to-PC SYNC fit RMS are **regression residuals**, not a worst-case physical timestamp error. Software USB timing asymmetry remains unknown.

## Standard multi-record experiment

Run from repository root:

```bash
git switch main
git pull origin main

# Device-free tests
python -m unittest discover -s tests -p 'test_gray*.py' -v
python -m unittest discover -s tests -p 'test_nano_binary_protocol.py' -v

# Print the exact acquisition plan; no hardware touched
python oscilloscope/test_gray_epoch_pause.py
```

The [`test_gray_epoch_pause.py`](../oscilloscope/test_gray_epoch_pause.py) wrapper configures this current standard:

| Parameter | Default |
| --- | --- |
| Scope record count | **3 separate records** |
| Per-record duration | **20 s**, not including USB download or pause |
| Record length | **1,000,000** points per scope acquisition |
| Scope horizontal | **2 s/div**, horizontal position **0%**, delay OFF |
| CH2 | Nano D7, **2 V/div**, +1 V rising trigger |
| Inter-record pause | **60 seconds** |
| Clock synchronization | **30 binary CRC32 exchanges before and after each record** |
| Nano generator | Remains running over the entire experiment |
| Matrix32 | **Never started** by this experiment |

Only after checking the wiring and scope configuration, start the hardware experiment:

```bash
timeout --signal=INT --kill-after=20s 300s \
  python -u oscilloscope/test_gray_epoch_pause.py --run
```

**Watchdog caution:** Three 20 s records plus two 60 s pauses already account for **180 s**, before Nano startup, 180 total SYNC transactions, and three potentially long waveform USB transfers. The 300 s external watchdog is an example, not an established upper bound on instrument transfer duration. A watchdog is not an electrical interlock.

For a single standalone Gray measurement, [`test_gray_timecode.py`](../oscilloscope/test_gray_timecode.py) is available with configurable count/timebase/record length; use its **dry-run** first and consult `--help` for arguments. For the standardized three-epoch comparison prefer the wrapper above rather than manually assembling inconsistent options.

## Output and analysis

Each experiment writes **one parent directory with independent record entries**:

```text
data/recordings/<UTC>_gray_epoch_pause_20s/
  metadata.json                 # exact scope settings, record indices and boundaries
  nano_serial.jsonl             # separate serial FRAME and binary SYNC observations
  waveforms/
    record_00001_ch2.csv        # finite scope acquisition 1
    record_00002_ch2.csv        # finite scope acquisition 2
    record_00003_ch2.csv        # finite scope acquisition 3
  gray_alignment.json           # matched CH2↔Nano frames for each record
  epoch_clock_analysis.json     # separate local Nano→PC fit and quality for each record
```

The wrapper attempts offline alignment after scope/Nano sessions have fully closed. It is also safe to reanalyze later, without devices:

```bash
python oscilloscope/analyze_gray_epoch_experiment.py \
  data/recordings/<GRAY_EPOCH_RUN>
```

The analysis reports **per-record** decoded/matched frame counts, edge-fit RMS, Nano→host drift estimates, fit diagnostics and qualification/warnings. Its JSON records preserve each acquisition's SYNC ID ranges and estimated host bounds. A record with Gray edge RMS above the **100 µs default criterion** is **unqualified** for that qualification gate but its physical CH2 waveform remains valid source data.

**Never concatenate CH2 samples from separate records or calculate one apparent sample cadence through the acquisition pauses.** The Nano clock keeps running through gaps; scope physical samples are absent. Every record requires its own fit; missing overlaps remain unobserved.

## Qualification boundaries and follow-up

What this experiment supports: confirming that a physical marker corresponds to a Nano frame ID, estimating **local** oscillator-rate relationships and fit residuals, documenting gaps, and measuring which separate records meet explicit fit-quality gates.

What it does **not** support: USB one-way delay calibration; proof of sub-millisecond absolute scope↔PC offset; recovery of samples missing between captures; or measurement of Matrix32 CH1 physical output (CH1 is not recorded here).

Future re-examination of historical Gray trials and any revised experimental conclusions should be performed separately on an experiment branch, not folded into this current operating guide. For the existing estimator assumptions and engineering detail see [clock mapping](clock_mapping_and_time_domains.md) and [scope integration](integrated_matrix32_scope_recording.md).


---

## Controlled clock-only trial: 5 Hz with delayed serial telemetry

The original 10 Hz test stays available, **unchanged**, in the PlatformIO
`nano_every` environment. A new experimental target
`nano_every_5hz_deferred` implements:

| Setting | Original 10 Hz | Experimental 5 Hz |
| --- | --- | --- |
| Frame period | 100 ms | 200 ms |
| START / DATA / END geometry | 4 / 1 / 7 ms | **unchanged** |
| First 97 ms of frame | Encoded Gray signal | Encoded Gray signal |
| Remaining idle | 3 ms | **103 ms** |
| FRAME serial report | Immediately after START | **After END**, in idle |
| Serial/SYNC servicing | Every main loop | **Idle only** |
| Intended use | Original scope/Nano reference | **Clock-only A/B comparison** |

**Hardware timer:** the ATmega4809 already contains a TCA timer with
compare channels and four TCB timers. The Arduino core uses a hardware
timer for `micros()`, but our D7 waveform uses repeated software
`digitalWrite` calls controlled by `tickGenerator()`; it is NOT
currently a dedicated timer-compare waveform. No external timer board is
required for a future hardware-timed design. Using a timer channel for
D7 requires checking peripheral pin routing and avoiding conflicts with
the Arduino core's own timer resources.

**Caveat:** This 5 Hz profile simultaneously changes repetition rate
and serial scheduling. If it improves timing we will know the combined
intervention helps, but a third experiment would be required to attribute
the improvement to either part independently. Deferring SYNC handling
may increase USB round-trip times and worsen Nano-to-PC offset bounds,
even if CH2 physical pulse geometry improves.

### Flash the 5 Hz test firmware

Use a bench setup with Nano D7 connected only to Tektronix CH2 according
to the verified safe grounding arrangement. Disconnect Matrix32
stimulation outputs and electrodes completely. Do **not** run a serial
monitor simultaneously with the recorder.

```bash
cd ~/code/mtb4_matrix32_implementation
conda activate matrix32
git switch feature/live-oscilloscope-recording
git pull --ff-only
cd arduino_nano

pio run -e nano_every_5hz_deferred
# Substitute the actual Nano Every port (check pio device list).
pio run -e nano_every_5hz_deferred -t upload --upload-port /dev/ttyACM1
cd ..
```

A 5 Hz firmware now reports `FRAME_US 200000` and
`FRAME_MODE 5HZ_DEFERRED` in its `INFO` reply. The capture tool checks
**both** values before starting the clock, and aborts rather than
silently recording with the wrong Gray variant.

### Dry run and acquire

```bash
python -m unittest discover -s tests -p 'test_gray_5hz_deferred.py' -v
python oscilloscope/test_gray_5hz_deferred.py
# Only if CH2 physical wiring, waveform levels and settings are verified:
timeout --signal=INT --kill-after=20s 360s \
  python -u oscilloscope/test_gray_5hz_deferred.py --run
```

The same wrapper produces **3 separate 20 s oscilloscope records** with
30 binary SYNC exchanges before and after every record and 60 s gaps,
as in the original 10 Hz epoch experiment. There is **no Matrix32
stimulation**, CH1 measurement, or reconstruction of the unrecorded gaps.

A matched, freshly collected 10 Hz control experiment is preferable
to comparing only with historical integrated CH1/CH2 measurements:

```bash
cd arduino_nano
pio run -e nano_every -t upload --upload-port /dev/ttyACM1
cd ..
timeout --signal=INT --kill-after=20s 360s \
  python -u oscilloscope/test_gray_epoch_pause.py --run
```

**Do not overwrite or relabel historical runs.** Flash the specific
firmware target before each experiment and save each run in a new
timestamped folder. A firmware upload is never attempted by the Python
recorder.

### Analyze physical widths and candidate local time rescaling

```bash
BASE=data/recordings/<your_fresh_10hz_clock_only_run>
TEST=data/recordings/<your_5hz_deferred_clock_only_run>

python -m oscilloscope.audit_gray_pulse_timing --run-dir "$BASE"
python -m oscilloscope.audit_gray_pulse_timing --run-dir "$TEST"
python -m oscilloscope.compare_gray_schedules \
  --baseline "$BASE" --candidate "$TEST"
code "$TEST/analysis/gray_schedule_comparison.md"
```

The physical pulse audit extracts ALL thresholded, complete CH2 Gray
START (4 ms), DATA HIGH (1 ms) and END (7 ms) pulses in decoded
frames. It reports every pulse's high duration, rise-phase error, and
within-frame rise-to-rise and low-gap spacing. Gray zero bits produce
no HIGH pulse, so inter-pulse distances are **content dependent**.
The audit also compares physical CH2 START-to-START spacing to digital
`EDGE_US` spacing after compensating a constant relative clock rate.
Unmatched/unassigned waveform pulses remain visible.

The audit additionally tests a **diagnostic phase rescaling**:
use the physical START and END rising edges (nominally separated by
90 ms) as anchors, and predict the DATA rises that were **not** used
to estimate the scale. Compare those bit-rise errors before and after
the putative correction. This is a cross-check of a local Gray
timing model, **not authorization to time-warp scope samples** or
Matrix32 physical CH1 pulses. The scope sample times are the primary
physical measurement. The analysis never silently forces physical
pulses to be 4/1/7 ms wide.

Outputs include `gray_pulse_geometry_report.md`,
`gray_pulse_widths.csv`,
`gray_frame_spacing_physical_vs_nano.csv`,
`gray_schedule_comparison.md` and
`gray_schedule_comparison.csv` in each recording's analysis directory
or candidate run comparison folder.

It is possible to obtain a cleaner physical 5 Hz Gray signal and yet
a **worse host time alignment**, because deferred serial servicing
delays SYNC replies. Evaluate the physical CH2 geometry, scope-to-Nano
affine-fit residual, and USB offset quality **separately**.


---

## New: 10 Hz on-Nano RAM-buffered Gray recording (clock-only)

This is the separate **third firmware profile**. The two existing
PlatformIO environments remain available and unchanged:
`nano_every` (10 Hz immediate serial) and
`nano_every_5hz_deferred` (5 Hz, serial only in idle).
The new environment is **`nano_every_10hz_buffered`** (10 Hz,
no FRAME serial output while recording). Select it explicitly in
VS Code **PlatformIO → Project Tasks → arduino_nano →
nano_every_10hz_buffered → General → Upload**.

### How each acquisition works

The PC starts the Nano Gray generator once; its frame counter is
monotonic through the acquisition gaps. For **each** of three separate
20-second scope acquisitions:

1. Request 30 pre-arm CRC32 SYNC exchanges while NO timestamp buffer
   is active (Nano operates normally but does not emit FRAME lines).
2. Issue `BUFFER_BEGIN` and receive the capacity ACK. It clears the
   previous buffer and starts retaining each actually observed START
   edge timestamp from `micros()` directly after `digitalWrite(D7,HIGH)`.
3. Arm and acquire **CH2 only**, 20 seconds, 1,000,000 samples,
   2 s/div. The Nano runs the original 10-Hz 4/1/7-ms Gray code.
   **No per-frame serial telemetry is transmitted during recording.**
   The firmware still checks for incoming serial commands in its loop.
4. When the scope reports acquisition completion, immediately send
   `BUFFER_END` to freeze the timestamp log.
5. Request `BUFFER_DUMP`; the Nano emits ordinary
   `FRAME <counter> GRAY <code> EDGE_US <time> OVERRUNS <n>` lines
   corresponding to stored measurements, followed by a count ACK.
   This happens after scope acquisition has completed. The PC verifies
   the count and fails closed on missing entries or buffer overflow.
6. Request 30 post-capture CRC32 SYNC exchanges, then download the
   CH2 oscilloscope waveform. Wait 60 seconds before the next record
   (Nano Gray generation continues, but buffer logging is inactive).

**Memory constraint:** The ATmega4809 has 6 KB SRAM. The experimental
buffer reserves **256 × 12 = 3072 bytes** for complete 32-bit frame
counter, 32-bit EDGE_US timestamp and 32-bit overrun counter. At 10Hz,
this holds approximately **25.6 seconds**. The host must not start
buffering until just before arming the scope. If acquisition is delayed
so much that the buffer overflows, an explicit LOST count invalidates
that record. Existing data is never silently overwritten.

**This experiment is distinct from the 5 Hz delayed-SYNC experiment.**
SYNC is performed before/after each scope acquisition, not during the
buffering window. Unlike the 5Hz profile, the new 10Hz profile does
not deliberately defer SYNC handling to a 103-ms idle period.

### Run (Matrix32 and Feather disconnected; Nano D7 → scope CH2)

In VS Code, select and upload PlatformIO environment
`nano_every_10hz_buffered` (close the serial monitor first). Pull
the latest experiment branch before flashing.

```bash
python -m unittest discover -s tests -p 'test_gray_buffered_capture.py' -v
python oscilloscope/test_gray_10hz_buffered.py
# Verify physical CH2 wiring and dry-run plan before the explicit opt-in.
timeout --signal=INT --kill-after=20s 360s \
  python -u oscilloscope/test_gray_10hz_buffered.py --run
```

The recorder refuses to start unless the Nano INFO response explicitly
contains `FRAME_US 100000` and `FRAME_MODE 10HZ_BUFFERED`.
The resulting folder name ends in
`gray_10hz_buffered_epoch_20s`. Existing Nano/PC epoch analysis and
physical CH2 pulse audit work on these FRAME lines because the stored
Nano EDGE_US values describe physical events, not the much later USB
dump receipt times. The files preserve separate scope acquisition gaps.

```bash
RUN=data/recordings/<your_10hz_buffered_epoch_run>
python -m oscilloscope.audit_gray_pulse_timing --run-dir "$RUN"
code "$RUN/analysis/gray_pulse_geometry_report.md"
code "$RUN/epoch_clock_analysis.json"
```

Compare physical pulse width, frame-spacing variation, scope-to-Nano
held-out/affine fit and Nano/PC SYNC diagnostics against the existing
5 Hz and original 10 Hz captures. Do not equate improved waveform
geometry with independently calibrated absolute USB offset. A clean
relative timing map is not established unless the actual fits qualify.

### Engineering caveats

- `BUFFER_BEGIN` and `BUFFER_END` are software command/ACK boundaries,
  **not precisely measured scope start/stop times**. The buffer
  deliberately contains a few markers before/after the exact capture
  and frame matching uses the encoded full counter to select overlaps.
- The physical D7 switching is still software-timed using `micros()`.
  The new profile tests whether avoiding serial printing during
  acquisition improves timing; it does not implement a hardware timer.
- The Nano may still experience interrupt/scheduler jitter while
  sampling `micros()`; no experiment should silently replace
  actual CH2 edges with ideal 100-ms spacing.
- `BUFFER_DUMP` printing after acquisition can disturb the Nano's
  physical Gray waveform **during an unrecorded gap**. The next capture
  starts only after the dump, 30 new pre-SYNC exchanges and a new
  `BUFFER_BEGIN`, so any such effects remain outside the analyzed
  scope window.
- A firmware upload or runtime failure is not a medical-device
  safety interlock. The experiment is strictly Nano/scope-only.


---

## New hardware-driven TCA0 Gray baseline (experimental)

**Single-click VS Code target.** The Nano Every `arduino_nano/platformio.ini`
now sets `[platformio] default_envs = nano_every_10hz_hardware_timer`.
After pulling this branch, **VS Code PlatformIO → Upload** builds and
uploads **only this profile**, not every previous environment.
Other four sources/build profiles are preserved, and explicit
**Project Tasks → arduino_nano → nano_every_10hz_buffered → Upload**
restores the proven software-timed baseline without editing source.

**What is genuinely hardware timed:** Nano Every D7 corresponds to
ATmega4809 PA1. By routing **TCA0-WO1 to PORTA**, the TCA0 single-slope
PWM hardware drives the HIGH/LOW output. The CPU never calls
`digitalWrite(D7)` to time transitions during this profile. A TCA0
overflow interrupt prepares the **next** millisecond's double-buffered
duty value. All physical transitions then occur at timer-period
boundaries even if the CPU services interrupts late (unless it misses
the full 1ms look-ahead deadline; diagnostic overrun increments).

**Clock detail (critical):** PlatformIO's `nano_every` board build
defaults to **16 MHz**, not 20 MHz. We preserve the Arduino core's
existing TCA prescaler **÷64**, so TCA receives exactly **250,000
ticks/s**. Each PWM cell is precisely **250 ticks = 1ms**, giving
100 cells per 100ms Gray frame. With an explicitly configured 20MHz
Nano clock, the software alternates 312/313 timer ticks per cell to
preserve the exact aggregate 2ms period. No changes to the normal
PlatformIO board clock are needed or recommended.

**What remains software timestamped:** A `TCA0_OVF_vect` ISR calls
`micros()` shortly **after** hardware raises START on D7, then stores
the returned `EDGE_US` together with the frame counter and diagnostic
overrun count. This is NOT a true input-captured electrical edge time.
The physical waveform is hardware-timed, but ISR-entry latency can
remain visible in Nano↔scope comparisons. We do **not** claim to have
implemented fully hardware-latched timestamps. If the electrical pulse
geometry improves but edge fits remain poor, hardware timestamp
capture is the next distinct engineering task.

The code preserves the original **4ms START, 3ms low guard,
16×5ms bit slots with 1ms HIGH for one, 3ms guard, 7ms END,
3ms idle** frame layout. Only D7 and the Nano's timer resources
are used. Arduino's TCA PWM outputs/analogWrite are not available
simultaneously; no other TCA output is used in this sketch.
The Arduino core's `micros()` timebase uses a separate TCB peripheral;
the TCA prescaler is not changed, because its divided clock is also
routed to core TCB peripheral clocks.

**RAM buffer:** Identical `BUFFER_BEGIN` / `BUFFER_END` /
`BUFFER_DUMP` protocol, fixed 256-frame capacity, 12 bytes per frame,
zero per-frame USB transmission during each 20s acquisition. The host
checks `FRAME_MODE 10HZ_HW_TCA_BUFFERED` to prevent accidentally
recording with an older 10Hz software profile. NO Matrix32 commands,
Feather interaction or CH1 recording occur.

### Build/flash with a single VS Code click

1. Pull `feature/live-oscilloscope-recording` into VS Code.
2. Open `arduino_nano/platformio.ini` and verify the **one**
   default environment is `nano_every_10hz_hardware_timer`.
3. Close the serial monitor; connect the Nano USB cable.
4. Click **PlatformIO Upload** once in the bottom VS Code toolbar.
   This uploads only `nano_every_10hz_hardware_timer`.
5. Read the **Build/Upload** log and confirm that compilation and
   flashing succeeded. The firmware has not been physically tested
   at development time.

### Scope-only test after successful upload

With Nano D7 on scope CH2, an independently verified safe low-voltage
ground connection, and **Matrix32 disconnected**:

```bash
python -m unittest discover -s tests -p 'test_gray_hardware_timer.py' -v
python oscilloscope/test_gray_10hz_hardware_timer.py
# Confirm dry-run says expected_frame_mode 10HZ_HW_TCA_BUFFERED.
timeout --signal=INT --kill-after=20s 360s \
  python -u oscilloscope/test_gray_10hz_hardware_timer.py --run
```

The recorder uses exactly three 20s CH2 captures, each with 30
pre-arm and 30 post-arm binary sync exchanges and a 60s gap between.
Output directories end in `gray_10hz_hardware_timer_epoch_20s`.

Afterward, run physical timing analysis:

```bash
RUN=data/recordings/<your_10hz_hardware_timer_epoch_run>
python -m oscilloscope.audit_gray_pulse_timing --run-dir "$RUN"
```

Compare with the previous successful `gray_10hz_buffered_epoch_20s`
run using the **same 10Hz rate, buffer protocol, and scope settings**.
Evaluate START/DATA/END physical width and frame-spacing variation,
Nano-vs-CH2 residuals, and Nano-to-PC fit separately. The original
scope/Nano 100us RMS criterion remains unchanged. No raw CH1 waveform,
operator approval or existing clock-alignment file is modified.

**Safety and resource note:** This profile repurposes Arduino's TCA0
hardware and introduces a 1kHz overflow ISR. A bench oscilloscope-only
validation is mandatory before using the reference in integrated
sessions. If the firmware unexpectedly stalls, restore the previous
`nano_every_10hz_buffered` profile through its explicit Project Task.
