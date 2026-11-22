# Adafruit Feather M4 CAN Express — MTB4 fingertip firmware

Embedded source for the **Adafruit Feather M4 CAN Express** reading a twelve-taxel MTB4 fingertip sensor through CAN and reporting **CRC32-framed USB CDC tactile telemetry and RA-I/SA-II rate snapshots** to the Linux host. This folder is an independent **PlatformIO project**.

[Project overview](../README.md) · [Install and hardware bring-up](../docs/installation_and_operations.md) · [Rate protocol](../docs/tactile_rate_mcu_v2.md) · [Architecture notes](ARCHITECTURE.md)

## Requirements and build

- Adafruit Feather M4 CAN Express, a compatible wired/powered MTB4 sensor/CAN interface, USB data cable and correctly provisioned CAN termination/power.
- [PlatformIO](https://platformio.org/), with environment `adafruit_feather_m4_can` from [`platformio.ini`](platformio.ini).
- PlatformIO resolves Adafruit CAN, NeoPixel, TinyUSB and FastCRC libraries from the project configuration.

From the repository root:

```bash
cd adafruit_feather_m4
pio run -e adafruit_feather_m4_can
# After selecting/confirming the correct board's upload device:
pio run -e adafruit_feather_m4_can -t upload
cd ..
```

To monitor a device manually:

```bash
cd adafruit_feather_m4
pio device monitor -b 115200
```

**Close the monitor before** `read_mcu.py` or the integrated recorder; one USB serial device should not be opened by two clients. The board identity/port can be inspected from `pio device list`, `python -m serial.tools.list_ports -v`, or `python -m communication.serial_ports` from the repository root.

## Firmware pipeline

```text
MTB4 fingertip CAN frames
   │
   ▼
CAN ISR → bounded 32-frame ring buffer
   │             no serial writes or encoding inside ISR
   ▼
foreground pairing of CAN 0x40/0xC0 taxel halves
   │
   ▼
12 taxels (8-bit values, expected order and inversion)
   │
   ├── RA-I encoder / event handling
   ├── SA-II encoder / event handling
   └── direct tactile-rate snapshot
               │
               ▼
      CRC32-framed USB serial → host SerialManager
```

The firmware uses callbacks to copy CAN data into the bounded queue; sample assembly, encoding, event sorting and USB writes occur in the foreground. A missing CAN half or sequence gap invalidates the partially assembled sample. The experimental telemetry cadence has been about **44.5 Hz**; do not confuse this with an internally requested Matrix32 SF or actual electrically measured pulse rate.

### Module ownership

| File | Purpose |
| --- | --- |
| [`src/main.cpp`](src/main.cpp) | Board/CAN setup, bounded CAN ISR queue, USB protocol, command ACKs, serial loop |
| [`src/taxel_assembler.cpp`](src/taxel_assembler.cpp), [`include/taxel_assembler.h`](include/taxel_assembler.h) | Pair sensor CAN halves into taxel sample |
| [`src/encoding.cpp`](src/encoding.cpp), [`include/encoding.h`](include/encoding.h) | Production RA-I/SA-II encoding implementation and tunable parameter ownership |
| [`src/event_transport.cpp`](src/event_transport.cpp) | Event batch handling |
| [`src/diagnostic_telemetry.cpp`](src/diagnostic_telemetry.cpp) | Diagnostic telemetry |
| [`include/firmware_mode.h`](include/firmware_mode.h) | Firmware communication mode selection |
| [`include/ra_threshold.h`](include/ra_threshold.h) | RA threshold parameter guard |
| [`test/`](test/) | Firmware-oriented test and smoke support |

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for extraction choices and ISR ownership boundaries.

## USB serial wire protocol

Serial speed: **115200 baud**. The current framed format is:

```text
length_u16_BE | frame_type_u8 | payload | crc32_u32_BE
```

The CRC is computed over `frame_type + payload`. See [`communication/serial_manager.py`](../communication/serial_manager.py) for the host parser.

| Frame | Meaning |
| --- | --- |
| `0x02` | Taxel telemetry: 32-bit timestamp + 12 pressure bytes |
| `0x04` | Event batch, reference timestamp + encoded event IDs and offsets |
| `0x05` | Tactile-rate snapshot: timestamp, twelve taxels, twelve RA rates, twelve SA rates; each rate is 32-bit BE centi-Hz |
| `0x10` | Host→MCU configuration command |
| `0x11` | MCU→host acknowledgment including applied value |
| `0x12` | Firmware diagnostic telemetry |

Relevant parameter command IDs include `0x01` (RA threshold), `0x02` (refractory period), `0x03` (SA maximum frequency) and `0x04` (configuration request). The host startup path **waits for the first valid telemetry** before requesting initial parameters and requires correct ACKs for RA threshold and SA max frequency before recording begins.

The MCU delivers requested firing-rate data; it **does not** command the E-Matrix32 over BLE itself. The PC's [`run_tactile_stimulation.py`](../run_tactile_stimulation.py) is the only standard selected-taxel → Matrix32 live control entry point.

## Host smoke tests

From repository root after flashing:

```bash
python -m communication.serial_ports
python read_mcu.py --print-every 10
```

This records MCU rates without Matrix32 stimulation. Current host configuration settings are in [`config/config.json`](../config/config.json). The recording starts after both encoding ACKs; output files are under the configured `recordings_dir`.

The host-only regression tests include parser, rate encoding, configuration ACK and tactile snapshot tests:

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

A firmware build, MCU output packet decode, and a live CAN sensor check are all separate conditions. No synthetic unit test establishes real-time performance on physical hardware.

## Limitations, safety and development notes

- This firmware **does not directly stimulate** and must not be connected to stimulation electrodes or Matrix32 returns as a shortcut to time synchronization.
- The preserved MCU timing and RA/SA algorithm were designed for this experimental pipeline; generated firing-rate values are **not** an electrically measured Matrix32 pulse rate.
- Keep CAN ISR operations minimal and preserve complete CAN frame queue and assembler behavior when adding diagnostics.
- Wire-protocol or command changes must be applied simultaneously to firmware, host parser and tests; never introduce unframed debug writes into a live framed stream without verifying parser behavior.
- The older [`README`](README) file contains historical Falange/FalangeGUI-oriented material and legacy paths; this `README.md` is the current repository entry point.
