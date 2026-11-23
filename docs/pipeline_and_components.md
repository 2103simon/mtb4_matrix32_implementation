# Software architecture, protocols and module ownership

Start with the [top-level README](../README.md), then use this reference to find the module that owns an observed behavior or experiment. The primary development branch is `main`; not every experiment script is an entry point for clinical or human use.

## End-to-end runtime

```text
CAN tactile sensor → Feather M4 ISR queue
        → 12-taxel assembler → RA-I / SA-II encoding
        → CRC32 USB type 0x05 snapshots
        → PC SerialManager → ACK handshake → bounded latest-rate consumer
        → Matrix32 BLE frequency worker (SF update on change)
        → actual CH1 output → scope analog waveform

Nano Every D7 Gray markers → scope CH2
Nano USB full frame index/EDGE_US + CRC32 SYNC ↔ PC monotonic ns
        → independent scope↔Nano↔PC mapping for each scope record
```

## Feather M4 firmware: [`adafruit_feather_m4/`](../adafruit_feather_m4/README.md)

- [`src/main.cpp`](../adafruit_feather_m4/src/main.cpp): Adafruit Feather M4 CAN Express CAN startup, receive ISR, bounded 32-frame queue, command parser, CRC-framed serial transport, foreground event serialization, and MCU control loop.
- [`src/taxel_assembler.cpp`](../adafruit_feather_m4/src/taxel_assembler.cpp) and [`include/taxel_assembler.h`](../adafruit_feather_m4/include/taxel_assembler.h): pair the 0x40/0xC0 halves of the MTB4 CAN sample, preserve the 12 taxel positions and invert the incoming 8-bit values.
- [`src/encoding.cpp`](../adafruit_feather_m4/src/encoding.cpp) and [`include/encoding.h`](../adafruit_feather_m4/include/encoding.h): RA-I/SA-II rate and event algorithms, parameters, refractory logic.
- [`src/event_transport.cpp`](../adafruit_feather_m4/src/event_transport.cpp): event packet batch handling.
- [`src/diagnostic_telemetry.cpp`](../adafruit_feather_m4/src/diagnostic_telemetry.cpp): periodic diagnostic telemetry helpers.
- [`platformio.ini`](../adafruit_feather_m4/platformio.ini): PlatformIO environment `adafruit_feather_m4_can`.

The ISR only queues CAN data. Serial writes, packet assembly and encoding run in the main/foreground context so interrupt timing is not burdened by protocol I/O.

### Feather ↔ host wire protocol

All listed traffic is framed as:

```text
length (2 bytes BE) | type (1 byte) | payload (variable) | CRC32 (4 bytes BE)
```

CRC is calculated over `type + payload`, not over the framing length. The host [`SerialManager`](../communication/serial_manager.py) validates length and CRC before decoding.

| Type | Direction | Payload/function |
| --- | --- | --- |
| `0x02` | Feather → host | timestamp (u32 BE) + 12 taxel bytes |
| `0x04` | Feather → host | reference timestamp + event IDs and 16-bit offsets |
| `0x05` | Feather → host | timestamp + 12 taxels + 12 RA rates + 12 SA rates; rates encoded as u32 BE **centi-Hz** |
| `0x10` | Host → Feather | parameter command ID plus optional u16 BE value |
| `0x11` | Feather → host | command ID + status + applied u16 value |
| `0x12` | Feather → host | diagnostic telemetry, when emitted/enabled by firmware |

The host command IDs include `0x01` RA threshold, `0x02` refractory setting, `0x03` SA maximum frequency, and `0x04` read/configuration request. Before accepting recording data, the host checks ACKs for **RA threshold and SA maximum frequency** against `config/config.json`.

The experimental update cadence has been approximately **44.5 Hz** (about 22.5 ms per sample), but the implementation does not hard-code that rate into Matrix32 pulse generation. The selected MCU `ra_i` or `sa_ii` rate is an input to an independent BLE frequency command worker. See [detailed tactile rate protocol](tactile_rate_mcu_v2.md) and [Feather guide](../adafruit_feather_m4/README.md).

