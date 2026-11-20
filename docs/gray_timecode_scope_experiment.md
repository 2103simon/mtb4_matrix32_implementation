# Nano Every Gray timecode + Tektronix MDO4104B-6 (scope-only)

Experimental branch: `feature/live-oscilloscope-recording`.

## Aim and limits

Generate autonomous **10 Hz framed 16-bit Gray-code time markers** on
Nano Every **D7**, record those markers on oscilloscope **CH2**, and decode marker indices
in *independent*, finite oscilloscope acquisitions. This trial does
**not** modify or operate Matrix32 or the Feather tactile MCU.

The intended next phase is offline alignment of scope-record segments to
Nano frame counters and an Arduino-to-host clock estimate. **No physical
data is recovered in the gaps**, and USB serial reception timestamps
are not exact scope-edge timestamps.

## 1. Hardware and flashing

1. Keep stimulation **OFF** and Matrix32 disconnected for this test.
2. Build and flash the standalone **PlatformIO** project under
   `arduino_nano/` (source: `arduino_nano/src/main.cpp`). Select the
   **Nano Every** board (not the classic Nano), and run:

   ```bash
   cd arduino_nano
   pio run -e nano_every
   pio run -e nano_every -t upload --upload-port /dev/ttyACM0
   cd ..
   ```

   Replace `/dev/ttyACM0` with the detected Nano port. The PlatformIO
   project mirrors the structure of `adafruit_feather_m4/` and requires
   no third-party libraries. Uploading replaces the earlier
   `S07_MARKER_V2` firmware: its command protocol is *different*.
   Flash the earlier firmware back before using previous S07 scripts.
3. Attach Nano USB to the Linux PC. Wire **Nano D7 -> scope CH2 probe
   tip**, **Nano GND -> CH2 probe ground**. CH2 should show a 0/5 V
   signal if the board is powered at 5 V. Confirm probe attenuation
   matches scope CH2 settings (the experiment assumes a correctly
   calibrated probe setting).
4. **PROBE COMP remains disconnected.** CH2 is both the Gray marker
   source and the trigger, rising edge at +1 V.
5. The Arduino/PC/scope share a ground *only within this bench reference
   circuit*. Do **not** connect scope ground or Nano ground directly
   to Matrix32 electrodes or stimulation outputs. A future Matrix32
   integration requires a reviewed isolated/differential measurement path.

Nano and Feather USB serial ports are now selected **by board identity**,
not by the volatile /dev/ttyACM index. The shared detector reports the
resolved path for each:

```bash
/home/icub/.virtualenvs/matrix32/bin/python -m communication.serial_ports
```

**Nano discovery is implicit:** omit `--nano-port` entirely.
`config/config.json` uses `serial.port = null` for implicit
Feather M4 discovery. The detector inspects
pyserial USB descriptors without opening the devices, requires an
unambiguous Nano Every / Feather M4 match, and fails with diagnostics
rather than selecting another Arduino or serial adapter. Explicit
port overrides remain supported.

If device descriptors cannot distinguish the boards, inspect:

```bash
/home/icub/.virtualenvs/matrix32/bin/python -m serial.tools.list_ports -v
```

Then specify the port manually rather than guessing.

## 2. Timecode wire protocol

The output repeats every **100,000 us**:

| Phase from START rising edge | High pulse |
| --- | --- |
| 0..4 ms | START, 4 ms |
| 4..7 ms | 3 ms LOW guard |
| 7 + 5*i ms, for i=0..15 | 1 ms HIGH iff Gray bit (15-i) is 1 |
| 87..90 ms | 3 ms LOW guard |
| 90..97 ms | END, 7 ms |
| 97..100 ms | 3 ms LOW idle |

The 16 bits are `gray = binary ^ (binary >> 1)`, transmitted MSB
first. The binary counter is the full unsigned 32-bit `frameIndex`;
only its **low 16 bits** appear on CH2 and repeat after 65536 frames
(**109 minutes 13.6 seconds** at 10 Hz). Serial logs include the full
counter and the Arduino `micros()` reading taken shortly after
physically raising D7 at START. The host stores monotonic reception
time and raw serial text in `nano_serial.jsonl`.

Protocol commands sent by the Python runner:

- `PING <seq>` -> `ACK PING <seq>`.
- `INFO <seq>` -> `ACK INFO <seq> FW GRAY_TC_V1 PIN 7 ...`.
- `START <seq>` -> `ACK START <seq> FW GRAY_TC_V1`.
- `STOP <seq>` -> `ACK STOP <seq>`.

The generator does **not** start until START. Opening serial can reset
the Nano; the runner retries only idempotent PING, not START.
START resets the counter and schedules first START edge 50 ms later.
Each frame emits `FRAME <full_counter> GRAY <hex> EDGE_US <micros>
OVERRUNS <count>`; USB receive times do not locate the physical edge
with microsecond accuracy. Timing overruns indicate degraded marker
timing and should trigger a review of captured pulse widths.

Frame START/END delimiters are 4/7 ms; a data-1 pulse is 1 ms.
The decoder rejects partial/malformed frames. **Gray code is not
an error-correcting code**: check sequential counters, serial logs,
timing residuals, and a future checksum if higher integrity is needed.

## 3. First experiment: one scope record

After flashing and connecting hardware:

```bash
cd ~/code/mtb4_matrix32_implementation
git switch feature/live-oscilloscope-recording
git pull origin feature/live-oscilloscope-recording

/home/icub/.virtualenvs/matrix32/bin/python -m unittest discover \
  -s tests -p 'test_gray_timecode.py' -v
```

Dry run (does **not** open hardware):

```bash
/home/icub/.virtualenvs/matrix32/bin/python oscilloscope/test_gray_timecode.py \
  --count 1
```

With automatic Nano discovery enabled:

```bash
timeout 120s /home/icub/.virtualenvs/matrix32/bin/python -u \
  oscilloscope/test_gray_timecode.py \
  --scope-device auto \
  --points 100000 --time-div-us 20000 \
  --count 1 --label gray_ch2_single --run
```

This requests one 100,000-point, 200 ms time window on **CH2 only**.
The scope triggers on an Arduino D7 rising edge. Depending on trigger
phase (START or a data pulse), the record may include zero, one, or
more complete Gray frames. A zero-frame result needs inspection of the
recorded waveform and is not automatically a recording failure. The program reports decoded counters.

The Nano is discovered without specifying a path. If discovery fails,
inspect USB descriptors and pass `--nano-port /dev/ttyACM1` (or the
appropriate stable `/dev/serial/by-id/...` path). An existing serial
monitor must be closed first.

The run saves:

- `waveforms/record_00001_ch2.csv` (Gray timecode).
- `nano_serial.jsonl` (raw serial events plus host timestamps).
- `metadata.json` (verified scope settings, capture preambles, decoded
   Gray frames and acquisition results).

## 4. Repeated acquisitions: without changing the software

Once the single-record test succeeds:

```bash
timeout 180s /home/icub/.virtualenvs/matrix32/bin/python -u \
  oscilloscope/test_gray_timecode.py \
  --scope-device auto \
  --points 1000000 --time-div-us 200000 \
  --count 3 --label gray_ch2_multi --run
```

This captures three *separate* 2 s waveform windows on CH2,
with roughly 19–20 complete Gray frames per record. The channel is
transferred after the scope stops; it remains frozen during transfer.
At one million points, CSV conversion can take several seconds
per record. The external `timeout` is required because USBTMC I/O
can block.

The Nano timecode continues to run autonomously *during gaps*. Decoded
counter numbers distinguish which frames are missing between records.
Physical missing samples **cannot** be interpolated.

## 5. Re-run offline decoding

```bash
/home/icub/.virtualenvs/matrix32/bin/python \
  oscilloscope/decode_gray_timecode.py \
  data/recordings/<RUN>/waveforms/record_00001_ch2.csv \
  --output data/recordings/<RUN>/gray_decode.json
```

Optionally pass all the `record_*_ch2.csv` files to the decoder
together. Each record is decoded independently and retains its own
scope-relative time axis. The decoder uses a +2 V crossing and ±450
us pulse-width/slot-timing tolerances. Investigate decoding failure
before altering these bounds; confirm CH2 scaling and wiring.

## 6. Interpretation / decision gate

The first goal is **reliable decoding of the exact Gray counters**
visible on CH2, with matching Arduino `FRAME` lines in the serial log
and no unexplained overruns.

