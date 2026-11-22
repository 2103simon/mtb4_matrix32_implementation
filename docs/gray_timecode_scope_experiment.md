# Nano Every Gray timecode: separate oscilloscope acquisitions and local clock fits

**Current scope-only procedure** for a Tektronix MDO4104B-6 and Arduino **Nano Every**. This page describes the **implemented workflow**; it does not attempt a new review of the earlier Gray benchmark findings. Detailed derivations and uncertainty definitions are in [clock mapping and time domains](clock_mapping_and_time_domains.md).

[Project README](../README.md) · [Nano firmware README](../arduino_nano/README.md) · [Scope scripts](../oscilloscope/README.md) · [Full clock-domain guide](clock_mapping_and_time_domains.md)

> **No Matrix32 output is used for this experiment.** Scope CH2 observes **Nano D7**, and an independent USB port carries Nano FRAME logs and clock SYNC responses. Never attach the Nano output or scope ground to an unapproved Matrix32 stimulation return. The physical wiring and ground-reference constraints for a separate integrated CH1 setup are documented in [electrical bench setup](electrical_bench_setup.md).

## Aim

The experiment provides a **physical timecode and independent software timestamp** from one Nano clock, then estimates how a **single scope record's** time axis relates to the Nano and host-PC monotonic clocks. Repeating finite scope acquisitions with pauses tests whether local clock fits can be formed separately; it does **not** provide gap-free acquisition or calibrated one-way USB timing.

## Hardware and firmware

- **Board:** Arduino Nano Every (ATmega4809), PlatformIO `[env:nano_every]`.
- **Output:** Nano D7 → oscilloscope **CH2** tip; confirm appropriate low-voltage reference/ground according to scope configuration.
- **USB:** Nano independent CDC serial port → Linux PC, 115200 baud; role-aware discovery in [`communication/serial_ports.py`](../communication/serial_ports.py).
- **Scope:** Tektronix MDO4104B-6 via USBTMC with CH2 **2 V/div**, rising trigger **+1 V**, 0% trigger position (left edge), delay OFF.
- **No stimulator:** Matrix32 and Feather are not connected or operated by the Gray-only acquisition script.

Build and flash, after inspecting the correct Nano upload port:

```bash
cd arduino_nano
pio run -e nano_every
pio run -e nano_every -t upload --upload-port /dev/ttyACM1
cd ..
```

The port above is illustrative and may change on reconnect. Close any serial monitor before launching Python so only one process owns the Nano USB stream. For complete firmware details see [`arduino_nano/README.md`](../arduino_nano/README.md).

## Framed Gray timecode on D7

Once the PC sends `START`, the Nano emits one 100 ms frame (**10 Hz**):

| Frame element | Position |
| --- | --- |
| Start marker, HIGH | 0–4 ms |
| Guard, LOW | 4–7 ms |
| Sixteen Gray bits (MSB first) | 7–87 ms; 5 ms per slot, 1 ms HIGH for bit 1 |
| Guard, LOW | 87–90 ms |
| End marker, HIGH | 90–97 ms |
| Idle, LOW | 97–100 ms |

The Gray value is `G = frame16 XOR (frame16 >> 1)`; the waveform represents the **low 16 bits** of a monotonic full frame counter. The Nano also emits one ASCII USB log entry per frame:

```text
FRAME <full_counter> GRAY <hex_gray_word> EDGE_US <nano_micros> OVERRUNS <count>
```

`EDGE_US` is sampled **just after** commanding the D7 START rising edge. It is distinct from the later host **USB message receipt** time, which can be delayed by buffering and OS scheduling. The `OVERRUNS` counter reports detected scheduling lateness.

The decoder [`decode_gray_timecode.py`](../oscilloscope/decode_gray_timecode.py) reads CH2 samples, identifies frame starts and bits, and recovers the encoded 16-bit counter. The alignment code [`align_gray_records.py`](../oscilloscope/align_gray_records.py) matches decoded frames to Nano USB logs and fits a local **scope → Nano** time mapping. Missing/ambiguous frames, wraps and monotonicity are diagnosed rather than treated as continuous data.

## USB clock synchronization

Each **binary CRC32 SYNC v1** exchange records four timestamps:

| Symbol | Clock | Measurement |
| --- | --- | --- |
| `t1` | PC `monotonic_ns` | Request send |
| `t2` | Nano `micros()` | Request reception |
| `t3` | Nano `micros()` | Response preparation |
| `t4` | PC `monotonic_ns` | Response reception |

The transport sends a 12-byte request and receives a 20-byte reply; `A5 5A` magic, type/version/sequence and CRC32 protect transport integrity. CRC32 does **not** measure or remove asymmetric USB delay. The source also offers a text `SYNC` transport for controlled diagnostic comparison, but **binary CRC32 is the default**.

