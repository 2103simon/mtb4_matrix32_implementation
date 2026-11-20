# Arduino Nano Every — Gray timecode reference

Self-contained PlatformIO project (same structure as `adafruit_feather_m4/`).
Target: **Arduino Nano Every** (ATmega4809), not the classic ATmega328P Nano.

## Build

From the repository root:

```bash
cd arduino_nano
pio run -e nano_every
```

## Upload

Close serial monitors or Python processes holding the Nano port. Discover it
using `pio device list` or `ls -l /dev/serial/by-id/`, then:

```bash
pio run -e nano_every -t upload --upload-port /dev/ttyACM1
```

Replace `/dev/ttyACM0` with the correct Nano device. On some machines
`platformio` is the executable rather than `pio`.

## Serial monitor (optional manual check)

```bash
pio device monitor -b 115200
```

Firmware **does not start D7 pulses automatically**. Commands are
`PING <seq>`, `INFO <seq>`, `START <seq>`, `STOP <seq>`, and `SYNC <seq>`.
Each command returns a matching `ACK`. `SYNC` returns
`ACK SYNC <seq> RX_US <micros_at_command_newline> TX_US <micros_before_reply>`.
The PC brackets the exchange with `time.monotonic_ns()` to estimate
clock offset and drift. USB delay asymmetry remains an uncertainty.

The **binary CRC32 SYNC v1** request/ACK transport is now the default
for Nano synchronization after the 2026-11-20 A/B latency comparison. It uses 12-byte requests and 20-byte
responses, with magic bytes `A5 5A`, version, message type, sequence
number, Nano timestamps in the response, and IEEE CRC32 over the
message body (little-endian fields). The ASCII Gray `FRAME` stream and
legacy commands remain unchanged. See the protocol specification and
A/B benchmark in `docs/gray_timecode_scope_experiment.md`.
Text SYNC is retained for legacy compatibility and controlled comparison.
CRC32 protects frame integrity but cannot reveal asymmetric USB delays.
At START the firmware
generates a framed 16-bit Gray counter at 10 Hz on D7 while reporting
the full counter over USB serial. Never run the serial monitor and
Python experiment against the same port simultaneously.

## Bench wiring and experiments

See [Gray timecode scope experiment](../docs/gray_timecode_scope_experiment.md)
for the CH2 Nano D7 marker wiring, single and repeated
oscilloscope capture commands, decoder, and timing limitations.
This reference firmware must **not** be connected directly to Matrix32
stimulation outputs or electrode grounds.

## Layout

- `platformio.ini`: `atmelmegaavr` / `nano_every` / Arduino environment.
- `src/main.cpp`: complete Gray timecode firmware; previous `.ino`
  sketch moved here without changing its behavior.
- `.gitignore`: ignores local PlatformIO build data.

No external libraries are required. This PlatformIO migration alone
does not establish hardware timing accuracy.
