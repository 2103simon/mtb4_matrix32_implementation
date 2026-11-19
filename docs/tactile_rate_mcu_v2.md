# MCU rate-streaming v2 — implementation and hardware checklist

Branch: `feature/tactile-rate-streaming-v2` (branched from `feature/tactile-rate-streaming`, which contains the new modular MCU firmware).  
Status: code committed; **PlatformIO build, native tests and hardware reception not yet verified here**.

## Scope and decisions

Our objective is latest-value tactile-to-Matrix32 control (MCU CAN 12 taxels -> Python -> Matrix32 CH1). This increment changes the MCU only. Rates are **requested unconstrained rates**; Python will later apply refractory and Matrix32 device limitations. No Matrix32 stimulation is performed here.

The new firmware layout divides CAN assembly (`taxel_assembler.cpp`), encoding (`encoding.cpp`), event transport (`event_transport.cpp`) and diagnostics; do not replace these modules with the previous monolithic firmware.

## Changes

- `include/firmware_mode.h`: `MCU_RATE_STREAMING` defaults to `1`. Set it to `0` to restore the previous active taxel + event transmission for regression experiments.
- `include/encoding.h` / `src/encoding.cpp`: new `calculateTactileRates(taxels, sampleDtMs, raRates, saRates)`, used in the MCU foreground only.
- `src/main.cpp`: one `0x05` snapshot per complete two-CAN-frame MTB4 sample. The original `mechanoreceptorEncoding` and `sortAndSendEvents` functions are retained and called only with rate mode disabled.
- **Unchanged:** ISR queue, CAN discovery, taxel assembler, phase-aware legacy SA-II event scheduler, event transport, command/ACK parsing, periodic `0x12` diagnostics.
- `CMD_REF` remains accepted and its value is stored for legacy event mode, but it does **not** limit rates in rate-streaming mode.

## Rate semantics

`sa_hz[i] = taxel[i] * saParams.slope`. With default `300/255`, full-scale=300 Hz. Updated by existing `SA_FREQ` MCU command.

RA-I uses the `raParams.threshold` (default 3) and the original **accumulating threshold reference** semantics. For `change = abs(current - reference)`: if `change >= threshold`, `requested_spikes = floor(change/threshold)`, then advance the reference to the current taxel; otherwise emit zero and retain reference. Requested `ra_hz = requested_spikes * 1000 / dt_ms`. This is **not** a refractory-filtered historical count. First acquired sample initializes the RA reference and reports RA=0. Zero or invalid elapsed intervals report RA=0; SA remains meaningful.

Values are rounded to nearest 0.01 Hz on the wire. RA and SA are separate arrays for all 12 channels.

## Binary protocol (compatible with existing v1 Python parser on parent branch)

Each `0x05` packet uses the existing outer framing: 2-byte big-endian length (113 = type + payload), 1-byte type, 112-byte payload, 4-byte CRC32 big-endian over type + payload. Total 119 bytes.

| Payload offset | Data | Bytes |
| ---: | --- | ---: |
| 0 | MCU timestamp ms, uint32 BE | 4 |
| 4 | taxels[12], uint8 | 12 |
| 16 | RA-I rates[12], uint32 BE centi-Hz | 48 |
| 64 | SA-II rates[12], uint32 BE centi-Hz | 48 |

The previous Python `communication/serial_manager.py` already has `FRAME_TYPE_TACTILE_RATES = 0x05`, decodes this exact layout, and delivers `("tactile_rates", snapshot)`; `read_mcu.py --print-every 1` prints rate snapshots. It was introduced earlier on the parent feature branch and is not newly modified in this increment.

Existing diagnostic `0x12` frames are still emitted every ten seconds. Event-related diagnostic counters will remain zero in rate mode.

## Fixed MTB4 acquisition period

The experimental `E06_MTB4_PERIOD_MS` compile-time selector has been removed. During startup the Feather now always requests a **10 ms MTB4 skin-service transmit period (100 Hz nominal)** using `SKIN_SET_BRD_CFG`. This configures the MTB4 at runtime; it does not flash MTB4 firmware.