## PC communication: [`communication/`](../communication/)

| Module | Ownership |
| --- | --- |
| [`serial_ports.py`](../communication/serial_ports.py) | Passive, role-aware USB serial discovery for Feather/Nano |
| [`serial_manager.py`](../communication/serial_manager.py) | CRC32 Feather telemetry parsing, connection, timestamped snapshots and latest-value access |
| [`mcu_configuration.py`](../communication/mcu_configuration.py) | ACK-verified initial RA/SA MCU parameter handshake |
| [`clock_compensator.py`](../communication/clock_compensator.py), [`clock_drift_monitor.py`](../communication/clock_drift_monitor.py) | Separate Feather MCU/PC time diagnostics and compensation |
| [`clock_sync.py`](../communication/clock_sync.py), [`clock_estimator_diagnostics.py`](../communication/clock_estimator_diagnostics.py) | Nano USB SYNC slope/offset estimation and uncertainty diagnostics |
| [`nano_binary_protocol.py`](../communication/nano_binary_protocol.py) | CRC32 binary clock SYNC packing, parsing and validation |
| [`instrumentation.py`](../communication/instrumentation.py) | Instrumentation/diagnostic helpers |

### Distinguish the clocks

- Feather packets carry an MCU-side millisecond timestamp, corrected or diagnosed via the host's MCU clock-compensation tools.
- Nano packets carry Nano `micros()` plus the full Gray counter.
- `time.monotonic_ns()` on the PC timestamps MCU arrivals and Matrix32 command/ACK logging.
- Scope CH1 and CH2 have acquisition-relative time. The **composition** of per-record Gray↔Nano and Nano↔PC fits yields a provisional relationship to PC monotonic time.

See [clock mapping and uncertainty](clock_mapping_and_time_domains.md).

## Matrix32: [`matrix32/`](../matrix32/) and controller scripts

- [`matrix32/device.py`](../matrix32/device.py): BLE GATT device API, readiness, configuration, start/stop, live frequency update.
- [`matrix32/ble.py`](../matrix32/ble.py): Linux BLE discovery and link helpers.
- [`matrix32/config.py`](../matrix32/config.py): Matrix32 configuration serialization.
- [`matrix32/ack.py`](../matrix32/ack.py): ACK interpretation and verification.
- [`matrix32/timing_marker.py`](../matrix32/timing_marker.py): optional serial timing-marker integration.

Runnable entry points:

| Script | Effect |
| --- | --- |
| [`connect_matrix32.py`](../connect_matrix32.py) | BLE connection/battery-only diagnostic; **never** starts stimulation |
| [`run_tactile_stimulation.py`](../run_tactile_stimulation.py) | MCU-selected taxel rate → Matrix32 CH1 SF worker; **dry-run unless `--arm`**; `--record` logs MCU and BLE events |
| [`nerve_stimulation/e_matrix32.py`](../nerve_stimulation/e_matrix32.py) | Interactive Matrix32 command console (operator-controlled; **not** the integrated recorder) |
| [`nerve_stimulation/ble_link_stress.py`](../nerve_stimulation/ble_link_stress.py) | BLE stress/diagnostic experiments, **not** the normal recording path |
| [`nerve_stimulation/test_connection.py`](../nerve_stimulation/test_connection.py) | Additional connection test utility |

In the controlled live path, the PC starts the selected stimulation configuration only after verifying the MCU's current input and the Matrix32 configuration ACKs. It sends ACK-checked `SF` changes sequentially and avoids sending the same SF on every unchanged MCU snapshot. For requested rates below 2 Hz it treats output as muted (SC1=0, then SF=400 after SC ACK for shortest permitted muted scheduler period (not a guaranteed immediate physical off)) instead of demanding an unsupported SF1 START. On stale input (default **250 ms**) or failure it attempts STOP, but STOP confirmation is software-side, **not a measurement of electrical output**.

