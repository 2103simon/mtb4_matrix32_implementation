# MTB4 tactile sensing → Feather M4 → Matrix32 stimulation and oscilloscope recording

Research/bench software for reading **MTB4 fingertip tactile sensors** through an **Adafruit Feather M4 CAN Express**, translating selected taxel activity into frequency requests for an **E-Matrix32** stimulator over Bluetooth Low Energy (BLE), and measuring the electrical output with a **Tektronix MDO4104B-6** oscilloscope. An **Arduino Nano Every** supplies a Gray-coded hardware time reference so separate oscilloscope and PC timestamps can be compared.

> **Experimental bench apparatus, not a clinically validated medical device.** Never attach a person or stimulation electrodes during exploratory testing. Use an appropriately rated **resistive dummy load**, an independently approved earth-reference/differential/isolated measurement arrangement where required, and an accessible physical output disconnect. A BLE ACK or a software STOP does **not** electrically prove that output is safe or off.

This README documents the repository's primary branch, **`main`**. **Scope hardware runs and firmware uploading require local hardware and operator verification**; passing unit tests alone cannot establish electrical safety or timing accuracy.

## Contents

- [System and signal flow](#system-and-signal-flow)
- [Hardware and software](#hardware-and-software)
- [Quick start](#quick-start)
- [Configuration and stimulation control](#configuration-and-stimulation-control)
- [Recording and analysis](#recording-and-analysis)
- [How time synchronization works](#how-time-synchronization-works)
- [Repository map](#repository-map)
- [Verification and limitations](#verification-and-limitations)
- [Troubleshooting and references](#troubleshooting-and-references)

## System and signal flow

```text
MTB4 fingertip sensor (12 taxels, CAN)
           │
           ▼
Adafruit Feather M4 CAN Express
  CAN reception and pairing of taxel halves
  RA-I / SA-II tactile encoding, ~44.5 Hz update cadence
  CRC-framed USB CDC telemetry + ACK-based MCU configuration
           │ USB serial (115200)
           ▼
Linux PC: SerialManager → recording + selected-taxel rate
           │
           ├── BLE → E-Matrix32, CH1 stimulation (SF frequency updates)
           │              │
           │              ▼
           │        100 kΩ CH1 dummy load ──→ Tektronix scope CH1
           │
Arduino Nano Every (independent `micros()` clock)
  10 Hz frame: D7 16-bit Gray marker ───────→ Tektronix scope CH2
  full counter + physical edge timestamp ──→ PC via USB serial
  active CRC32 SYNC (four timestamps)  ←───→ PC
           │
           ▼
Per-record scope↔Nano fit + Nano↔PC fit
  → provisional overlay of measured output / MCU samples / BLE ACKs
```

**Three different rates must not be confused:** (1) the MCU's tactile update cadence (approximately 44.5 Hz in the current experiments), (2) the Matrix32 **requested/ACK-indicated SF** frequency, and (3) the **physical pulse rate** detected on oscilloscope CH1. The live controller sends an SF update only when the selected rate changes; BLE updates do not generate one independent stimulus pulse per tactile snapshot.

## Hardware and software

| Component | Interface | Purpose |
| --- | --- | --- |
| MTB4 / fingertip sensing assembly | CAN to Feather | Twelve taxel measurements |
| Adafruit Feather M4 CAN Express | USB CDC serial, CAN | Sensor packet assembly, encoding, telemetry, configuration ACKs |
| E-Matrix32 | Linux Bluetooth / BLE | CH1 configurable output and acknowledged SF/current commands |
| Arduino Nano **Every** (not classic Nano) | USB serial + D7 | Independent 10 Hz Gray clock reference |
| Tektronix MDO4104B-6 | USBTMC / SCPI | Finite CH1 physical + CH2 Gray waveform acquisition |
| Linux PC with Python | USB, BLE | Control, logging, synchronization, offline analysis |

The current host implementation was developed for a Linux environment with **BlueZ**, Python 3 and PlatformIO. The repository pins **Bleak 0.22.3** because its BLE readiness path was validated against an older BlueZ 5.53 setup; other versions are not presumed compatible. USBTMC and serial access require appropriate device permissions. See [installation and device setup](docs/installation_and_operations.md).

## Quick start

All commands below are issued **from the repository root** unless a `cd` is shown.

### 1. Host Python dependencies

```bash
git clone https://github.com/2103simon/mtb4_matrix32_implementation.git
cd mtb4_matrix32_implementation
git switch main

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Your existing experimental environment may instead use `/home/icub/.virtualenvs/matrix32/bin/python`; substituting that executable avoids creating a second environment.

PlatformIO is separately needed to build/flash the two boards: [firmware installation](docs/installation_and_operations.md). Avoid opening a serial monitor at the same time as the Python experiment, because both require exclusive access to a board's USB port.

### 2. Discover hardware without opening/commanding it

```bash
python -m communication.serial_ports
python -m serial.tools.list_ports -v
ls -l /dev/serial/by-id/
bluetoothctl show
```

The Python serial resolver matches the **Feather** and **Nano** by USB identity; it does not blindly select `/dev/ttyACM0` or `/dev/ttyACM1`. For device-role rules see [serial discovery](docs/serial_device_discovery.md).

### 3. Check the software without stimulating

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
python connect_matrix32.py
```

`connect_matrix32.py` connects, checks readiness/battery and holds a BLE connection, but **does not configure or start stimulation**. Stop it with Ctrl+C before starting another Matrix32 controller. Firmware builds and hardware timing need separate verification.

### 4. Read tactile data

```bash
python read_mcu.py --print-every 10
```

The host waits for initial telemetry, applies `raThreshold` and `saMaxFrequencyHz` to the MCU, **requires both ACKs**, then records rate snapshots. End with Ctrl+C.

Inspect the latest MCU-only recording:

```bash
python analyze_mcu_recording.py
```

### 5. Integrated 10M-per-channel / 20-second baseline

**Canonical Nano project:** open `arduino_nano/` as a PlatformIO project.
`arduino_nano/platformio.ini` has **one** environment,
`nano_every_10hz_hardware_timer`, which is also its default Upload target.
The complete Arduino build inputs remain `platformio.ini` and
`src/main.cpp`; historical alternative firmware implementations are
not selectable. From the repo root:

```bash
cd arduino_nano
pio run -e nano_every_10hz_hardware_timer
pio run -e nano_every_10hz_hardware_timer -t upload
cd ..
```

The Nano produces a **10 Hz hardware TCA0 PWM Gray waveform on D7**.
At every START frame boundary, the TCA0 overflow ISR reads `micros()`
and buffers `{counter, edgeUs, overruns}` in a fixed **256-entry
(3,072-byte) RAM array**. The ISR timestamp follows the physical
edge; it is not hardware-latched. The 256 entries cover 25.6 seconds
at 10 Hz, provided buffering is not opened substantially earlier than
a 20-second scope acquisition. **No per-frame FRAME messages are sent
during acquisition.** The integrated runner requires
`FRAME_MODE 10HZ_HW_TCA_BUFFERED` and performs 30 pre-recording
binary SYNC exchanges, `BUFFER_BEGIN`, scope capture,
`BUFFER_END`/`BUFFER_DUMP` (with zero-loss verification), and
30 post-recording SYNC exchanges.

**Next baseline:** one simultaneous 20-second CH1+CH2 waveform with
**10,000,000 samples per channel**, 2 s/div (2 µs nominal sampling
interval). CH1 measures Matrix32 physical output on the verified dummy
load; CH2 measures Nano D7. The Feather tactile stream and BLE
command/ACK logs are retained. Simultaneous two-channel 10M acquisition
has **not yet been bench-verified**, even though a 20M CH2-only test
worked.

From the repository root, run a **dry-run plan** (no hardware touched):

```bash
python oscilloscope/record_integrated_matrix32.py \
  --count 1 --points 10000000 --label integrated_hw_10M_20s_baseline
```

After an independent grounding, probe, 100 kΩ rated dummy-load,
no-electrodes and physical-disconnect review, an authorized operator
can initiate the **armed bench-only** trial:

```bash
timeout --signal=INT --kill-after=20s 1200s \
  python -u oscilloscope/record_integrated_matrix32.py \
  --run --arm --dummy-load-confirmed \
  --count 1 --points 10000000 --label integrated_hw_10M_20s_baseline
```

The watchdog covers potentially slow USB transfer but is **not** an
electrical interlock. A non-stimulating device check uses `--run`
without `--arm` or `--dummy-load-confirmed`. Observe Matrix32
physically OFF after the recording.

The legacy **100 µs scope↔Nano affine-fit threshold** remains a
clock-mapping diagnostic. An unqualified cross-device fit does not
invalidate source-locked, operator-approved negative CH1 onsets or
direct within-record CH1 intervals. Cross-device absolute timing and
BLE actuation latency remain uncalibrated. Never invent scope samples
during download gaps.

See the [integrated acquisition and electrical precautions]
(docs/integrated_matrix32_scope_recording.md) and
[Nano build/firmware reference](arduino_nano/README.md).

## CH1 electrical dummy-load setup

The integrated bench uses the **CH1 branch** of the [Matrix32 reference dummy-load rig](docs/electrical_bench_setup.md): a nominal **100.0 kΩ resistor between Matrix32 CH1 cathode and the common anode**. CH1 is observed using a Tektronix **TPP1000 10× passive probe**; the independent Nano Gray signal is observed on scope CH2.

**Important:** The reference setup connected ordinary probe ground clips to the Matrix32 common anode, potentially bonding it to oscilloscope protective earth. This is **not automatically an isolated differential measurement**. The compatibility of that electrical reference with the Matrix32 and Nano/scope connections must be checked before an armed test. **All three reference resistor branches remain physically connected:** CH1–100.0 kΩ, CH2–99.4 kΩ and CH3–99.5 kΩ, each from its cathode to the shared anode. Only Matrix32 **CH1 is driven/recorded as a stimulator channel**; scope CH2 is reserved for Nano D7 Gray timecode. See the [CH1 wiring diagram, values, limitations and safety gate](docs/electrical_bench_setup.md).

## Configuration and stimulation control

The editable host configuration is [`config/config.json`](config/config.json). Important groups:

| Key | Meaning |
| --- | --- |
| `tactile_encoding.raThreshold`, `saMaxFrequencyHz` | Firmware RA-I threshold and SA-II maximum-rate settings, applied only after explicit MCU ACKs |
| `stimulation_source.taxel` | Selected sensor taxel, 0–11 |
| `stimulation_source.encoding` | `ra_i` or `sa_ii` rate source |
| `matrix32_stimulation.channel` | Currently **1** only |
| `matrix32_stimulation.currentCommand` | Matrix32 SC command value (not a measured current) |
| `matrix32_stimulation.phaseWidthUs` | Per-polarity phase width command |
| `matrix32_stimulation.outputVoltageCommand` | SV command; not a substitute for independently measured load voltage |
| `matrix32_stimulation.interPulseTimingCommand` | ST command |
| `serial.port` | `null` for USB role discovery; explicit path is an override |
| `clock_compensation`, `clock_drift_monitor` | Separate MCU/PC diagnostic timestamp compensation and export settings |

**Do not equate command values with electrically verified amplitude or dose.** Review the active configuration, load impedance, pulse width, and output constraints before armed testing. The live Matrix32 frequency range is 2–400 Hz; requests below 2 Hz cause a **current-zero command (SC1=0) followed by SF=400**, in that order and with checked ACKs, rather than an unsupported SF=0 command. The follow-up fast scheduler setting is intended to speed future cycle-boundary opportunities; SC1=0 can itself wait until the end of the preceding cycle and is **not** an emergency physical OFF guarantee.

For MCU-driven Matrix32 operation without the oscilloscope, the separate controller is:

```bash
python run_tactile_stimulation.py --record           # no stimulation
# Isolated dummy load and authorized bench setup ONLY:
python run_tactile_stimulation.py --arm --record
```

It uses a **250 ms stale-tactile-input supervisor** (overridable with `--stale-ms`), sequential BLE commands/ACKs, and best-effort STOP on shutdown. See [Matrix32/MCU software architecture](docs/pipeline_and_components.md).

## Separate evidence, analysis and published reports

On the recording/results refactor branch, the experiment layout is:

- **Raw inputs**: `data/recordings/<RUN_ID>/` (never overwritten by analysis).
- **Signed reviews and derived work**: `results/<RUN_ID>/analysis/` (Git-ignored).
- **Curated final and technical reports with their figures**:
  `reports/<RUN_ID>/<TAG>/` (intentionally version-controlled).
- **Scientific code**: `experiment_analysis/`; hardware-specific acquisition
  stays under `oscilloscope/`.

For the historical approved 20-second run, first run the **verified,
non-destructive migration** before regenerating anything:

```bash
python -m experiment_analysis.migrate_layout --run-dir "$MTB4_RUN"
python -m experiment_analysis.migrate_layout --run-dir "$MTB4_RUN" --apply
python -m experiment_analysis.run_extended_analysis --run-dir "$MTB4_RUN"
python -m experiment_analysis.publish_reports --run-dir "$MTB4_RUN" --tag initial
```

See the [complete migration and publication workflow](docs/recording_results_separation.md)
before removing any old files; publication never copies raw waveforms into Git.

## Recording and analysis

### Output and provenance

```text
data/
  recordings/
    <UTC>_integrated_matrix32_scope/
      metadata.json                  # acquisition sequence, channel settings
      nano_serial.jsonl              # FRAME stream + CRC32 SYNC exchanges
      gray_alignment.json            # scope↔Nano per-record fit
      epoch_clock_analysis.json      # Nano↔PC per-record local fits
      matrix32_runner.log
      waveforms/
        record_00001_ch1.csv         # physical CH1 samples
        record_00001_ch2.csv         # Nano D7 marker samples
      mcu_matrix32/
        *_tactile_rates.csv          # MCU snapshots / requested RA+SA rates
        *_matrix32_commands.jsonl    # requested and ACKed BLE commands
        *_settings.json              # frozen configuration
      analysis/
        integrated_report.md         # whole-session report
        integrated_summary.json
        mcu_full_stream.png          # entire MCU stream, independent time axis
        record_00001/
          report.md                  # this scope record only
          summary.json
          *.png                      # pulse, frequency and overlay figures
```

**Important time-domain rule:** each scope acquisition has its **own** sample time axis and Nano/PC fit. Continuous MCU/BLE logs start earlier and may extend beyond scope capture. The analysis associates only the provisionally mapped portion of those logs with each scope record; it **does not concatenate scope captures, fill pauses, or infer unobserved pulses across gaps**.

Analyze the latest *completed integrated* session:

```bash
python -u oscilloscope/analyze_integrated_matrix32.py
```

Or specify a run:

```bash
python -u oscilloscope/analyze_integrated_matrix32.py \
  --run-dir data/recordings/<YOUR_RUN_DIRECTORY>
```

The analyzer reports measured **candidate positive-phase pulses** from CH1; per-cycle physical frequencies; BLE ACK-indicated SF; and provisional compatibility/mismatch when timing evidence allows. It also retains a sliding-window view for trends. **BLE ACKs are not physical pulse-edge timestamps**, and protocol success does not verify exact device-application time.

For an integrated **six- or nine-record** session, use `--count 6` or `--count 9` after verifying the full bench and an initial short sequence. Each acquisition is independently synchronized; the oscilloscope is **not recording during waveform transfers** and the Matrix32 controller stays active until the final record. See the [integrated recording guide](docs/integrated_matrix32_scope_recording.md) for the longer watchdog and electrical precautions.

For repeated **Gray-only** capture with pauses (not Matrix32 stimulation), see [Gray acquisition experiments](docs/gray_timecode_scope_experiment.md) and `oscilloscope/test_gray_epoch_pause.py`. For scope-only finite and repeated capture, see [oscilloscope guide](oscilloscope/README.md).

## How time synchronization works

**The Nano does transmit each frame number over USB while emitting the same Gray-coded frame on D7.** The firmware logs:

```text
FRAME <full_counter> GRAY <hex_gray16> EDGE_US <nano_micros> OVERRUNS <count>
```

The scope decodes the Gray pattern on CH2 to recover the low 16 bits of the frame counter. The PC log has the **full counter** and Nano `micros()` sampled immediately after the frame-start D7 transition. Matched physical frame edges yield, for **each scope record**:

```text
Nano_time_seconds ≈ a * scope_time_seconds + b
```

Next, the PC conducts independent **binary CRC32 four-timestamp SYNC** exchanges just before and after acquisition: PC request send `t1`, Nano request receive `t2`, Nano response preparation `t3`, PC response receipt `t4`. These estimate a local Nano-to-PC monotonic mapping:

```text
PC_monotonic_time ≈ c * Nano_time + d
```

Composition maps a scope CH1 sample to an **estimated** PC timestamp and permits a cautious overlay with MCU and BLE logs. The USB serial **FRAME arrival time is not treated as the physical frame-edge time**. Frame matching also exposes sample gaps between distinct scope records; it cannot recover samples that were never acquired.

**Critical limitation:** the scope↔Nano fit RMS and Nano↔PC fit RMS are **regression residuals**, not independent proof of sub-millisecond absolute latency. One-way USB/OS asymmetry leaves an uncalibrated offset. A record may be `unqualified` under the original 100 µs Gray fit criterion and still be useful for scope-local pulse counting.

Read the fully worked [clock-mapping and uncertainty guide](docs/clock_mapping_and_time_domains.md), [Gray protocol/experiments](docs/gray_timecode_scope_experiment.md), and [clock estimator source](communication/clock_sync.py).

## Repository map

| Path | Responsibility |
| --- | --- |
| [`adafruit_feather_m4/`](adafruit_feather_m4/README.md) | Adafruit firmware, CAN taxel assembly, RA/SA encoder, CRC32 telemetry and MCU command ACKs |
| [`arduino_nano/`](arduino_nano/README.md) | Nano Every D7 framed Gray counter and text/binary clock SYNC firmware |
| [`communication/`](docs/pipeline_and_components.md) | Serial parser, USB device discovery, MCU config handshake, MCU drift/compensation, Nano sync |
| [`matrix32/`](docs/pipeline_and_components.md) | BLE link/device API, configuration, ACK checking, timing marker |
| [`oscilloscope/`](oscilloscope/README.md) | Tektronix USBTMC, finite capture, Gray decoding, local alignment, integrated recordings and analysis |
| [`recording/`](docs/pipeline_and_components.md) | Timestamped MCU CSV/manifest recording and Matrix32 command JSONL |
| [`processing/`](docs/pipeline_and_components.md) | Taxel/event processing and lightweight host analysis helpers |
| [`nerve_stimulation/`](docs/pipeline_and_components.md) | Optional interactive/debug Matrix32 controllers and BLE stress tests |
| [`config/`](config/config.json) | Runtime experiment and output configuration |
| [`tests/`](tests/) | Host-side deterministic regression tests; no substitute for live bench validation |
| [`docs/`](docs/) | Experiment procedures, timing evidence, firmware, serial discovery, design notes |
| [`analysis/`](analysis/) | Committed standalone MCU analysis plots; new integrated reports are under each run's `analysis/` |

### Experimental pulse-detection audit

For the new multi-record experiments, the [analysis review workflow](docs/analysis_review_workflow.md) documents the proven Gray-frame decoder, source-preserving Matrix32 **negative-onset detector**, per-record candidate/rejected/suppressed CSV inventories, MCU arrival cadence and BLE ACK statistics, and the unresolved rules for attributing physical periods to ACKed SF states. The offline [`review_pulse_detection.py`](oscilloscope/review_pulse_detection.py) creates **unverified** onset and interval evidence; it does **not** approve all pulses or prove every ACK frequency was output.

## Verification and limitations

- Prefer **software dry run → board/USB check → scope-only check → non-stimulating integrated baseline → explicitly armed isolated dummy-load trial**.
- `--run` opens hardware for scope/Nano recording; it is **not** equivalent to `--arm`. The integrated runner additionally requires `--dummy-load-confirmed` for Matrix32 output.
- A 20-second scope capture with a million points is a **finite acquisition**, not continuous streaming. If multiple acquisitions are saved, the missing intervals remain genuinely **unobserved**.
- Independent physical CH1 pulse timing is meaningful on the scope's own local axis. A combined physical-output-versus-host-command latency estimate is presently **provisional**.
- On a USB/BLE fault use the physical output disconnect and verify the dummy-load output electrically. A Python exception, attempted STOP, BLE acknowledgment or process exit does not guarantee zero output.
- Use `timeout --signal=INT --kill-after=20s 180s` around scope runs because an underlying USBTMC read can block. A process watchdog is not an electrical interlock.
- Preserve the raw waveforms, Nano log, MCU rate CSV, BLE trace and metadata alongside all derived figures; never replace raw data with a fitted timeline.

## Troubleshooting and references

- **Feather/Nano port changed or ambiguous:** `python -m communication.serial_ports`; verify stable `/dev/serial/by-id` paths; see [serial discovery](docs/serial_device_discovery.md).
- **BLE scan failure or `KeyError('Roles')`:** check `bleak==0.22.3`, `bluetoothctl show`, BlueZ daemon status, and close competing BLE clients.
- **No MCU recording:** first valid MCU telemetry and both encoding ACKs are required before files are created. Inspect `serial.port`, firmware mode and cable.
- **No visible CH1 pulses:** confirm dummy-load wiring/probe attenuation, CH1 scale, nonzero selected tactile rate, BLE START/STOP event order and stale-data shutdown. Do not interpret missing data as physical zero without checking the instrument.
- **Unexpected Gray clock RMS:** inspect D7 waveform integrity, trigger position, frame overruns, local fit classification and USB SYNC intervals. A 100 µs qualification threshold is a diagnostic criterion, not a physical absolute-accuracy guarantee.
- **Tests:** `python -m unittest discover -s tests -p 'test_*.py' -v`.
- [CH1 bench wiring](docs/electrical_bench_setup.md) · [Installation and operating guide](docs/installation_and_operations.md) · [System modules](docs/pipeline_and_components.md) · [Timing and Gray-code mapping](docs/clock_mapping_and_time_domains.md) · [Feather firmware](adafruit_feather_m4/README.md) · [Scope scripts](oscilloscope/README.md) · [Integrated experiment](docs/integrated_matrix32_scope_recording.md).

This project is under active experimental development; the actual firmware and acquisition logs are authoritative when old experiment notes conflict with a new implementation.
