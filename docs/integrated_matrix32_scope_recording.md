# Integrated Matrix32 + Feather M4 + Nano + Tektronix oscilloscope recording

This is the **current standard** for CH1-only, dummy-load integrated measurements. One run gathers continuous Feather MCU telemetry, live BLE Matrix32 command/ACK history, and Arduino Nano Every Gray reference while acquiring **N independent, finite 20-second scope CH1+CH2 records** (`--count N`, default 1). Scope USB transfers introduce genuine unobserved gaps. All results are recorded for later independent per-record analysis.

[Project README](../README.md) · [Electrical CH1 setup](electrical_bench_setup.md) · [Clock mapping](clock_mapping_and_time_domains.md) · [Oscilloscope scripts](../oscilloscope/README.md)

> **Bench only; never attach a human subject.** The CLI output-permission flags are software acknowledgments, not electrical interlocks. Consult the [documented 100.0 kΩ dummy-load wiring and earth-reference cautions](electrical_bench_setup.md) before any real output.

## Hardware wiring and configuration

| Interface | Connection / settings |
| --- | --- |
| Matrix32 CH1 | CH1 cathode → **R1 = 100.0 kΩ** → Matrix32 shared anode (operator-reported reference setup) |
| Tektronix CH1 | TPP1000 10× probe tip on CH1 cathode, reference on shared anode in the source rig; check earth-bonding compatibility and use an approved safe probing arrangement |
| Tektronix CH2 | Arduino Nano Every **D7** Gray timecode, nominal 0–5 V, 2 V/div, +1 V rising trigger |
| MCU/Feather | Adafruit Feather M4 CAN Express via independent USB serial (115200 baud), connected to MTB4 tactile CAN sensor |
| Nano | Separate USB serial, **single canonical `nano_every` TCA0 hardware-timed buffered** 10 Hz Gray firmware, `FRAME_MODE 10HZ_HW_TCA_BUFFERED` |
| Scope | MDO4104B-6 over Linux USBTMC, SAMPLE/SEQUENCE, **10,000,000 points per channel in the next baseline** (CLI override), 2 s/div, trigger position 0%, horizontal delay OFF |
| Host | Python 3.11+ with required packages, Linux BLE/BlueZ, permissions for USB serial and USBTMC |

The physical breadboard retains **all three resistor branches** from the documented reference rig: **R1 = 100.0 kΩ** from Matrix32 CH1 cathode, **R2 = 99.4 kΩ** from Matrix32 CH2 cathode, and **R3 = 99.5 kΩ** from Matrix32 CH3 cathode, each connected to the **same shared anode**. The **992 kΩ spare is disconnected**, and other Matrix32 channels are unconnected as in the source setup. **This project's software commands only Matrix32 CH1 and records its output on oscilloscope CH1**; scope CH2 is connected to Nano D7, not Matrix32 CH2. The TPP1000 passive-probe ground is protective-earth referenced on a conventional Tektronix bench scope: **the reference setup is not inherently an isolated differential measurement**. All scope ground and Nano return connections require an electrical safety review before enabling the stimulator.

The live stimulation settings (SC, SW, SV, ST) are read from [`config/config.json`](../config/config.json). The requested `SF` comes from one selected taxel's `ra_i` or `sa_ii` rate. Commands are limited to channel 1. MCU rate update cadence (~44.5 Hz in recent runs), ACKed SF, and physical CH1 pulse frequency are **three different observables**.

## Commands

Run from repository root using your selected venv (shown here as `python`).

```bash
git switch feature/live-oscilloscope-recording
git pull --ff-only
# After review/merge, switch to the selected merged branch.
python -m unittest discover -s tests -p 'test_integrated_matrix32_recording.py' -v
python -m unittest discover -s tests -p 'test_analyze_integrated*.py' -v

# Default: plan only, no devices opened or stimulation commanded.
python oscilloscope/record_integrated_matrix32.py --points 10000000

# Non-stimulating baseline: accesses scope/Nano/Feather/BLE, but NO Matrix32 START.
timeout --signal=INT --kill-after=20s 180s \
  python -u oscilloscope/record_integrated_matrix32.py --run \
  --count 1 --points 10000000 --label integrated_hw_10M_20s_baseline
```

After the [electrical safety gate](electrical_bench_setup.md) has been completed and the bench load/probe arrangement confirmed, an authorized operator may run:

```bash
timeout --signal=INT --kill-after=20s 180s \
  python -u oscilloscope/record_integrated_matrix32.py \
  --run --arm --dummy-load-confirmed \
  --count 1 --points 10000000 --label integrated_hw_10M_20s_baseline
```

For approximately **2 minutes of observed scope data**, add `--count 6`; for approximately **3 minutes**, add `--count 9`. These capture 6/9 **separate 20-second segments**, not a gap-free 2–3-minute waveform. The wall-clock experiment is longer due to USB transfers and clock synchronization. For nine records, increase the **external watchdog** accordingly (e.g. `900s` instead of `180s`) but never rely on it as an electrical shutdown interlock.

