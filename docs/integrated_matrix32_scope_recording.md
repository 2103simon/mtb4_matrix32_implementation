# First integrated Matrix32 + Feather M4 + Nano + Tek scope recording

This procedure captures one finite 20-second physical waveform record on
the MDO4104B-6 while independently recording **Feather tactile telemetry**,
**ACK-verified BLE Matrix32 CH1 commands**, and **CRC32 Nano clock exchanges**.
It reuses the existing `run_tactile_stimulation.py` controller and its
250 ms stale-input supervisor; the integrated launcher never replaces the
Matrix32 safety guards or claims that BLE ACKs are physical output events.

## Wiring and bench constraints

**BENCH ONLY — NOT FOR HUMAN STIMULATION.** Use a verified, isolated,
appropriately rated dummy load with NO person/electrode connected. Verify
the Matrix32 stimulation supply/return, isolated scope probing, probe
attenuation and CH1 voltage range before entering `--arm`. An ordinary
ground-referenced probe clip must **not** be connected to a floating/high
voltage output without an approved differential/isolated measurement
configuration. Keep a physical output disconnect available.

- **Scope CH1:** actual Matrix32 CH1 output voltage measured safely across
  the dummy load, with operator-chosen scale (default 10 V/div).
- **Scope CH2:** Nano Every D7 Gray timecode (0–5 V, scope at 2 V/div), also
  used as the +1 V rising trigger. Nano and Matrix32 electrical paths MUST
  remain isolated as required by their hardware; no shared stimulation
  return may be inferred from the Nano/scope setup.
- **Feather M4:** own USB serial port. The controller uses the same MCU
  rate snapshots, ACK-verified BLE updates and stale-input supervisor as
  the existing live stimulation script.
- **Nano Every:** separate USB port, GRAY_TC_V1 firmware, and 10 Hz framed
  Gray counter on D7.
- **MDO4104B-6:** 1,000,000 samples, 2 s/div, SAMPLE/SEQUENCE, CH2 rising
  trigger, horizontal position 0% and delay OFF. Readbacks are checked
  before and after acquisition.

The **44.5 Hz MCU tactile update rate is not necessarily the Matrix32
physical pulse frequency**. The existing controller translates the newest
selected MCU tactile rate into a bounded SF command (2..400 Hz) and
sends only changed values, serially, waiting for BLE ACKs; it does not
issue one BLE stimulation pulse per input update. The output waveform
is the authoritative evidence of physical stimulation frequency and
latency. The exact CH1 settings (SC, SW, SV, ST) remain from
`config/config.json` — the integrated runner does not silently change
them. Confirm those commands are safe for the dummy load before arming.

## Dry-run and checks (no real devices touched)

```bash
cd ~/code/mtb4_matrix32_implementation
git pull origin feature/live-oscilloscope-recording

/home/icub/.virtualenvs/matrix32/bin/python -m unittest discover \
  -s tests -p 'test_integrated_matrix32_recording.py' -v

/home/icub/.virtualenvs/matrix32/bin/python -u \
  oscilloscope/record_integrated_matrix32.py

/home/icub/.virtualenvs/matrix32/bin/python -m communication.serial_ports
```

The dry run prints the plan and never opens devices, BLE or serial ports.

## Hardware recording: non-stimulating baseline first

```bash
timeout --signal=INT --kill-after=20s 180s \
  /home/icub/.virtualenvs/matrix32/bin/python -u \
  oscilloscope/record_integrated_matrix32.py --run
```

This connects and records MCU/BLE state, but **does not** send the Matrix32
configuration/START/SF/SC stimulation writes. It captures CH1 and CH2
for 20 s to verify channel wiring, Nano decoding, and recording outputs.
If the physical output shows non-zero stimulation during this dry-run,
stop and investigate an unexpected independently running controller.

## Hardware recording: live Matrix32 output on verified dummy load

Only after reviewing the baseline, manually confirming **NO person,
isolated and rated dummy load, approved probing, and physical disconnect**:

```bash
timeout --signal=INT --kill-after=20s 180s \
  /home/icub/.virtualenvs/matrix32/bin/python -u \
  oscilloscope/record_integrated_matrix32.py \
  --run --arm --dummy-load-confirmed
```

The explicit flags are both required for stimulation. `--run` alone
leaves Matrix32 in dry-run mode. **External `timeout` is necessary**
because a USBTMC read may block. If a hardware fault occurs, use the
physical output disconnect; software STOP/kill is not guaranteed to
stop electrical output after BLE failure.

The integration runner (**revised after the first live test**) now:

1. Opens the Nano serial session and starts the 10 Hz Gray generator.
2. Configures and verifies scope CH1+CH2, trigger and 0% position but
   **keeps the scope stopped**.