The integrated target is Matrix32 **channel 1** on the documented [100.0 kΩ cathode-to-common-anode dummy load](electrical_bench_setup.md). The referenced TPP1000 passive-probe ground is earth-referenced and must not be described as intrinsically isolated. Never infer current or delivered charge from `currentCommand` alone.

## Recording: [`recording/`](../recording/)

- `data_recorder.py`: CSV rows and frozen `*_settings.json` manifest for MCU snapshots. It starts only after both MCU configuration ACKs.
- `matrix32_trace.py`: monotonic-timestamped Matrix32 command/ACK JSONL capture (if `--record` enabled).
- `read_mcu.py`: standalone host MCU recorder; source and log destination are defined in `config/config.json`.
- `analyze_mcu_recording.py`: standalone MCU taxel, RA/SA rate plots, and optional Omega alignment when that source was separately recorded.

A new integrated session creates a unique parent directory and directs its MCU/Matrix32 controller subprocess to `mcu_matrix32/` under that parent; the oscilloscope recordings are under `waveforms/`.

## Oscilloscope and Nano reference

See [scope module guide](../oscilloscope/README.md), [Nano guide](../arduino_nano/README.md), [Gray clock mapping](clock_mapping_and_time_domains.md), and [integrated recording procedure](integrated_matrix32_scope_recording.md).

Key files:

- `oscilloscope/tek_mdo_usb.py`: Tektronix binary waveform transfer and SCPI I/O.
- `oscilloscope/scope_device.py`: scope USBTMC discovery and identity check.
- `oscilloscope/record_scope.py`: generic finite scope-only acquisition.
- `oscilloscope/record_scope_live.py`: repeated independent finite-record scope acquisitions **with gaps**.
- `oscilloscope/test_gray_timecode.py`, `test_gray_epoch_pause.py`: Nano Gray acquisition experiments; the latter defaults to three 20-second acquisitions with planned pauses.
- `oscilloscope/decode_gray_timecode.py`, `align_gray_records.py`: decode CH2 and associate serial Nano `FRAME` edges.
- `oscilloscope/analyze_gray_epoch_experiment.py`: independent per-record active Nano-to-PC SYNC fits.
- `oscilloscope/record_integrated_matrix32.py`: one finite simultaneous CH1+CH2 record with concurrent MCU/Matrix32 controller.
- `oscilloscope/analyze_integrated_matrix32.py`, `analyze_integrated_session.py`, `matrix32_dynamic_frequency.py`: independent record analysis and session-level reporting.

## Processing and tests

`processing/taxel_processing.py`, `event_processing.py`, and `data_analysis.py` are small host-side helpers for taxel/event data and legacy analysis. These are not the firmware or the Matrix32 stimulation loops.

Host tests live under [`tests/`](../tests/). Run:

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

When modifying firmware, also build the actual PlatformIO project. Passing Python tests does not guarantee an MCU flash or oscilloscope timing calibration. Before integrating a new experiment, verify both the raw protocol fields and the saved metadata record boundaries.

## Maintenance guidance

1. Identify the correct owning module above; avoid duplicating clock estimators, independent record mapping, or BLE ACK handling in an application script.
2. Keep wire-format changes synchronized between Feather firmware and host parser/ACK tests.
3. Keep Nano D7 timecode separate from stimulation output; any change to marker positions requires a matching CH2 decoder test.
4. Treat each scope acquisition as an atomic record. Do not concatenate local scope time axes or interpolate gaps to make apparent continuous sampling.
5. Preserve source CSV/JSONL and experiment configuration so results can be reanalyzed without another physical trial.
6. When reporting accuracy, separate within-scope sample timing, scope↔Nano residuals, Nano↔PC offset uncertainty, and true physical device latency.
