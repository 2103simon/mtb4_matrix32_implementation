# Installation, hardware bring-up and safe operating procedure

This guide accompanies the [repository README](../README.md). It describes host installation, flashing both PlatformIO boards, role-based device discovery, BLE checks, and the recommended order of test operations. These steps were verified against source, not executed on your devices by the documentation change.

## 1. Host prerequisites

The host code assumes Linux with a working BlueZ/Bluetooth adapter, USB CDC access to Feather M4 and Nano Every, and a Tektronix MDO4104B-6 reachable over USBTMC. Install on a Python 3 system with `venv`, `pip`, and the build tools needed by PlatformIO. For example, on Debian/Ubuntu:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip bluetooth bluez git
```

The exact OS package names for USBTMC kernel support, udev rules and PlatformIO depend on distribution. This repository does **not** supply an automated permissions installer. Inspect `ls -l /dev/usbtmc*`, `ls -l /dev/serial/by-id/`, `groups` and applicable udev permissions before changing them; do not use `chmod 777` as a workaround.

```bash
git clone https://github.com/2103simon/mtb4_matrix32_implementation.git
cd mtb4_matrix32_implementation
git switch master

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The scientific analysis imports NumPy, which is explicitly declared in `requirements.txt`. `requirements.txt` pins `bleak==0.22.3` (the historically validated combination with Linux BlueZ 5.53); updates require explicit regression testing. Python source also uses `asyncio.TaskGroup`, so use **Python 3.11 or later**.

PlatformIO can be installed using the official documented method or into a separate tooling environment. Check `pio --version` / `platformio --version` before flashing. The Python project venv does not itself flash the boards.

## 2. Feather M4 CAN Express firmware

Project: [`adafruit_feather_m4/`](../adafruit_feather_m4/README.md), environment `adafruit_feather_m4_can`, platform `atmelsam`, framework `arduino`.

```bash
cd adafruit_feather_m4
pio run -e adafruit_feather_m4_can
# Confirm board identity/USB upload device, then:
pio run -e adafruit_feather_m4_can -t upload
cd ..
```

The firmware's dependencies are resolved through `platformio.ini`: Adafruit NeoPixel, Adafruit CAN, Adafruit TinyUSB and FastCRC. Only upload firmware compatible with the current host CRC/telemetry parser and ACK handshake. Do not keep a serial monitor open while a Python acquisition is using the Feather.

The Feather takes MTB4 taxel data over CAN, pairs split CAN messages, produces 12-taxel input samples, and reports calculated RA-I / SA-II requested rates in CRC-protected type `0x05` packets. It accepts the host's encoding parameters through CRC-protected command type `0x10` and acknowledges them with type `0x11`. For the exact layout, see [firmware README](../adafruit_feather_m4/README.md) and [tactile rate protocol](tactile_rate_mcu_v2.md).

## 3. Nano Every Gray reference firmware

Project: [`arduino_nano/`](../arduino_nano/README.md), environment `nano_every`. The code targets **ATmega4809 Nano Every**, not the classic Nano.

```bash
cd arduino_nano
pio run -e nano_every
# First identify the correct Nano USB upload port:
pio device list
pio run -e nano_every -t upload --upload-port /dev/ttyACM1
cd ..
```

The `/dev/ttyACM1` value is an **example**, not a stable device assignment. Use the actual Nano path from the device list. No D7 timecode is emitted until the host sends `START`. At 115200 baud the Nano prints full Gray counter + `EDGE_US` and supports active binary CRC32 four-timestamp clock synchronization. It must not be electrically tied to an unapproved stimulation return or a person.

## 4. Device discovery and scope connection

```bash
python -m communication.serial_ports
python -m serial.tools.list_ports -v
ls -l /dev/serial/by-id/
ls -l /dev/usbtmc*
bluetoothctl show
```

The serial resolver performs **passive USB identity matching** for `feather` and `nano`; it fails closed on missing or ambiguous descriptors. An explicit stable `/dev/serial/by-id/...` override is preferable to hard-coded `/dev/ttyACM0`.

The scope scripts auto-resolve an unambiguous supported Tektronix USBTMC node and query the actual instrument identity with SCPI. Scope discovery is independent of serial board discovery.

For a connection-only Matrix32 BLE check:

```bash
python connect_matrix32.py
```

This **does not configure or stimulate**; stop it before launching an integrated experiment, to avoid two clients fighting for the BLE connection. If the Bluetooth service is absent, check `systemctl status bluetooth`, adapter power from `bluetoothctl show`, and Bleak version. This repository's validated Bleak is `0.22.3`.

## 5. Scope and dummy-load wiring rules

**Authoritative bench description for this repository:** [CH1 Matrix32 dummy-load and electrical-reference setup](electrical_bench_setup.md). The operator confirmed the same physical three-branch rig: **CH1–100.0 kΩ**, **CH2–99.4 kΩ**, **CH3–99.5 kΩ** from their respective cathodes to the shared anode, with the **992 kΩ spare disconnected** and **TPP1000 10×** probing. The referenced passive-probe grounding arrangement ties the Matrix32 shared anode to the scope's protective-earth reference; it is **not intrinsically isolated**, and its compatibility with this exact setup remains to be checked by the operator. No Matrix32 output channel other than CH1 is enabled by the integrated runner.