The `--arm` option requires **both** `--run` and `--dummy-load-confirmed`. The external `timeout` is a process watchdog for potentially blocking USBTMC calls, **not** an electrical shutdown guarantee. Verify CH1 physically off and use a physical disconnect if required.

## Actual acquisition order

1. Open Nano Every USB session, **verify** `FRAME_MODE 10HZ_HW_TCA_BUFFERED` and `FRAME_US 100000`, then issue `START` for hardware-timed 10 Hz Gray D7 output.
2. Configure and verify scope CH1+CH2, CH2 rising +1 V trigger, 0% left-edge position and delay OFF. **Keep scope stopped**.
3. Launch the existing [`run_tactile_stimulation.py`](../run_tactile_stimulation.py) controller subprocess. Wait for both Feather MCU encoding parameter ACKs; in armed mode additionally wait for **Matrix32 START completion** based on a fresh nonzero tactile rate. Default startup readiness timeout is **45 s** (`--controller-ready-timeout`: 5–120 s).
4. For **each** record, obtain **30 binary CRC32 Nano SYNC exchanges** before arming, then issue `BUFFER_BEGIN`. The Nano timestamps real hardware-generated START events in RAM and does not transmit per-frame USB telemetry during acquisition.
5. Arm the oscilloscope for **one finite 20 s**, **10,000,000 samples per channel** for the new baseline, CH1+CH2 simultaneously, while the same controller continues to process tactile inputs and live SF updates.
6. **Immediately after scope completion**, issue `BUFFER_END` then `BUFFER_DUMP`. Verify all stored frame lines arrived and `LOST=0`; any overflow or missing data fails the record.
7. Obtain 30 post-recording SYNC exchanges, then **download and verify the frozen CH1+CH2 waveforms without stopping the controller**. The scope is not acquiring during this transfer; Matrix32 may still be outputting into the dummy load.
8. For repeated records, restart buffering with a fresh `BUFFER_BEGIN` for each independent acquisition. **After the final record**, request controller shutdown and best-effort Matrix32 STOP. Preserve all source waveforms, Nano SYNC epochs, continuous MCU/BLE data and acquisition metadata.

The integrated runner performs `--count N` **independent scope acquisitions** (1–12) under one MCU/Matrix32 controller session. Each record has its own pre/post SYNC epoch and waveform files. Scope samples **do not exist** during inter-record downloads, so those gaps must never be presented as continuous coverage. A Nano-triggered capture is **not** triggered by a physical Matrix32 CH1 pulse.

A printed `[START] Matrix32 CH1` means the command returned through the software protocol; it is **not electrical proof** that pulses were present. A `[SHUTDOWN] Matrix32 STOP sent.` likewise is **not proof** that the output is electrically off. The MCU's independent **250 ms stale-input supervisor** attempts to stop output if fresh tactile samples stop arriving.

## Recording directory and provenance

Every integrated run has its own `data/recordings/<UTC>_integrated_matrix32_scope/` directory; for `--count N`, add `record_00002_ch1.csv`, `record_00002_ch2.csv`, etc., and a distinct `metadata.json.records[index].clock_epoch` for every capture:

```text
<run>/
  metadata.json                # experiment config, scope settings, record boundaries
  nano_serial.jsonl            # Nano buffered FRAME dump (post-acquisition) and binary SYNC measurements
  matrix32_runner.log          # controller startup and shutdown
  gray_alignment.json          # per-record CH2-to-Nano frame matches
  epoch_clock_analysis.json    # per-record Nano-to-PC local clock estimates
  mcu_matrix32/
    *_settings.json
    *_tactile_rates.csv
    *_taxel.csv
    *_events.csv
    *_matrix32_commands.jsonl
  waveforms/
    record_00001_ch1.csv       # physical Matrix32 CH1 dummy-load voltage
    record_00001_ch2.csv       # Nano D7 Gray timecode
  analysis/
    integrated_report.md      # session-level findings
    integrated_summary.json
    mcu_full_stream.png       # entire independent MCU stream
    record_00001/
      report.md                # individual scope record
      summary.json
      integrated_overview.png
      matrix32_pulse_zoom.png
      matrix32_frequency_comparison.png
      matrix32_dynamic_tracking.png
```

The scope, Nano, MCU and BLE measurements have **different native time domains**. The controller remains active across recorded acquisition gaps, so physical output can occur while the scope is **not acquiring**; the Python runner cannot infer those missing pulses. Do not concatenate individual scope records or label time outside a measured 20 s window as observed output. The full MCU/Matrix32 log is preserved once, even though it starts before and may end after scope acquisition.

## Offline analysis and human-readable report

```bash
# Latest completed integrated session
python -u oscilloscope/analyze_integrated_matrix32.py

# An explicitly selected experiment session
python -u oscilloscope/analyze_integrated_matrix32.py \
  --run-dir data/recordings/<INTEGRATED_RUN>

# Focus on a taxel and waveform detail window
python -u oscilloscope/analyze_integrated_matrix32.py \
  --run-dir data/recordings/<INTEGRATED_RUN> \
  --taxel 11 --zoom-start-s 5 --zoom-sec 0.2
```

The offline analyzer opens **no BLE/serial/scope hardware**. Its independent per-record outputs include:

- **Direct CH1 measurements:** voltage extrema, candidate positive-phase pulses, pulse-to-pulse intervals, rate/count trends, detailed sample waveform.
- **MCU coverage:** selected taxel and encoding; only samples whose host-arrival timestamps fall within the record's **provisionally** mapped scope interval. Missing clock fit makes cross-device overlap **unknown**, not zero.
- **Matrix32 BLE evidence:** requested frequency, ACK-indicated SF and muted/STOP states. ACKs are **protocol events**, not electrical application timestamps.
- **Frequency-tracking evidence:** an overview using a **1 s sliding window / 0.25 s step**, and a rapid-change test comparing each measured pulse interval with the last ACK-indicated frequency only outside excluded transition/mute/unknown periods. Dynamic compatibility uses an **8 Hz or 15% tolerance**, whichever is larger. Settled 1 s windows use a different, stricter threshold and can legitimately be unavailable while SF changes rapidly.
- **Clock uncertainty:** per-record scope→Nano Gray frame alignment and Nano→PC four-timestamp SYNC fit, with qualification warnings and uncalibrated one-way USB offset.

The **record report** explains measurements and plots for its own acquisition; the **session report** links records, sums only actually observed scope durations and pulse counts, marks acquisition gaps, and counts the continuous MCU/Matrix32 logs just once. It never infers an instantaneous pulse rate across an unobserved gap.

Optional pulse detector / sliding window parameters:

```bash
python -u oscilloscope/analyze_integrated_matrix32.py \
  --frequency-window-s 1 --frequency-step-s 0.25 \
  --frequency-tolerance-hz 3 --frequency-tolerance-fraction 0.05 \
  --threshold-v 2 --deadtime-ms 1
```

The **2 V threshold** and **1 ms deadtime** detect *candidate* positive phases, not independently validated complete biphasic cycles. A scope-local CH1 pulse rate does not require USB clock synchronization; any comparison to PC-host command timing **does** and remains provisional.

## Interpretation and troubleshooting

- **No Matrix32 START before arming:** check fresh nonzero selected tactile rate, MCU ACK handshake, BLE readiness, 45 s startup timeout and `matrix32_runner.log`.
- **Flat CH1:** check the actual load/probe/attenuation/clipping, selected taxel, ACK trace and any stale-input STOP. Never infer correct electrical state from command logs alone.
- **Gray fit >100 µs RMS:** retain real raw data and quality warnings; RMS residual is not proof of absolute offset calibration.
- **0 comparable one-second windows:** expected for rapid SF transitions (tens of milliseconds). Use per-cycle dynamic results; absence of stable windows is **not** evidence of failed stimulation.
- **Multiple records:** analyze individually and report gaps; no artificial continuous scope stream or interpolated physical output.
- **Further frequency-accuracy experiments:** fixed-SF plateaus for several seconds on the verified load can assess steady-state accuracy more robustly than a rapidly changing live input.

For electrical details and source provenance, see [CH1 dummy-load wiring](electrical_bench_setup.md). For the complete Nano frame/timestamp method, see [clock mapping](clock_mapping_and_time_domains.md).


## New canonical 10M × 20s integrated baseline (after branch cleanup)

The target is **one** 20-second CH1+CH2 acquisition, with **10M
samples on each channel** (20M samples combined), an active Feather M4
tactile stream and the Matrix32 controller. `--run` without `--arm`
is a non-stimulating system check. **Actual CH1 output is not
authorized until the electrical safety gate is met and the operator
supplies `--run --arm --dummy-load-confirmed`.** There is no
automated electrical interlock.

```bash
python -m unittest discover -s tests -p 'test_integrated_matrix32_recording.py' -v
python oscilloscope/record_integrated_matrix32.py \
  --count 1 --points 10000000 --label integrated_hw_10M_20s_baseline
# After verifying dummy load, common-ground / earth compatibility,
# disconnected electrodes, live tactile samples, correct channel
# scale and physical STOP/disconnect:
timeout --signal=INT --kill-after=20s 1200s \
  python -u oscilloscope/record_integrated_matrix32.py \
  --run --arm --dummy-load-confirmed \
  --count 1 --points 10000000 --label integrated_hw_10M_20s_baseline
```

Check the displayed acquisition plan is CH1+CH2, 10M points per
channel, 2 s/div, 20 s duration, single record and the required
`nano_mode_required = 10HZ_HW_TCA_BUFFERED`. The Nano has already
recorded 20M points on CH2 alone in a bench test, **but we have not
yet verified this simultaneous two-channel 10M configuration**. The
scope settings readback must match before the controller is armed.
A long wall-clock timeout accommodates a slow two-channel USB
download; it does not imply the actual stimulation lasts 1200s.

After capture, use the source-locked CH1 negative-onset approval and
direct CH1 pulse intervals as primary physical evidence. Cross-device
Nano/PC/BLE/MCU comparisons must report scope↔Nano local-fit RMS and
absolute offset limitations without treating a failed 100us threshold
as invalidating direct CH1 electrical measurements. A physically
unobserved acquisition gap remains unknown.