**Throughput consideration:** a 119-byte rate snapshot at 100 Hz is **11,900 bytes/s**, excluding periodic diagnostics and USB overhead. This exceeds the nominal **11,520 bytes/s** payload capacity of a conventional 115200-baud 8N1 UART. The Feather's `Serial` may be USB CDC (where the configured baud is not necessarily a physical line-rate limit), so actual sustained throughput and backpressure **must be measured** before assuming all 100 samples/s reach Python without queuing.

## Validation

1. Switch to this branch: `git fetch origin && git switch feature/tactile-rate-streaming-v2 && git pull`.
2. Run Python/native tests from project root: `python -m unittest discover -s tests -v`. New `tests/test_mcu_rate_encoding.py` compiles the real `encoding.cpp` with g++, asserting startup RA=0, threshold accumulation, SA scaling, zero dt, runtime SA slope, and no refractory cap. An unavailable g++ skips the native test.
3. Build firmware: `cd adafruit_feather_m4 && pio run`, then flash with `pio run -t upload` after checking the selected port and physical setup.
4. From repository root, start `python read_mcu.py --print-every 1`. Verify successful CRC frame parsing, taxels 0..255, SA relation to configured slope, RA transients on pressure changes, and sample timestamps.
5. Review saved `data/*_tactile_rates.csv`; check inter-sample intervals and actual throughput. No physical Matrix32 output in this stage.

**Unverified:** GitHub source edits were possible, but this environment could not access github.com from the code container (DNS resolution failure). Compilation, tests and hardware behavior have therefore not been claimed as passing.

## Plot recorded taxels and rates

`analyze_mcu_recording.py` now automatically discovers the most recently modified `*_tactile_rates.csv` in the configured recordings directory. It plots three synchronized time-series panels: **raw taxel values**, **RA-I requested rates in Hz**, **SA-II requested rates in Hz**. This replaces the spike raster on new rate recordings, while legacy taxel/event files still use the old raster plot.

Run from the repository root:

- `python analyze_mcu_recording.py` — newest rate recording; all 12 channels.
- `python analyze_mcu_recording.py --taxel 3` — inspect one taxel more clearly.
- `python analyze_mcu_recording.py --datetime 20261119_082300` — choose recording by ID.
- `python analyze_mcu_recording.py --zoom-sec 1.0` — plot the most active 1-second pressure window.
- `python analyze_mcu_recording.py --pc-time` — use arrival-time axis instead of corrected MCU time.

Outputs are saved in `analysis/<id>_traces*.png`. If an associated Omega recording exists, a six-panel combined figure is also saved, with rate traces instead of spike rasters. Plot tests: `python -m unittest discover -s tests -v` (includes `tests/test_analyze_rate_recording.py`). **These changes have not been executed in the user's environment yet.**

## Fixed encoding and CH1 source configuration

`config/config.json` no longer contains GUI slider metadata. It contains:

```json
"tactile_encoding": {
  "raThreshold": 3,
  "saMaxFrequencyHz": 150
},
"stimulation_source": {
  "taxel": 0,
  "encoding": "sa_ii"
}
```

- `raThreshold`: integer **1–255**, applied to MCU using `RA_THR`.
- `saMaxFrequencyHz`: integer **10–1000 Hz**, applied to MCU using `SA_FREQ`.
- `stimulation_source.taxel`: integer **0–11**, using zero-based taxel indexing.
- `stimulation_source.encoding`: **`ra_i`** or **`sa_ii`**.
- `read_mcu.py` validates these values, sends the two MCU encoding commands **after receiving link-ready telemetry**, and prints the selected channel's requested frequency during acquisition. Existing ACK messages show whether the MCU accepted each setting.
- The selected source is **not yet connected to Matrix32**; no stimulation starts as a result of these settings. The current source selection is a preparation for the single-channel CH1 stimulation worker.

The default encoding maximum is **150 Hz**, matching the former slider default; the MCU's internal startup default remains 300 Hz until the host sends its command. This initial period can appear in early telemetry.

## MCU configuration handshake before recording