- Scope **CH1**: Matrix32 **physical** CH1 output across the documented **100.0 kΩ CH1 resistor dummy load**, after verification of actual ratings and wiring. Ground clips from normal bench scopes are not arbitrary floating differential probes. Before connecting, determine whether the output is floating/high voltage and use an appropriately rated approved differential/isolated probing method.
- Scope **CH2**: Nano D7 timecode line, with proper Nano ground reference in a correctly isolated scope measurement setup. **Never attach Nano ground to any stimulation electrode/return simply because a scope channel needs a reference**.
- Default CH2 scale: **2 V/div**, rising trigger at **+1 V**; default CH1: **10 V/div**, but change the CH1 scale/probe configuration to match the real approved setup.
- Confirm the scope reads 2 s/div, 1,000,000 points, SAMPLE mode, SEQUENCE stop, 0% horizontal position, and delay OFF when running the integrated procedure.
- Have a **physical output disconnect/interlock**; Python's exception handlers and BLE STOP are best-effort rather than electrical evidence.

## 6. Safest progression from no hardware to output

First, device-free host tests and dry runs:

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
python oscilloscope/record_integrated_matrix32.py
```

Then Feather telemetry and standalone Matrix32 connectivity:

```bash
python read_mcu.py --print-every 10
# Stop read_mcu.py (Ctrl+C) before integrated recording.
python connect_matrix32.py
# Stop this too (Ctrl+C).
```

Then scope/Nano-only setup (read plan first):

```bash
python oscilloscope/test_gray_epoch_pause.py
# --run actually opens the Nano/scope; only use after wiring review.
```

Then **integrated non-stimulation baseline**:

```bash
timeout --signal=INT --kill-after=20s 180s \
  python -u oscilloscope/record_integrated_matrix32.py --run
```

Only once the rated CH1 resistor load, compatible electrical reference and probing arrangement, and physical disconnect have been independently confirmed may an authorized operator explicitly enable bench output:

```bash
timeout --signal=INT --kill-after=20s 180s \
  python -u oscilloscope/record_integrated_matrix32.py \
  --run --arm --dummy-load-confirmed
```

A live trial starts the MCU/Matrix32 controller **before arming** the finite scope capture; the script waits for the MCU configuration ACKs and Matrix32 START indication, then performs 30 pre-SYNC exchanges, arms the scope, records a nominal 20 s waveform, performs 30 post-SYNC exchanges, requests output STOP, and transfers CH1+CH2. An active positive tactile rate is needed for Matrix32 START; a non-positive source does not authorize startup.

The shell `timeout` is an **OS watchdog** against blocked USBTMC operations, not a guaranteed stimulator stop. If anything is electrically wrong, use the physical disconnect and verify the load output with suitable instrumentation.

## 7. Data inspection and analysis

```bash
# Latest standalone MCU recording:
python analyze_mcu_recording.py

# Latest completed integrated session:
python -u oscilloscope/analyze_integrated_matrix32.py

# Select a specific integrated recording:
python -u oscilloscope/analyze_integrated_matrix32.py \
  --run-dir data/recordings/<INTEGRATED_RUN>
```

Raw CSV, Nano USB JSONL and BLE JSONL files are preserved under each run. Derived plots and Markdown/JSON reports live in `<run>/analysis/`. The session report links to **independent per-scope-record** reports, not one artificial continuous waveform.

## 8. Failure cases

| Symptom | Check |
| --- | --- |
| `/dev/ttyACM*` switched | Use role discovery; do not hard-code port numbers |
| Nano firmware does not print frames | It needs the host's `START`; verify correct Nano Every firmware, USB port and 115200 baud |
| MCU file absent | Initial telemetry and both config ACKs must succeed before recorder starts |
| BLE connection unavailable | Stop competing BLE clients, verify BlueZ, adapter power and Bleak version |
| Integrated live capture does not start | Positive fresh tactile input, verified Matrix32 START, 45 s controller readiness timeout |
| Scope runs but CH1 flat | Examine load/probe, requested rate, START/STOP and SC mute state; do not interpret absent waveform as electrical certainty |
| Many “not comparable” frequency windows | Frequency changes can be faster than one-second settling windows; inspect per-pulse dynamic tracking |
| Clock report “unqualified” | Review per-record Gray RMS, Nano SYNC fit and USB offset uncertainty; do not silently change threshold |
| USBTMC operation hangs | External watchdog, permissions, supported instrument identity and SCPI diagnostics |

For code ownership and maintenance see [pipeline/components](pipeline_and_components.md), [time synchronization](clock_mapping_and_time_domains.md), and [integrated acquisition](integrated_matrix32_scope_recording.md).
