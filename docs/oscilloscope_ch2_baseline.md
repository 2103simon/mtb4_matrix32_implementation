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

## Next experiment

Only after the above succeeds on CH2, add a separate scope-only loop
that repeats fresh finite acquisitions and measures acquisition time,
transfer time, and uncovered dead time. Separate files must not be
misrepresented as one gap-free waveform. The earlier reference also
used an **Arduino Nano CH4 marker**; reserve another scope channel for
that hardware timing reference when we later align to BLE/MCU logs.
