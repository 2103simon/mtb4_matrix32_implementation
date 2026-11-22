# Tektronix oscilloscope acquisition and integrated Matrix32 analysis

This module controls a **Tektronix MDO4104B-6** over USBTMC/SCPI, records finite acquisitions, decodes the independent **Arduino Nano Every D7 Gray reference**, and relates those scope observations to the PC's MCU/Matrix32 command logs.

[Repository overview](../README.md) · [Linux installation and safe wiring](../docs/installation_and_operations.md) · [Clock mapping](../docs/clock_mapping_and_time_domains.md) · [Full integrated bench procedure](../docs/integrated_matrix32_scope_recording.md) · [CH1 100 kΩ dummy load and grounding](../docs/electrical_bench_setup.md)

> **Bench hardware warning:** CH1 can measure a hazardous Matrix32 output. Use the documented CH1 100.0 kΩ resistive dummy load and an operator-approved earth-reference/probing arrangement; the source rig uses an earth-referenced TPP1000 passive probe, not intrinsically isolated differential probing. Never connect a grounded scope probe clip arbitrarily to a floating stimulation return or a person. Scope-only or Gray-only tests do not energize the Matrix32; the integrated runner does only with explicit armed flags.

## Scope prerequisites and connection

- Supported target: Tektronix MDO4104B-6 accessible as a Linux `/dev/usbtmc*` node.
- For CH2 Gray timecode: Nano Every D7 digital marker and a reviewed Nano/scope ground/reference circuit; nominal 0–5 V levels. All scope-channel reference leads may share protective earth.
- For integrated recording CH1: voltage across the operator-reported **100.0 kΩ** cathode-to-common-anode resistor; source rig uses a TPP1000 **10×** probe referenced to common anode. Review earth compatibility and probe ratings before use.
- Run a scope script with no `--run` first to print/validate its intended settings without touching devices.
- The scope discovery code [`scope_device.py`](scope_device.py) selects an unambiguous supported instrument and verifies the instrument identity using `*IDN?`. The low-level USBTMC SCPI operations and waveform binary-transfer implementation are in [`tek_mdo_usb.py`](tek_mdo_usb.py).

Use an **external OS watchdog** on instrument runs, since a blocking USBTMC transfer might not respond to the script's polling timeout.

## Which script should I run?

| Program | Hardware used | Purpose |
| --- | --- | --- |
| [`record_scope.py`](record_scope.py) | Scope only | Configurable one-off finite SAMPLE/SEQUENCE CH1–CH4 waveform |
| [`record_scope_live.py`](record_scope_live.py) | Scope only | Repeated **separate** finite scope acquisitions, gaps retained |
| [`test_gray_timecode.py`](test_gray_timecode.py) | Nano + scope | Decode CH2 Gray timecode; optionally multiple captures |
| [`test_gray_epoch_pause.py`](test_gray_epoch_pause.py) | Nano + scope | Three 20-second captures, normally with 60-second pauses and independent pre/post SYNC epochs |
| [`record_integrated_matrix32.py`](record_integrated_matrix32.py) | Feather + Matrix32 BLE + Nano + scope | One 20-second simultaneous CH1 physical output and CH2 Nano reference recording |
| [`decode_gray_timecode.py`](decode_gray_timecode.py) | Offline files | Extract Gray frames and scope-local times from CH2 CSV |
| [`align_gray_records.py`](align_gray_records.py) | Offline files | Match CH2 counter/edge times to Nano USB full counter + `EDGE_US` |
| [`analyze_gray_epoch_experiment.py`](analyze_gray_epoch_experiment.py) | Offline files | Per-record Nano↔PC SYNC fit, quality and drift report |
| [`analyze_integrated_matrix32.py`](analyze_integrated_matrix32.py) | Offline files | Latest integrated session: independent per-record pulse/ACK analysis, plots, Markdown/JSON |
| [`analyze_integrated_session.py`](analyze_integrated_session.py) | Offline library | Correct record boundaries, MCU/ACK overlap windows and gap-aware session summary |
| [`matrix32_dynamic_frequency.py`](matrix32_dynamic_frequency.py) | Offline library | Physical positive-phase interval versus last ACK-indicated SF comparison |
| [`analyze_nano_clock.py`](analyze_nano_clock.py) | Offline files | Passive Nano clock diagnostics |
| [`compare_nano_sync_transports.py`](compare_nano_sync_transports.py) | Offline files | Legacy text versus CRC32 binary SYNC transport comparison |
| [`test_nano_clock_sync.py`](test_nano_clock_sync.py) | Nano (when explicitly run) | Nano clock/serial transport diagnostic |

The name `record_scope_live.py` means **repeated finite acquisitions**, not gap-free continuous raw ADC streaming. The integrated runner currently produces **one** scope acquisition per invocation even though the offline analyzer supports multiple record entries.

## Scope-only finite capture

Print the intended scope configuration:

```bash
python oscilloscope/record_scope.py
```

Capture is initiated only after a deliberate `--run` and appropriate channel configuration. All scope scripts save their own acquisition metadata and downloaded CSV waveforms. The baseline procedure is in [`docs/oscilloscope_ch2_baseline.md`](../docs/oscilloscope_ch2_baseline.md).

For repeated scope-only acquisitions:

```bash
python oscilloscope/record_scope_live.py
# Operator-verified scope-only acquisition:
timeout --signal=INT --kill-after=20s 180s \
  python -u oscilloscope/record_scope_live.py --run
```

