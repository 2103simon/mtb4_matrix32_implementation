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
| [`record_integrated_matrix32.py`](record_integrated_matrix32.py) | Feather + Matrix32 BLE + Nano + scope | One or more separate 20-second CH1 physical output + CH2 Nano reference records under one continuous controller session |
| [`decode_gray_timecode.py`](decode_gray_timecode.py) | Offline files | Extract Gray frames and scope-local times from CH2 CSV |
| [`align_gray_records.py`](align_gray_records.py) | Offline files | Match CH2 counter/edge times to Nano USB full counter + `EDGE_US` |
| [`analyze_gray_epoch_experiment.py`](analyze_gray_epoch_experiment.py) | Offline files | Per-record Nano↔PC SYNC fit, quality and drift report |
| [`analyze_integrated_matrix32.py`](analyze_integrated_matrix32.py) | Offline files | Latest integrated session: independent per-record pulse/ACK analysis, plots, Markdown/JSON |
| [`analyze_integrated_session.py`](analyze_integrated_session.py) | Offline library | Correct record boundaries, MCU/ACK overlap windows and gap-aware session summary |
| [`matrix32_dynamic_frequency.py`](matrix32_dynamic_frequency.py) | Offline library | Physical positive-phase interval versus last ACK-indicated SF comparison |
| [`analyze_nano_clock.py`](analyze_nano_clock.py) | Offline files | Passive Nano clock diagnostics |
| [`compare_nano_sync_transports.py`](compare_nano_sync_transports.py) | Offline files | Legacy text versus CRC32 binary SYNC transport comparison |
| [`test_nano_clock_sync.py`](test_nano_clock_sync.py) | Nano (when explicitly run) | Nano clock/serial transport diagnostic |

The name `record_scope_live.py` means **repeated finite acquisitions**, not gap-free continuous raw ADC streaming. The integrated runner accepts **`--count N` (1–12)** and produces N independent scope acquisitions while retaining the same MCU/Matrix32 controller and Nano generator. It downloads each frozen record before rearming, so there are unavoidable **unobserved acquisition gaps**; the offline analyzer already supports independent record entries.

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

## Supported record lengths and higher-resolution captures

The operator verified these **discrete record-length selections** on the Tektronix MDO4104B-6: **1,000; 10,000; 100,000; 1,000,000; 5,000,000; 10,000,000; 20,000,000 points**. These are instrument menu choices, not an assertion that every timebase/channel combination will accept every length. The runner requests a length and then checks the *instrument-reported* record length and horizontal scale before acquiring; a mismatch aborts the acquisition.

| Proposed single acquisition | Timebase (10 divisions) | Points per channel | Nominal sample spacing | Samples per 100 µs phase | Raw two-channel 16-bit transfer |
| --- | --- | ---: | ---: | ---: | ---: |
| Existing baseline: 20 s | 2 s/div | 1M | 20 µs | 5 | 4 MB |
| Better first trial: 30 s | 3 s/div | 5M | 6 µs | ~17 | 20 MB |
| Longer / finer: 60 s | 6 s/div | 20M | 3 µs | ~33 | 80 MB |

The sample spacing above is **nominal** (window/points), not a measured sample rate or proof that the scope accepts 3 s/div or 6 s/div at these record lengths. The scope may quantize/reject the horizontal scale; the existing 2.5% scale verification intentionally fails if the requested setting was not honored. Check the dry-run plan, then verify the scope's reported settings and waveform preamble in a non-stimulating trial. For a scope restricted to standard 1-2-5 divisions, a 50 s capture at 5 s/div is an alternative to 60 s at 6 s/div (20M/50 s = 2.5 µs); don't relabel 50 s as 60 s.

At 400 Hz a 30 s capture contains roughly 12,000 pulse periods and a 60 s capture roughly 24,000. Higher sample density improves phase-edge localization, short-pulse detection and event-review zooms; longer windows provide more transition examples. Neither improves the **~44 Hz BLE command update rate**, ACK-to-physical latency, or cross-clock alignment automatically. The CH2 Gray reference still sends one frame per 100 ms. Record-length upgrades can also expose trigger-position, memory sharing, transfer and processing limits.