The next step (not yet implemented in the Matrix32 recording path)
is to join each decoded `counter_mod_65536` against the full Nano
counter in `nano_serial.jsonl`, resolve 16-bit rollover using logged
session history, fit the Nano-to-scope clock transform from multiple
START edges, and estimate **Nano-to-host uncertainty** from serial
receive brackets (or stronger host-request/ACK evidence). No
microsecond-precision PC clock synchronization has been established.

This design tests **offline alignment despite acquisition gaps**; it
does not claim live, gap-free streaming.


## 7. Offline clock alignment of separate recordings

The repeated-acquisition trial on 2026-11-20 decoded **57** complete
frames across three separate 1M-sample records: **4..22**, **98..116**,
**192..210**. These match the Arduino serial frame counters; no Nano
timing overruns were reported. The scope's inferred 100 ms frame periods
are systematically shorter than the Arduino `micros()` intervals by
roughly 30–46 µs per frame, so do **not** align records by naively
multiplying frame index by 100 ms.

The separate offline tool `oscilloscope/align_gray_records.py` now
joins scope-decoded frames to full Arduino `FRAME` serial messages,
unrolls 32-bit Nano `micros()`, and fits an independent affine clock
model for **each** finite record. The fit reports RMS and maximum edge
residuals in microseconds and the estimated Nano-time positions of
each record's first and last samples.

From the repository root, after a completed experiment:

```bash
/home/icub/.virtualenvs/matrix32/bin/python \
  oscilloscope/align_gray_records.py \
  data/recordings/20261120T101230327188Z_gray_ch2_multi
```

This writes `gray_alignment.json` next to `metadata.json`.
It does not rewrite the large waveform CSV files; sample `i` in a
record can later be mapped using its `clock_fit` coefficients:

```text
t_scope = xzero + (i - ptoff) * xincr
t_nano = slope_nano_s_per_scope_s * t_scope + offset_nano_s
```

**Important limits:** This establishes sample timing in the Arduino's
local clock domain, not UTC or host monotonic time. The host serial
reception timestamp can be delayed by USB/OS buffering and is not an
exact hardware timing observation. Physically missing oscilloscope
samples cannot be recovered. Aligning MCU and Matrix32 host logs
requires a separately validated Nano-to-host clock/uncertainty model.
The current script fails closed for missing/ambiguous frame matches or
fit RMS > 100 µs, and reports observed Nano timing overruns.


## 8. Nano / PC monotonic clock synchronization

### Host clock domain

`SerialManager` now passes the **same captured** `pc_arrival_monotonic_ns`
into adaptive Feather `ClockCompensator` and the tactile-rate/legacy
recording events. `time.time_ns()` is no longer used for drift fitting.
The existing Feather initial correction of 3625 ppm is **not** reused for
the Nano; each hardware clock requires separate parameters.

### Stage 1: existing serial logs (no reflashing required)

```bash
/home/icub/.virtualenvs/matrix32/bin/python \
  oscilloscope/analyze_nano_clock.py \
  data/recordings/20261120T101230327188Z_gray_ch2_multi
```

This writes `nano_host_clock.json` with `status=passive_drift_only`.
The `passive` section is **NOT** a calibrated host-time mapping:
`FRAME` timestamps and host serial reception times differ by variable
USB/OS delays. In the November 20 trial, 283 serial frame observations
over 28.2 s showed very large outliers, including nearly 1 s buffering
delay, while the Nano reported zero timing overruns. A simple regression
gave ~2584 ppm and ~243 ms RMS residual; this apparent slope must **not**
be interpreted as actual Nano oscillator drift. Passive residuals over
1 ms are flagged unreliable.

### Stage 2: active four-timestamp Nano SYNC

**Reflash the Nano Every PlatformIO project** after pulling these changes:
the new firmware implements `SYNC <seq>`, returning
`ACK SYNC <seq> RX_US <t2> TX_US <t3>`.
The PC records `t1` immediately before serial write, and `t4`
immediately after reading the full ACK line, using `time.monotonic_ns()`.
No Matrix32 or Feather connection is required for this calibration.

From the repository root, after flashing:

```bash
timeout 90s /home/icub/.virtualenvs/matrix32/bin/python -u \
  oscilloscope/test_nano_clock_sync.py \
  --duration-s 30 --sync-interval-s 0.25 --run
```

By default, this performs a **Nano-only** 30 s bench run, generating
Gray frame logs and active SYNC measurements. It saves
`nano_serial.jsonl`, `metadata.json`, and
`nano_host_clock.json`. Detection of the Nano Every is implicit.
It **only** starts/stops the Nano D7 reference generator; Matrix32
remains OFF/disconnected.

The four timestamps are used to fit
`PC_monotonic_ns = a * Nano_unwrapped_us + b`.
The fit uses weighted midpoint regression, with lower-delay exchanges
receiving more weight. Each exchange provides a causal offset interval
conditional on the fitted clock slope:
`t1-a*t2 <= b <= t4-a*t3`.
The interval midpoint is **not** a guaranteed unbiased clock offset if
USB/OS delay is asymmetric; the fit is labeled
`provisional_active_mapping`, not validated synchronization.
Do not interpret its RMS residual as a complete timing uncertainty.

For future Gray scope captures, the existing scope experiment now
collects **12 SYNC exchanges before** the scope recordings and **4 after
each** recording by default; this requires the updated Nano firmware.
The scope experiment still accepts `--sync-count 0 --sync-per-record 0`
for offline comparability with legacy firmware.

After the SYNC-enabled scope experiment finishes, run the two offline
tools **in this order**:

```bash
/home/icub/.virtualenvs/matrix32/bin/python \
  oscilloscope/align_gray_records.py data/recordings/<RUN>

/home/icub/.virtualenvs/matrix32/bin/python \
  oscilloscope/analyze_nano_clock.py data/recordings/<RUN>
```

The second report optionally composes
`scope -> Nano micros() -> PC monotonic_ns` for every record's first and
last sample. This does not alter the raw CSV data, recover gap samples,
or prove physical Matrix32 output-edge timing. A validated delay/jitter
budget is still required before interpreting microsecond-scale cross
device timing. The existing stimulation pipeline is not modified to
consume this experimental mapping.


## 9. Experimental CRC32 binary SYNC and controlled benchmark

The Nano now accepts **both** the original textual `SYNC <seq>` and
a compact CRC32-protected binary synchronization request; `FRAME` logs,
`PING`, `INFO`, `START`, and `STOP` stay textual, and the D7 frame
waveform is unchanged.

### Binary wire format (v1)

All integers are unsigned **little-endian**. CRC32 is the standard
reflected IEEE algorithm (same result as Python `zlib.crc32`),
computed over bytes after `A5 5A` and before the trailing CRC.

| Packet | Fields | Total bytes |
|---|---|---|
| SYNC request | `A5 5A`, version=`01`, type=`01`, sequence u32, CRC32 u32 | 12 |
| SYNC ACK | `A5 5A`, version=`01`, type=`02`, sequence u32, RX_US u32, TX_US u32, CRC32 u32 | 20 |

The host verifies version, type, length, CRC, and sequence. An invalid
binary ACK is never treated as valid clock evidence. The receiver
supports fragmented reads and mixed binary ACKs/text `FRAME` lines.
The Nano discards invalid binary requests silently. No automatic SYNC
retry is performed inside a single measurement, preventing duplicate
observations from being mistaken for independent samples.

**Important:** `t1` is taken immediately before the Python serial
`write()`; `t4` is taken when the host reader receives a chunk
containing the completed ACK. `RX_US` is sampled after the Nano reads
the final packet byte, and `TX_US` immediately before enqueueing the
binary response. These are *software* timestamps, not USB hardware
timestamps. CRC32 guarantees integrity of correctly framed data, not
one-way delay symmetry or sub-millisecond absolute accuracy.

### Tests and A/B run (Nano and scope kept disconnected from Matrix32)

After pulling, reflash the Nano **once** with the new firmware:

```bash
cd arduino_nano
pio run -e nano_every -t upload --upload-port /dev/ttyACM1
cd ..
```

Run offline tests from repository root:

```bash
/home/icub/.virtualenvs/matrix32/bin/python -m unittest discover \
  -s tests -p 'test_nano_binary_protocol.py' -v
/home/icub/.virtualenvs/matrix32/bin/python -m unittest discover \
  -s tests -p 'test_clock_sync.py' -v
```