Each finite acquisition gets its **own 30 pre- and 30 post-capture SYNC measurements**, giving a local affine Nano→PC slope/offset estimate. The two stages are composed:

```text
nano_time ≈ a * scope_time + b
PC_monotonic_time ≈ c * nano_time + d
```

This is a **provisional cross-device mapping**, not calibrated absolute latency. Scope-to-Nano edge RMS and Nano-to-PC SYNC fit RMS are **regression residuals**, not a worst-case physical timestamp error. Software USB timing asymmetry remains unknown.

## Standard multi-record experiment

Run from repository root:

```bash
git switch main
git pull origin main

# Device-free tests
python -m unittest discover -s tests -p 'test_gray*.py' -v
python -m unittest discover -s tests -p 'test_nano_binary_protocol.py' -v

# Print the exact acquisition plan; no hardware touched
python oscilloscope/test_gray_epoch_pause.py
```

The [`test_gray_epoch_pause.py`](../oscilloscope/test_gray_epoch_pause.py) wrapper configures this current standard:

| Parameter | Default |
| --- | --- |
| Scope record count | **3 separate records** |
| Per-record duration | **20 s**, not including USB download or pause |
| Record length | **1,000,000** points per scope acquisition |
| Scope horizontal | **2 s/div**, horizontal position **0%**, delay OFF |
| CH2 | Nano D7, **2 V/div**, +1 V rising trigger |
| Inter-record pause | **60 seconds** |
| Clock synchronization | **30 binary CRC32 exchanges before and after each record** |
| Nano generator | Remains running over the entire experiment |
| Matrix32 | **Never started** by this experiment |

Only after checking the wiring and scope configuration, start the hardware experiment:

```bash
timeout --signal=INT --kill-after=20s 300s \
  python -u oscilloscope/test_gray_epoch_pause.py --run
```

**Watchdog caution:** Three 20 s records plus two 60 s pauses already account for **180 s**, before Nano startup, 180 total SYNC transactions, and three potentially long waveform USB transfers. The 300 s external watchdog is an example, not an established upper bound on instrument transfer duration. A watchdog is not an electrical interlock.

For a single standalone Gray measurement, [`test_gray_timecode.py`](../oscilloscope/test_gray_timecode.py) is available with configurable count/timebase/record length; use its **dry-run** first and consult `--help` for arguments. For the standardized three-epoch comparison prefer the wrapper above rather than manually assembling inconsistent options.

## Output and analysis

Each experiment writes **one parent directory with independent record entries**:

```text
data/recordings/<UTC>_gray_epoch_pause_20s/
  metadata.json                 # exact scope settings, record indices and boundaries
  nano_serial.jsonl             # separate serial FRAME and binary SYNC observations
  waveforms/
    record_00001_ch2.csv        # finite scope acquisition 1
    record_00002_ch2.csv        # finite scope acquisition 2
    record_00003_ch2.csv        # finite scope acquisition 3
  gray_alignment.json           # matched CH2↔Nano frames for each record
  epoch_clock_analysis.json     # separate local Nano→PC fit and quality for each record
```

The wrapper attempts offline alignment after scope/Nano sessions have fully closed. It is also safe to reanalyze later, without devices:

```bash
python oscilloscope/analyze_gray_epoch_experiment.py \
  data/recordings/<GRAY_EPOCH_RUN>
```

The analysis reports **per-record** decoded/matched frame counts, edge-fit RMS, Nano→host drift estimates, fit diagnostics and qualification/warnings. Its JSON records preserve each acquisition's SYNC ID ranges and estimated host bounds. A record with Gray edge RMS above the **100 µs default criterion** is **unqualified** for that qualification gate but its physical CH2 waveform remains valid source data.

**Never concatenate CH2 samples from separate records or calculate one apparent sample cadence through the acquisition pauses.** The Nano clock keeps running through gaps; scope physical samples are absent. Every record requires its own fit; missing overlaps remain unobserved.

## Qualification boundaries and follow-up

What this experiment supports: confirming that a physical marker corresponds to a Nano frame ID, estimating **local** oscillator-rate relationships and fit residuals, documenting gaps, and measuring which separate records meet explicit fit-quality gates.

What it does **not** support: USB one-way delay calibration; proof of sub-millisecond absolute scope↔PC offset; recovery of samples missing between captures; or measurement of Matrix32 CH1 physical output (CH1 is not recorded here).

Future re-examination of historical Gray trials and any revised experimental conclusions should be performed separately on an experiment branch, not folded into this current operating guide. For the existing estimator assumptions and engineering detail see [clock mapping](clock_mapping_and_time_domains.md) and [scope integration](integrated_matrix32_scope_recording.md).