`read_mcu.py` now waits for the first valid MCU telemetry frame before sending `RA_THR=<config value>` and `SA_FREQ=<config value>` through `SerialManager.send_command()`. **No recording files are opened and no samples are recorded until both successful ACKs are received**. Each ACK must include the corresponding command ID, success status **1**, and the exact requested applied value. ACK order is arbitrary.

- Console prints each ACK and announces `Both MCU settings confirmed; recording started.`
- Data queued before the second ACK is discarded, including any frames generated with startup defaults.
- Missing ACKs cause a configuration error after 5 seconds. Initial telemetry not received within 15 seconds also fails. Command write errors, rejection, wrong applied values, unexpected command ACKs, or premature disconnection all stop without starting recording.
- The `communication/mcu_configuration.py` handshake is separately unit-tested by `tests/test_mcu_configuration.py`. The serial sender now returns success/failure to allow early abort.
- `stimulation_source` remains selection-only; this does not operate Matrix32.

## Self-describing experiment recordings and analysis figures

For every run successfully configured via both MCU ACKs, `read_mcu.py` now starts recording with a descriptive run ID:

```text
20261119_101112_ra5_sa200Hz_t7_rai
```

The ID contains the local acquisition start date/time, confirmed RA threshold (5), confirmed SA maximum (200 Hz), selected taxel (7), and selected encoding (`rai` or `saii`). Changing the configuration before the next run creates a different descriptive ID. The timestamp is assigned **after both successful ACKs**, when recording begins.

The files in the configured `recordings_dir` use this exact prefix, for example:

- `<run_id>_tactile_rates.csv` — all 12 raw taxels and unconstrained RA/SA rates.
- `<run_id>_taxel.csv` and `<run_id>_events.csv` — legacy compatibility outputs (header-only with the rate-streaming MCU).
- `<run_id>_settings.json` — versioned, frozen manifest with a copy of the entire experiment `config/config.json`, ACK-confirmed applied encoder values, recording start ISO timestamp, protocol description, recording ID and CSV filenames.

The metadata is saved **with the data**, rather than inferred from whatever `config/config.json` contains when analysis is run later. `analyze_mcu_recording.py` loads the same manifest when available, prints the settings, annotates plot titles, and preserves the run ID in all figure filenames. For example: `analysis/20261119_101112_ra5_sa200Hz_t7_rai_traces.png` and `_traces_zoom.png`. The `--datetime` analysis argument accepts a **full run ID** (including suffix), while omitting it still selects the newest recording. Old timestamp-only recordings without a JSON manifest remain supported.

The writer refuses to reuse an existing run ID instead of silently overwriting data. Run `python -m unittest discover -s tests -v` (includes `tests/test_recording_manifest.py`) to check metadata integrity and analysis naming. Changes are committed but not yet confirmed in a local user test session.

## Matrix32 connection-only diagnostic

`connect_matrix32.py` is the first Matrix32 integration increment. It calls the **existing** `Matrix32Device.connect()` (scans/retries, GATT discovery, 3-second stabilization, non-stimulating battery-report round trip) and then holds the BLE event loop open. It prints a heartbeat every 5 seconds, exits on unexpected disconnection, and attempts `Matrix32Device.disconnect()` in `finally` on Ctrl+C or other errors.

```bash
python connect_matrix32.py
```

Before connection attempts, the script now verifies that Linux BlueZ is active and D-Bus-accessible (using the existing `check_bluetooth_service()` helper), then confirms that the default PC Bluetooth adapter reports `Powered: yes` through `bluetoothctl show`. When either preflight check fails, it exits with a terminal error before attempting a Matrix32 BLE connection. Recovery commands are `sudo systemctl start bluetooth` or `bluetoothctl power on` as appropriate. Unit tests mock both checks so no physical adapter is required.

There are **no calls to** `apply()`, `start()`, `apply_live_frequency()`, or other stimulation commands. No automatic reconnection is attempted. This standalone diagnostic does not use the MCU serial port and does not create a recording. Offline mock-based tests in `tests/test_connect_matrix32.py` cover cancellation, connection loss and failed readiness; physical BLE behavior must be verified separately.

## Bleak / BlueZ compatibility check