Then run **two 30-second trials** with the same conditions:

```bash
timeout 90s /home/icub/.virtualenvs/matrix32/bin/python -u \
  oscilloscope/test_nano_clock_sync.py \
  --duration-s 30 --sync-interval-s 0.25 \
  --sync-transport text --run

timeout 90s /home/icub/.virtualenvs/matrix32/bin/python -u \
  oscilloscope/test_nano_clock_sync.py \
  --duration-s 30 --sync-interval-s 0.25 \
  --sync-transport binary --run
```

Both use Nano USB auto-discovery (no `auto` port flag). Each run
contains `nano_serial.jsonl`, `nano_host_clock.json` and
`metadata.json`. The binary log has sequence-tagged
`binary_sync_ack` evidence and `sync_exchange` rows with
`transport="binary_crc32_v1"`. The JSON output now includes RTT
p10/median/p90/p99, Nano handler time and RTT minus measured handler
time. No original log format is overwritten.

Compare, replacing the two paths with the generated run directories:

```bash
/home/icub/.virtualenvs/matrix32/bin/python \
  oscilloscope/compare_nano_sync_transports.py \
  --text-run data/recordings/<TEXT_RUN> \
  --binary-run data/recordings/<BINARY_RUN> \
  --output data/recordings/nano_sync_transport_comparison.json
```

The CRC32 binary transport is now the default following the 2026-11-20
A/B bench run; text SYNC remains available as a compatibility override. For a Gray scope acquisition, no transport argument is required;
use `--sync-transport text` only for legacy experiments.

**Interpretation:** A smaller RTT, reduced jitter or smaller fit RMS
would be a measurable improvement, but without independent timing
hardware we still cannot establish exact one-way latency. Avoid
interpreting the causal offset half-width as a guaranteed full
error bound. The existing Feather MCU CRC32 format and Matrix32
stimulation pipeline are not modified by this experiment.


## 10. Nano synchronization default and estimator refinement (2026-11-20)

After the controlled 30 s A/B tests, CRC32 binary SYNC is the **default**
for `oscilloscope/test_nano_clock_sync.py` and
`oscilloscope/test_gray_timecode.py`. The text SYNC transport is retained
solely for legacy compatibility, diagnostics, and repeatable A/B comparison.
There is no mandatory `--sync-transport` argument in new runs.

**Observed A/B comparison** (119 binary vs 118 text exchanges):

| Quantity | Text | CRC32 binary |
|---|---:|---:|
| Median RTT | 4811.41 us | 3447.62 us |
| p10 RTT | 4618.69 us | 3340.59 us |
| p90 RTT | 5042.16 us | 3590.51 us |
| Median Nano handler duration | 139.90 us | 111.93 us |
| Active fit RMS residual | 346.51 us | 398.98 us |
| Best offset interval halfwidth | 2029.79 us | 1587.32 us |

Binary therefore improves measured latency, **not** automatically
host-clock offset accuracy. CRC32 is checked before using a binary ACK
as synchronization evidence. The original Nano frame logs and CH2 marker
waveform are unchanged.

### Offline estimator analysis

`communication/clock_estimator_diagnostics.py` adds quality diagnostics
to `nano_host_clock.json` under `estimator_diagnostics`:

- **Baseline:** original weighted four-timestamp regression is
  preserved for compatibility; its time mapping is not silently changed.
- **Stratified lowest-delay fit:** divide the recording into six
  chronological bins; retain the lowest 25% of measured transport RTTs
  in *each* bin (at least two per bin) before refitting.
  This guards against selecting only an early or late latency cluster.
- **Clock stability:** independently fit three nonoverlapping time
  windows and report the drift spread and standard deviation; windows
  that lack minimum samples/duration are explicitly unqualified.
- **Causal offset feasibility:** at each fitted slope, intersect all
  per-exchange bounds `t1 - a*t2 <= b <= t4 - a*t3`, evaluated relative
  to a numerical anchor. Report a feasible interval width or the
  infeasibility gap; never invent a feasible bound when none exists.
- **Warnings:** flag sensitivity above 50 ppm, window drift spread
  above 100 ppm, and incompatible offset constraints.

