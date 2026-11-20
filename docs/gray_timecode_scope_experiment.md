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