The Matrix32 BLE code (`matrix32/device.py`, `matrix32/ble.py`) and `requirements.txt` match the reference repository at `cleanup/analysis-consolidation`. Its BLE troubleshooting handoff records that **Bleak 3.0.2 with BlueZ 5.53** caused repeatable scanner `KeyError('Roles')`, which was resolved with **`bleak==0.22.3`**.

`connect_matrix32.py` now checks the active Python environment's installed Bleak distribution **before scanning**, and stops with an installation instruction when the version differs from the validated `0.22.3`. Check your environment using `python -m pip show bleak`, and install the pinned version with `python -m pip install 'bleak==0.22.3'`. This check is intentionally scoped to the connection-only diagnostic; existing Matrix32 library code remains unchanged.

## First combined tactile -> Matrix32 CH1 implementation

`run_tactile_stimulation.py` combines the existing MCU telemetry/parser and ACK handshake with the verified Matrix32 BLE device object. It is **dry-run by default**, records all rate samples, and prints the selected frequency without enabling stimulation. The same `config/config.json` `stimulation_source.taxel` (0–11) and `stimulation_source.encoding` (`ra_i` / `sa_ii`) select the only rate forwarded to **Matrix32 CH1**.

The new `config/stim_tactile_ch1.yaml` is an isolated *single-channel* setup. It intentionally starts with `sc: [0]` and `sw: [0]` to prevent inadvertent stimulation; before using `--arm`, set approved bench current and pulse-width values, and independently verify `sv`, `st`, pulse morphology and output wiring. No output parameters are automatically borrowed from the multi-channel `stim_config.yaml`.

Run using the already-validated Matrix32 Python environment:

```bash
/home/icub/.virtualenvs/matrix32/bin/python run_tactile_stimulation.py
# Explicit output enabling only after safe CH1 parameters are configured:
/home/icub/.virtualenvs/matrix32/bin/python run_tactile_stimulation.py --arm
```

Behavior:

- Verify Bleak 0.22.3 and powered PC Bluetooth adapter; establish stable Matrix32 BLE and battery response; open MCU USB serial.
- Send `RA_THR` and `SA_FREQ`, await **both exact successful MCU ACKs**, then start CSV and JSON manifest recording. The JSON contains the full MCU configuration **plus** Matrix32 CH1 configuration, source, arm flag and stale timeout.
- Keep recording **every rate snapshot** through the normal queue; drive stimulation separately from `get_latest_rates_snapshot()` to avoid stale-sample replay.
- Ignore any latest sample timestamped before the MCU configuration ACK gate.
- For Matrix32, round the selected requested rate to whole Hz, clip to **400 Hz**, and treat anything below **2 Hz** as **OFF**. Matrix32 SF=1 is not used due to the previously documented START anomaly. These are device bounds, not a general physiological-safety guarantee.
- `--arm` explicitly permits output. To START after a positive request, set CH1 SF to the selected fresh rate, require all seven configuration ACKs, recheck freshness, then call `device.start()`. Subsequent changed positive frequencies use `device.apply_live_frequency()` sequentially with the library's qualified ACK/guard transport. Each BLE update is completed before reading the latest next value.
- Zero requested rate issues Matrix32 STOP. A separate 20-ms stale-data supervisor attempts STOP if output is running and MCU input has aged more than 250 ms (configurable via `--stale-ms`); it then aborts the run rather than automatically restarting. Any sensor/connection failure or Ctrl+C triggers a best-effort STOP and BLE disconnect in cleanup. **If BLE is lost, physical output state is unknown**; isolate output and verify physically.
- This is a **bench-test increment**, not medically or clinically qualified stimulation. Confirm frequency, latency, output-off behavior and rapid-change behavior on the oscilloscope with a suitable dummy load before any other use.
- Offline tests: `python -m unittest discover -s tests -p 'test_tactile_stimulation.py' -v`. The integration has **not been physically tested** and the tests have **not been executed** here.

## Next increments

- Measure whether 119 bytes/sample and diagnostic traffic introduce USB queueing at observed ~70 Hz.
- Confirm sample-age and loss behavior from timestamps/PC arrival time.
- Python output worker: latest-only updates, selectable taxel and encoding, refractory and Matrix32 limit enforcement, stale-input stop behavior.
