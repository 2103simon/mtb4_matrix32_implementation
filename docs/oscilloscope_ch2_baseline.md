# First Tektronix acquisition on CH2 (scope-only baseline)

This experiment is intentionally **not** integrated into `run_tactile_stimulation.py`.
It uses the working finite-triggered-record acquisition code from
`2103simon/matrix32_nerve_stimulation` (branch
`cleanup/analysis-consolidation`) without changing the Matrix32 control loop.

## Hardware and reference calibration evidence

- Scope: Tektronix **MDO4104B-6**, S/N **C010233**, firmware
  **CF:91.1CT FV:v3.22**, USB VID:PID **0699:0450**.
- Host: Linux kernel USBTMC at **/dev/usbtmcN** (N may change).
  Automatic unique-device detection and `*IDN?` identity verification are
  included in `oscilloscope/scope_device.py`. PyVISA is not required.
- User's current hookup: **CH2 -> PROBE COMP** on the scope itself.
  Connect the probe ground to the neighboring **PROBE COMP ground**, not to
  Matrix32 output.
- Earlier repository calibration tests on **CH1**, not CH2, showed the
  built-in probe-comp signal around **2.52 Vpp** and **1.064 kHz** scope
  instantaneous frequency (historical observed mean ~1.061 kHz).
  Tektronix PROBE COMP is a *functional test source*, not an absolute
  metrology reference; CH2 needs independent confirmation.
- Historical calibration used **1 V/div** and **1 V rising-edge trigger
  level**. These are different settings. Confirm scope probe attenuation
  matches the physical probe and waveform is unclipped.

## 1. Check connection (no stimulation)

From `~/code/mtb4_matrix32_implementation`:

```bash
lsusb -d 0699:0450
ls -l /dev/usbtmc*
```

Test the instrument with a scope-only acquisition; **no Matrix32 is
connected or commanded by this program**.

## 2. Dry run (no instrument accessed)

```bash
/home/icub/.virtualenvs/matrix32/bin/python oscilloscope/record_scope.py \
  --device auto --channels 2 --scale 2:1 \
  --time-div-us 2000 --points 10000 \
  --trigger-channel 2 --trigger-edge rising --trigger-level 1 \
  --trigger-position-pct 50 --timeout 15 --label probe_comp_ch2
```

The proposed 2 ms/div * 10 divisions is a 20-ms waveform window,
nominally ~20 cycles at 1.064 kHz. 10,000 points give nominal
~2 us/sample (actual WFMPRE acquisition timing is authoritative).
`--trigger-position-pct 50` selects a centered trigger and disables
horizontal delay. The script queries the actual configuration and fails
if the scope does not match; these requested settings are not guaranteed
to be supported exactly until tested on the instrument.

## 3. One triggered acquisition

Add `--run` to the same command:

```bash
/home/icub/.virtualenvs/matrix32/bin/python -u oscilloscope/record_scope.py \
  --device auto --channels 2 --scale 2:1 \
  --time-div-us 2000 --points 10000 \
  --trigger-channel 2 --trigger-edge rising --trigger-level 1 \
  --trigger-position-pct 50 --timeout 15 \
  --label probe_comp_ch2 --run
```

If `/dev/usbtmcN` denies access, the legacy hardware used
root-only `0600` permissions: temporarily run with
`sudo /home/icub/.virtualenvs/matrix32/bin/python -u ...`.
That may create root-owned files. Long term, use a restricted udev rule
rather than repeatedly using root.

Success creates `data/recordings/<UTC-timestamp>_probe_comp_ch2/` with:

- `waveforms/ch2.csv`: `time_s,voltage_v,raw`. Time is
  **scope-relative**, not host time; voltage is converted using WFMPRE.
- `metadata.json`: actual scope identity, verified settings, acquisition
  state observations, WFMPRE preamble, binary transfer counts, and status.
- Optional acquisition marker files only when the corresponding
  `--signal-armed` or `--signal-progress` flags are specified.

The recorder explicitly sends `ACQuire:STATE STOP`, configures
`SAMPLE` / `SEQUENCE` / `NORMAL` CH2 rising trigger, arms with
`ACQuire:STATE RUN`, waits until the sequence stops, then downloads a
fresh record via `CURVE?` using a length-checked IEEE-488.2 binary block.
It checks sample count and monotonic waveform time, and writes
`metadata.json` even on failures.

**The USBTMC read can block** despite the Python timeout, so keep an
external watchdog in first tests, e.g. prefix the command with
`timeout 45s`. If the instrument reports different supported record
length/timebase settings, inspect the error and actual scope readback
before adjusting the request.

## 4. Repeated finite acquisitions (next experiment)

The initial CH2 PROBE COMP triggered capture was successfully performed on
2026-11-20: `/dev/usbtmc3`, serial C010233, 10,000 samples, binary transfer
succeeded. The following **new recorder** has not yet been tried on hardware.

Run a **dry run**, without opening USB or creating any files:

```bash
/home/icub/.virtualenvs/matrix32/bin/python oscilloscope/record_scope_live.py \
  --channel 2 --scale 1 --time-div-us 2000 --points 10000 \
  --trigger-edge rising --trigger-level 1 --trigger-position-pct 50 \
  --count 5 --label probe_comp_ch2_live
```

Then record **five separately triggered finite waveforms**:

```bash
timeout 120s /home/icub/.virtualenvs/matrix32/bin/python -u \
  oscilloscope/record_scope_live.py \
  --device auto --channel 2 --scale 1 --time-div-us 2000 \
  --points 10000 --trigger-edge rising --trigger-level 1 \
  --trigger-position-pct 50 --count 5 \
  --label probe_comp_ch2_live --run
```

The program reuses the already verified `record_scope.configure()`,
`record_scope.acquire()`, and `tek_mdo_usb.capture()` routines.
It first checks `*IDN?` and the configured settings; then repeats
**STOP -> single SEQUENCE RUN -> observe acquisition STOP -> CURVE? binary
download -> save CSV -> next STOP/RUN**. Unlike the earlier single capture
it does not involve Matrix32 stimulation at all.

Output is in `data/recordings/<UTC-run>_probe_comp_ch2_live/`:

- `waveforms/record_00001_ch2.csv`, `record_00002_ch2.csv`, etc.:
  independent scope-relative `time_s,voltage_v,raw` records.
- `record_00001.json`, etc.: per-record host monotonic bounds for
  RUN/write, acquisition-state polling, CURVE? download and waveform
  validation; record length/sample interval and sha256.
- `summary.json`: scope identity, configuration, record filenames,
  durations and success/failure status. It is updated after each
  successful acquisition and on clean termination/failure.

**Interpretation:** `cycle_duration_s` is *host end-to-end time*,
including scope SCPI polling, USB transfer, CSV creation and validation.
`transfer_duration_s` also includes the CSV write/validation and is
**not solely USB transport time**. `scope_window_s` is the span of
one downloaded waveform measured from WFMPRE. The ratio of recorded
waveform spans to host elapsed time is only a **nominal capture
utilization indicator**; neither actual pulse sampling duty cycle
nor physical sample-to-sample gaps are established by these fields.
Each waveform's trigger-relative time axis starts anew. Even an
identical CSV hash does not prove stale data, since a periodic
calibration waveform can reproduce identical records. Recording cannot
be described as gap-free or time-aligned to the host.

These Linux USBTMC operations can block indefinitely in the kernel.
Always use an OS-level `timeout` during initial runs. A forced
SIGTERM may prevent the final summary write; previously completed
records remain saved. If `/dev/usbtmcN` is root-only, apply a narrow
udev permission rule or temporarily run through `sudo` with the same
Python interpreter, accounting for root-owned output.

**Future work:** measure how much wall time is spent in acquisition,
transfer and CSV conversion; optimize storage only after measuring.
We can later record a separate **Arduino Nano marker** on another
scope channel to relate trigger-relative times to BLE/MCU host times.

## 5. One-million-point timeout diagnosis (20 November 2026)

The operator confirmed that `--points 1000000 --time-div-us 200000`
**passes the strict scope configuration readback**, unlike 500,000 points,
which failed that check. However, the initial 1-million-point run
timed out waiting for `ACQuire:STATE` to reach STOP after **15 s**,
*before any waveform was transferred*. Thus the instrument's ability
to complete this particular long sequence remains **unverified**.

Do not infer a transfer-bandwidth problem from this timeout: no
`CURVE?` read occurred. Possible causes include trigger wait, scope
processing/holdoff for the long record, or acquisition settings not
yet characterized. The existing log did not distinguish them.

The recorder now supports an optional `--diagnose-trigger` switch,
which polls `TRIGger:STATE?` as well as acquisition state and retains
the observations in `record_00001.json` even if acquisition times out.
Trigger polling adds SCPI traffic and can affect timing; use only for
diagnostics, not throughput benchmarks.

**First diagnostic** (one record, longer per-record timeout, external
watchdog):

```bash
timeout 120s /home/icub/.virtualenvs/matrix32/bin/python -u \
  oscilloscope/record_scope_live.py \
  --device auto --channel 2 --scale 1 \
  --time-div-us 200000 --points 1000000 \
  --trigger-edge rising --trigger-level 1 \
  --trigger-position-pct 50 --count 1 \
  --acquire-timeout 60 --diagnose-trigger \
  --label probe_comp_ch2_1m_diag --run
```

If that times out again, inspect `record_00001.json`:
`diagnostic_summary.last_acquisition_state`,
`diagnostic_summary.last_trigger_state`, the trigger-state history
and `summary.json`. Report those observations before changing
the trigger or trying different sample lengths. The on-screen
trigger state and whether CH2 visibly still shows PROBE COMP are
useful independent checks. No reconfiguration of Matrix32 is involved.

### Offline regression tests

```bash
/home/icub/.virtualenvs/matrix32/bin/python -m unittest discover \
  -s tests -p 'test_scope_live.py' -v
```

