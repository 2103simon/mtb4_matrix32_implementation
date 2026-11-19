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

## Validation

1. Switch to this branch: `git fetch origin && git switch feature/tactile-rate-streaming-v2 && git pull`.
2. Run Python/native tests from project root: `python -m unittest discover -s tests -v`. New `tests/test_mcu_rate_encoding.py` compiles the real `encoding.cpp` with g++, asserting startup RA=0, threshold accumulation, SA scaling, zero dt, runtime SA slope, and no refractory cap. An unavailable g++ skips the native test.
3. Build firmware: `cd adafruit_feather_m4 && pio run`, then flash with `pio run -t upload` after checking the selected port and physical setup.
4. From repository root, start `python read_mcu.py --print-every 1`. Verify successful CRC frame parsing, taxels 0..255, SA relation to configured slope, RA transients on pressure changes, and sample timestamps.
5. Review saved `data/*_tactile_rates.csv`; check inter-sample intervals and actual throughput. No physical Matrix32 output in this stage.

**Unverified:** GitHub source edits were possible, but this environment could not access github.com from the code container (DNS resolution failure). Compilation, tests and hardware behavior have therefore not been claimed as passing.

## Next increments

- Measure whether 119 bytes/sample and diagnostic traffic introduce USB queueing at observed ~70 Hz.
- Confirm sample-age and loss behavior from timestamps/PC arrival time.
- Python output worker: latest-only updates, selectable taxel and encoding, refractory and Matrix32 limit enforcement, stale-input stop behavior.