Two-channel raw payload estimates above exclude CSV: the current downloader expands each sample to **three text columns** and materializes a complete tuple of 16-bit samples; CSV size, RAM and transfer/analysis time can be much larger. At 20M points × 2 channels, validate disk headroom, RAM and external watchdog duration in a no-stimulation capture first. The current CSV validator also reads an entire waveform into memory. Frozen-record downloads create gaps between successive records; a *single* longer record avoids those gaps but does not make the BLE sequence continuous or prove the waveform's timing relative to MCU host timestamps.

After checking hardware acceptance, example **dry-run plans**:

```bash
python oscilloscope/record_integrated_matrix32.py --points 5000000 --time-div-us 3000000 --count 1
python oscilloscope/record_integrated_matrix32.py --points 20000000 --time-div-us 6000000 --count 1
```

Live acquisitions still require the existing operator-confirmed dummy-load, `--run --arm --dummy-load-confirmed` gating and an appropriately increased external watchdog; no long-record hardware acceptance or live-run performance has yet been demonstrated.

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

**Live CH1 dummy-load measurement** after approved wiring, probing and physical disconnect checks (one 20-second record):

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

For a longer sequence, use `--count 6` (six × 20 s, **120 s observed scope time**) or `--count 9` (nine × 20 s, **180 s observed scope time**). The overall session is longer than that because of Nano pre/post SYNC and **two-channel waveform transfers** between acquisitions. For example, once a two-record dry-run/bench trial has been validated:

```bash
# One MCU/Matrix32 session and nine independent CH1+CH2 scope records.
# 900 seconds is an external wall-clock guard, not the requested scope duration.
timeout --signal=INT --kill-after=20s 900s \
  python -u oscilloscope/record_integrated_matrix32.py \
  --run --arm --dummy-load-confirmed --count 9
```

**Electrical safety:** Matrix32 output may remain active during the **unobserved USBTMC waveform download gaps**. Software STOP is requested only after the final capture, or on a caught error/interruption. The external shell watchdog cannot guarantee an electrical STOP if blocking USBTMC calls or process termination bypass Python cleanup. Maintain an independent physical disconnect and monitor the output appropriately. A software STOP/ACK does not establish that electrical output is absent.

### Experimental pulse review before frequency attribution

The [analysis review workflow](../docs/analysis_review_workflow.md) describes source-locked negative-onset detection from the related Matrix32 project. Run [`review_pulse_detection.py`](review_pulse_detection.py) on saved CH1 recordings to export accepted, rejected and suppressed pulse candidates, inverse-period estimates and raw zooms for manual audit. The historical 2 V positive-phase analyzer remains separately available; no candidate is automatically approved as a verified physical pulse.

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

## Zero-frequency combined-write feasibility

Cross-reference: `2103simon/matrix32_nerve_stimulation`, branch `cleanup/analysis-consolidation`, especially `docs/live_frequency_updates.md` and `docs/matrix32_ble_stack_fingerprint_research.md`. The related repository retains the Matrix32 V1.3 manual.

The supported command encoder sends a full 32-channel `>SC;...<` current vector and a separate `>SF;...<` global-frequency frame. There is **no established combined/atomic SC+SF command**. The Matrix32 returns generic `>OK<` indications without a command identifier. Concatenating frames into one BLE write would not establish that both were parsed or accepted, and moving SF=400 before SC=0 has physically taken effect could temporarily increase the output rate at the old current.

The qualified optimized SF transport has one outstanding command and a minimum 7.5 ms ACK-to-next-write interval. Short-gap experiments found a link stall. **Decision:** retain sequential ACK-verified SC=0 then SF=400; do not introduce unqualified packet batching or drop either application ACK. Neither ACK proves electrical output is zero. A combined mode requires explicit protocol support and a separate dummy-load transition/ACK qualification before deployment.

## Tests

```bash
python -m unittest discover -s tests -p 'test_integrated_matrix32_recording.py' -v
python -m unittest discover -s tests -p 'test_analyze_integrated*.py' -v
python -m unittest discover -s tests -p 'test_gray*.py' -v
```

For troubleshooting inspect `metadata.json`, `matrix32_runner.log`, `nano_serial.jsonl`, the CH2 physical waveform, scope SCPI settings before/after capture, and the original CH1 waveform before changing thresholds or making latency claims.