The resulting records must not be concatenated as if there were continuous scope samples; see [timing domains and gaps](../docs/clock_mapping_and_time_domains.md).

## Gray timecode, Nano serial and clock experiments

The Nano Every independently emits **one framed 16-bit Gray counter every 100 ms** on D7 and sends the corresponding **full frame counter, Gray word and Nano `micros()` at frame start** over USB serial. The scope sees the actual edge and Gray bits on CH2. Matching those two observations yields one affine **scope→Nano** mapping per captured record.

An independent binary CRC32 four-timestamp SYNC exchange then estimates **Nano→PC monotonic** clock mapping. The integrated runner performs 30 exchanges before and after acquisition; the Gray-only repeated-epoch script uses the same count by default. The PC serial receive time of a `FRAME` is **not** treated as the timestamp of the physical edge.

Gray-only setup and dry-run:

```bash
python oscilloscope/test_gray_timecode.py
python oscilloscope/test_gray_epoch_pause.py
```

Gray-only three-epoch acquisition on verified Nano+scope wiring:

```bash
timeout --signal=INT --kill-after=20s 300s \
  python -u oscilloscope/test_gray_epoch_pause.py --run
```

The deliberate pauses make this run longer than the integrated 20-second run; adjust the **external watchdog** only after reviewing the planned count/pause configuration. The script itself defaults to 3 acquisitions, 20 seconds each, 60-second pauses, and 30 pre/post SYNC exchanges.

Offline:

```bash
python oscilloscope/analyze_gray_epoch_experiment.py \
  data/recordings/<GRAY_EPOCH_RUN>
```

Read [`docs/gray_timecode_scope_experiment.md`](../docs/gray_timecode_scope_experiment.md) for waveform framing, decoder constraints, firmware configuration, and the experimental clock data.

## Integrated Matrix32 + Feather + Nano + scope

**Dry-run plan (no hardware):**

```bash
python oscilloscope/record_integrated_matrix32.py
```

**Non-stimulating baseline** (`--run` only):

```bash
timeout --signal=INT --kill-after=20s 180s \
  python -u oscilloscope/record_integrated_matrix32.py --run
```

**Live isolated dummy-load measurement** after approved wiring, probing and physical disconnect checks:

```bash
timeout --signal=INT --kill-after=20s 180s \
  python -u oscilloscope/record_integrated_matrix32.py \
  --run --arm --dummy-load-confirmed
```

The integrated sequence is:

1. Start Nano D7 Gray reference.
2. Configure scope CH1, CH2, CH2 +1 V rising trigger, 0% pretrigger, horizontal delay OFF, finite SEQUENCE acquisition (**scope remains stopped**).
3. Connect MCU/Matrix32 controller; wait for MCU encoding ACKs and, if armed, Matrix32 START completion. A fresh positive tactile rate is required for actual output.
4. Take **30 binary CRC32 SYNC** exchanges with the Nano.
5. Arm scope; acquire one nominal **20-second** record (1,000,000 samples and 2 s/div).
6. Take 30 post-recording SYNC exchanges, request Matrix32 STOP, download CH1+CH2 after output shutdown attempt, and preserve raw data.
7. Compute diagnostic per-record Gray/SYNC alignment, which may be **unqualified** under the legacy 100 µs scope RMS threshold without invalidating raw scope-local pulse measurements.

A software STOP/ACK does not establish that electrical output is absent: verify CH1 and use physical disconnection when required.

## Offline analysis and report layout

```bash
python -u oscilloscope/analyze_integrated_matrix32.py
python -u oscilloscope/analyze_integrated_matrix32.py \
  --run-dir data/recordings/<INTEGRATED_RUN>
```

Output:

```text
<run>/analysis/
  integrated_report.md       # combined session summary (not a merged waveform)
  integrated_summary.json
  mcu_full_stream.png        # continuous MCU plot on its own host axis
  record_00001/
    report.md                # this acquisition only
    summary.json
    integrated_overview.png
    matrix32_pulse_zoom.png
    matrix32_frequency_comparison.png
    matrix32_dynamic_tracking.png
  record_00002/              # only for a future N>1 integrated scope session
    ...
```

**Per record**, the analyzer loads its CH1 waveform and matching record-index Nano/host fit; calculates candidate physical positive-phase pulse count, interval and rate; selects only MCU snapshots and BLE events within that record's provisionally mapped host window; and compares physical pulse periods with the most recent ACK-indicated SF.

**At session level**, the analyzer counts the full MCU and BLE logs **once**, sums *observed* scope durations and pulse counts, and explicitly reports gaps. It does **not** join pulse intervals across recordings or recover samples that were never acquired.

A 1-second sliding-window frequency trend is useful for overview but can produce zero eligible *steady-state* windows when SF changes every few tens of milliseconds. The separate per-cycle dynamic analysis handles rapid changes cautiously: ACK transitions, muted intervals and times near updates are excluded or marked not comparable. All resulting cross-clock classifications are **provisional** because USB one-way asymmetry and physical Matrix32 apply times remain uncalibrated.

## Tests

```bash
python -m unittest discover -s tests -p 'test_integrated_matrix32_recording.py' -v
python -m unittest discover -s tests -p 'test_analyze_integrated*.py' -v
python -m unittest discover -s tests -p 'test_gray*.py' -v
```

For troubleshooting inspect `metadata.json`, `matrix32_runner.log`, `nano_serial.jsonl`, the CH2 physical waveform, scope SCPI settings before/after capture, and the original CH1 waveform before changing thresholds or making latency claims.