**These are diagnostic thresholds, not acceptance criteria for
microsecond-accurate synchronization.** Slope uncertainty, USB one-way
asymmetry, and software-to-hardware timestamp latency have not been
independently calibrated. The provisional mapping remains in use for
offline visualization only, not as an authoritative physical event time.

Run the estimator on **existing** A/B records (no hardware or firmware
changes are necessary):

```bash
/home/icub/.virtualenvs/matrix32/bin/python \
  oscilloscope/analyze_nano_clock.py \
  data/recordings/20261120T104903872313Z_nano_clock_sync

/home/icub/.virtualenvs/matrix32/bin/python \
  oscilloscope/analyze_nano_clock.py \
  data/recordings/20261120T104937304807Z_nano_clock_sync
```

Both overwrite only their own derived `nano_host_clock.json`, never
the raw `nano_serial.jsonl`. To collect new data, the normal command
now defaults to binary:

```bash
timeout 90s /home/icub/.virtualenvs/matrix32/bin/python -u \
  oscilloscope/test_nano_clock_sync.py \
  --duration-s 30 --sync-interval-s 0.25 --run
```

To compare with older runs or intentionally select text SYNC, pass
`--sync-transport text`.

Offline tests:

```bash
/home/icub/.virtualenvs/matrix32/bin/python -m unittest discover \
  -s tests -p 'test_clock_estimator_diagnostics.py' -v
```

Do **not** update the Feather compensation algorithm or the Matrix32
stimulation event timestamps based on Nano ppm values; clocks are
independent and must be evaluated separately.


## 11. Blocked out-of-sample estimator validation

The November 20 comparison found that the low-delay filtered slope changed
by only **1.74 ppm** for CRC32 binary versus **10.47 ppm** for text;
however, three nonoverlapping ten-second windows still showed a
**185.0 ppm** range (binary) and **119.9 ppm** range (text).
Filtering insensitivity therefore is not sufficient evidence of a
stable oscillator estimate or reliable host-time offset.

The offline estimator now also performs **three-fold blocked holdout
validation**: fit using the other two chronological thirds of the
SYNC measurements and predict the withheld third's PC arrival/return
midpoints. For each held-out block, report the RMS, mean signed error
and maximum absolute error, plus the variability of training-fit slopes.
This detects extrapolation problems and time-dependent USB latency bias
without changing the canonical Nano-to-PC mapping.

All held-out errors are comparisons to the *software-observed SYNC
midpoints*. They are **not** errors against an independently measured
physical clock offset. A systematic one-way USB delay asymmetry may be
shared by both training and validation observations and remain invisible.

Run the offline estimator on existing text and CRC32 records; no
reflashing or new hardware collection is required:

```bash
/home/icub/.virtualenvs/matrix32/bin/python \
  oscilloscope/analyze_nano_clock.py \
  data/recordings/20261120T104903872313Z_nano_clock_sync

/home/icub/.virtualenvs/matrix32/bin/python \
  oscilloscope/analyze_nano_clock.py \
  data/recordings/20261120T104937304807Z_nano_clock_sync
```

The report stores results at
`estimator_diagnostics.blocked_holdout_validation`. A withheld midpoint
bias above 1 ms generates a diagnostic warning. Interpret it as
**clock-prediction instability and/or changing communication delay**,
not proof of a specific physical oscillator defect.

Recommended next hardware characterization, after verifying the
offline diagnostics: run the same **binary CRC32** experiment for
**180 seconds**. Analyze its full-record drift, 60-second windows and
holdout predictions before modifying production timestamp correction:

```bash
timeout 240s /home/icub/.virtualenvs/matrix32/bin/python -u \
  oscilloscope/test_nano_clock_sync.py \
  --duration-s 180 --sync-interval-s 0.25 --run
```


## 12. Three independent scope captures with deliberate 60-second pauses

Purpose: test whether **per-record Nano-to-PC clock fits** are more stable
than one global fit across deliberate acquisition gaps. The Nano Every
D7 Gray-code generator is **started once and never reset between records**.
Its continuous frame counter disambiguates successive waveform captures.
Only the **estimator sample set** resets per record; each epoch has its
own disjoint pre- and post-acquisition CRC32 SYNC observations. The
Feather M4 and Matrix32 are not used, and stimulation stays OFF.