3. Starts the existing Feather/Matrix32 controller in a subprocess. In
   `--run` mode it waits for `[MCU] Configured; ready for live data.`
   (both MCU configuration ACKs). In `--arm` mode it **additionally
   waits for `[START] Matrix32 CH1:`**, emitted only after the Matrix32
   start request has returned. Startup is capped at 45 seconds by default
   (`--controller-ready-timeout` overrides, range 5..120 s).
   No scope acquisition is started on startup timeout/failure.
4. Takes 30 binary CRC32 SYNC measurements AFTER controller readiness.
5. Arms the scope for its finite 20 s acquisition. The running
   controller continues to consume fresh tactile data and sends changed
   SF commands via the existing sequential ACK-verifying BLE worker.
6. Takes 30 post-acquisition CRC32 exchanges, gracefully interrupts the
   controller, waits for its shutdown STOP, and THEN downloads both
   waveform channels. If the child fails or the scope times out, its
   shutdown is attempted in the `finally` block.
7. Writes scope/Gray alignment and provisional per-record host clock
   diagnostics, if possible. Scope-to-Nano RMS >100 us generates an
   unqualified status without deleting valid data.

The earlier live 20261120T144228169465Z run displayed a Matrix32 START
only after the scope was already running; that sequence could squander
part of the 20 s on BLE connection and handshake. The revised order
captures a running device instead of startup delay.

**Qualification caveat:** MCU handshake and the printed Matrix32 START
line prove software protocol progress, NOT that CH1 pulses are physically
present. The new CH1 waveform is needed to verify sustained output.
If the selected tactile input remains zero, there is no valid positive
START and the armed runner times out **without starting scope capture**.
Triggering remains on CH2 Nano D7; it is not synchronized to a Matrix32
pulse. The existing 250 ms stale-sample supervisor still stops output
if input is lost, even after readiness has been signaled.

**Trigger limitation:** the 20-second scope capture still begins on
the next Nano D7 rising edge, not on a Matrix32 output pulse. Matrix32
START is now observed before arming, but output can still change or
stop during the record. Check CH1 voltage directly, and inspect the
MCU/Matrix32 trace for any stale-input STOP or frequency changes.
This is a first integrated experiment, not proof that every MCU update
was captured electrically or that BLE latency is negligible.

## Saved files

Each run is in `data/recordings/<UTC>_integrated_matrix32_scope/`:

- `metadata.json`: scope configuration before and after, the bracketing
  SYNC sequence IDs, acquisition timing, status and shutdown evidence.
- `nano_serial.jsonl`: Nano FRAME and SYNC measurements.
- `waveforms/record_00001_ch1.csv`: actual CH1 dummy-load voltage.
- `waveforms/record_00001_ch2.csv`: Nano Gray reference.
- `matrix32_runner.log`: exact controller startup, BLE and STOP messages.
- `mcu_matrix32/*_tactile_rates.csv`, `*_taxel.csv`,
  `*_events.csv`, `*_matrix32_commands.jsonl`, and settings manifest
  after the MCU config handshake succeeds (when `--record` is active).
- `gray_alignment.json` and `epoch_clock_analysis.json` if offline
  analysis succeeds.

All PC-side timestamps are **monotonic clock timestamps on the same
host**. Scope sample-to-host mapping remains *provisional*: Gray edge
fit RMS may exceed 100 us, and USB one-way delay asymmetry is not
calibrated. The scope waveforms must be evaluated directly to establish
physical pulse times. A shutdown report recording "STOP sent" means a
BLE write completed, not verified absence of output voltage.

After the first run, inspect `matrix32_runner.log`, the MCU trace,
CH1 voltage range/clipping, the two waveform time axes and the
resulting clock quality report. Run offline reanalysis as needed:

```bash
/home/icub/.virtualenvs/matrix32/bin/python \
  oscilloscope/analyze_gray_epoch_experiment.py \
  data/recordings/<YOUR_INTEGRATED_RUN>
```


## Offline visualization and printed summary of the latest run

The integrated analyzer reuses `analyze_mcu_recording.load_recordings()`
to load MCU rates and experiment settings, and reads the saved Matrix32
BLE trace plus CH1 physical waveform. **It never opens hardware**.

From the repository root:

```bash
/home/icub/.virtualenvs/matrix32/bin/python -m unittest discover \
  -s tests -p 'test_analyze_integrated_matrix32.py' -v

# Automatically select the newest COMPLETED integrated capture
/home/icub/.virtualenvs/matrix32/bin/python -u \
  oscilloscope/analyze_integrated_matrix32.py

# Or explicitly analyze the first complete pre-armed test
/home/icub/.virtualenvs/matrix32/bin/python -u \
  oscilloscope/analyze_integrated_matrix32.py \
  --run-dir data/recordings/20261120T144806568426Z_integrated_matrix32_scope
```

Optional diagnostic zoom and threshold:

```bash
/home/icub/.virtualenvs/matrix32/bin/python -u \
  oscilloscope/analyze_integrated_matrix32.py \
  --taxel 11 --zoom-start-s 5 --zoom-sec 0.2 \
  --threshold-v 2 --deadtime-ms 1
```

The analyzer prints the exact selected run directory, physical CH1
voltage range and sample count, number and per-second rate of candidate
positive CH1 pulses, median pulse-to-pulse interval, MCU arrival cadence,
selected taxel and encoded requested rate range, Matrix32 command/ACK
event counts and scope/Nano clock-fit status. Note that the MCU input
update cadence is not the physical Matrix32 output frequency.

It saves `analysis/integrated_overview.png`,
`analysis/matrix32_pulse_zoom.png` and
`analysis/integrated_summary.json` **inside the selected run folder**.
The overview uses a min/max waveform envelope so narrow electrical
pulses are not lost when one million samples are plotted at screen scale.
The zoom shows actual CH1 samples without decimation.

The default pulse detector records positive threshold crossings with
hysteresis and a 1 ms deadtime. This is a provisional event detector,
not certified complete biphasic pulse reconstruction; change the
threshold only after inspecting CH1 amplitude and noise. A flat or
saturated waveform invalidates physical rate interpretations.

When available, the scope-to-Nano and Nano-to-host affine mapping
provides **provisional host-time placement** of MCU rates and BLE
requests alongside the scope waveform. The plots label this explicitly
even when the record is `unqualified`. Neither BLE ACKs nor host
arrival times are physical stimulation-edge timestamps. The plot is
not evidence of sub-millisecond absolute latency.

The 20261120T144806568426Z initial integrated session reported
111.6 us scope-to-Nano RMS and -846.6 ppm Nano-to-PC drift; the saved
record was correctly marked **unqualified under the original
100 us Gray-fit threshold**. This does not prevent diagnostic
visualization or count-based CH1 waveform analysis.


## Human-readable Markdown report and expected-versus-measured output frequency

Running `oscilloscope/analyze_integrated_matrix32.py` now writes **both**
`analysis/integrated_summary.json` and
`analysis/integrated_report.md`, plus three embedded PNG figures:

- `integrated_overview.png`: CH1 min/max envelope, candidate event
  density, the selected MCU taxel, requested rate and BLE markers.
- `matrix32_pulse_zoom.png`: un-decimated CH1 biphasic waveform detail.
- `matrix32_frequency_comparison.png`: sliding-window measured CH1
  positive-phase event rate and the expected BLE ACK-indicated SF setting
  (only settled intervals, placed on the provisional scope/host map).

The Markdown report includes recording metadata, MCU sampling statistics,
physical pulse counts and spacing, the command/ACK event table, a
per-second subset of measured/expected/transition-status windows
(including *all detected discrepant windows*), clock-quality warnings,
and image links relative to the report so it can render on GitHub.

The physical event detector has a default **2 V** positive threshold,
hysteresis and a **1 ms** minimum pulse separation; these parameters are
diagnostic, not a validated full biphasic-cycle detector. The physical
sliding-window count defaults to a **1 s** window stepped every
**0.25 s**. A 1-second count has approximately 1 Hz quantization.
You can adjust these values for a pulse train whose amplitude/rate
requires a different threshold:

```bash
/home/icub/.virtualenvs/matrix32/bin/python -u \
  oscilloscope/analyze_integrated_matrix32.py \
  --frequency-window-s 1 --frequency-step-s 0.25 \
  --frequency-tolerance-hz 3 \
  --frequency-tolerance-fraction 0.05 \
  --threshold-v 2 --deadtime-ms 1
```

**Expected rate uses only protocol success evidence**: initial
`configuration_ack` plus `start_write_completed`; later
`sf_ack` events with status `OK`; `sc_ack` with current 0
mutes the physical expected pulse count; STOP disables the expectation.
Pure request events (`sf_requested`) and failed ACKs are not used as
successful frequency transitions. A sliding window is **not comparable**
if a change occurs within the window or 0.5 seconds of its edges,
if a START/ACK is missing, or if the scope-to-host mapping is missing.
In stable intervals the tolerance is
`max(3 Hz, 5% of ACK frequency, 1/window_seconds Hz)`.
Outputs are `compatible_provisional`, `discrepant_provisional`,
or `not_comparable`.

**Caution:** ACK timestamps indicate the host observed a protocol
response; they do not prove when a physical SF change happened.
The scope↔Nano↔PC time map is not absolutely calibrated and can be
marked unqualified. Consequently even 'compatible' is a **provisional
consistency check, not verified electrical latency or perfect tracking**.
Independent CH1 waveforms remain the source of physical frequency
evidence. The report retains every comparison window in JSON and a
readable subset plus all discrepancies in Markdown.
