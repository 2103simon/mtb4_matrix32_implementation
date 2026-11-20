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

The integration runner:
1. Opens the Nano serial session and starts the 10 Hz Gray generator.
2. Verifies oscilloscope CH1+CH2 setup, trigger and 0% position.
3. Takes 30 binary CRC32 SYNC measurements.
4. Arms the scope; on the verified running observation starts the
   existing Matrix32/Feather controller as a separate process.
5. Collects the finite 20 s CH1+CH2 recording. While the scope is
   acquiring the controller reads MCU telemetry, saves events and sends
   ACK-verified SF updates as rapidly as the existing BLE command worker
   can safely process fresh values.
6. Takes 30 post-acquisition CRC32 exchanges, gracefully interrupts
   the controller, waits for its shutdown STOP, and THEN downloads both
   waveform channels. If the child controller fails or the scope
   times out, its shutdown is attempted in the `finally` block.
7. Writes scope/Gray alignment and provisional per-record host clock
   diagnostics, if possible. Scope-to-Nano RMS >100 us generates an
   unqualified status without deleting valid data.

**Startup limitation:** the 20-second scope capture begins on a Nano D7
rising edge, possibly before BLE/Feather readiness and before Matrix32
START. The command log and resulting CH1 waveform show how much of the
20-second capture contained actual output. This is a first integration
record, not proof that every MCU update was captured electrically or
that BLE latency is negligible.

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