The specialized entry point wraps the validated scope-only Gray capture:

```bash
cd ~/code/mtb4_matrix32_implementation
git pull origin feature/live-oscilloscope-recording

# OFFLINE tests; do not open Arduino or scope
/home/icub/.virtualenvs/matrix32/bin/python -m unittest discover \
  -s tests -p 'test_gray_epoch_pause.py' -v

# DRY RUN: show the exact plan; no serial/USBTMC device opened
/home/icub/.virtualenvs/matrix32/bin/python -u \
  oscilloscope/test_gray_epoch_pause.py
```

Bench experiment with external USBTMC watchdog:

```bash
timeout 900s /home/icub/.virtualenvs/matrix32/bin/python -u \
  oscilloscope/test_gray_epoch_pause.py --run
```

Default plan: **three nominally 30-second, 1,000,000-sample finite
oscilloscope records** (3 s/div, CH2 trigger at +1 V), **60 seconds
of deliberate pause after each completed transfer except the last**,
and **30 binary SYNC measurements immediately before arming and 30
immediately after the acquisition completes** for each record. No SYNC
messages are sent during the acquisition. The host reader still records
the ongoing Nano `FRAME` messages. The sync time windows bracket each
record; per-record fits use only that record's 60 SYNC exchanges.

**Hardware qualification:** 3 s/div and a 1M-point record length are
requested, not assumed supported. The code refuses acquisition if the
Tektronix scope does not confirm both settings. If the scope rounds
3 s/div to another supported step, use an explicit override such as
`--time-div-us 2000000` for a 20 s record (same sample count), rather
than ignoring the failed verification. Always inspect the actual saved
waveform preamble and time span. At 30 s / 1M samples the nominal 30 us
sample spacing gives approximately 33 samples for the narrowest 1 ms
Gray pulse, adequate for a first decoder attempt but coarser than
the prior 2 us marker experiment.

The pause starts **after waveform transfer/decoding**, before the next
pre-SYNC batch. The scope acquisition is not continuous between these
records; physically missing waveform samples cannot be recovered.
An actual run may take considerably longer than the three acquisition
windows plus the two pauses because waveform transfer and verification
are also required. The script logs real pause start and end host
monotonic timestamps.

On success the run directory contains:
`metadata.json`, `nano_serial.jsonl`, and
`waveforms/record_00001_ch2.csv` through
`waveforms/record_00003_ch2.csv`.
Metadata stores `clock_epoch` for every record with independent pre/post
SYNC sequence IDs, bracketing host monotonic times, and actual pause
intervals. No firmware reflash is needed if CRC32 SYNC v1 is already
installed.

### Analyze after the experiment

Use the run directory printed by the acquisition tool:

```bash
/home/icub/.virtualenvs/matrix32/bin/python \
  oscilloscope/analyze_gray_epoch_experiment.py \
  data/recordings/<YOUR_GRAY_EPOCH_RUN>
```

The `test_gray_epoch_pause.py --run` experiment **automatically runs
this analysis after all three acquisitions**, once USBTMC and the Nano
serial session are closed. The standalone analysis command is also
provided so you can rerun it with different thresholds without
repeating the physical experiment.

This reads the capture and raw serial observations, recomputes a
separate **scope-to-Nano alignment** per waveform record, and then
fits a separate **Nano-to-PC monotonic clock mapping** from that
record's pre/post SYNC set. It writes both `gray_alignment.json`
and `epoch_clock_analysis.json`. The final report includes per-record
drift, fit RMS, latency/offset diagnostics, independent sequence IDs,
sample endpoint estimates in PC monotonic time, qualification warnings
and an explicit scope-to-Nano timing-error threshold.

A local fit is `provisional_local_fit` only if its sync windows
correctly bracket acquisition, there are enough observations, its
midpoint RMS does not exceed 1 ms, its offset feasibility constraints
overlap, and its low-delay slope sensitivity is at most 50 ppm.
Otherwise it is **unqualified**, preserving raw evidence and warnings.
These are exploratory diagnostic thresholds, **not** a claim of
microsecond-accurate host synchronization. USB asymmetry still limits
absolute offset accuracy even when an epoch fit passes all checks.
