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
pio run -e nano_every -t upload --upload-port /dev/ttyACM0
```

Replace `/dev/ttyACM0` with the correct Nano device. On some machines
`platformio` is the executable rather than `pio`.

## Serial monitor (optional manual check)

```bash
pio device monitor -b 115200
```

Firmware **does not start D7 pulses automatically**. Commands are
`PING <seq>`, `INFO <seq>`, `START <seq>`, `STOP <seq>`, and `SYNC <seq>`.
Each command returns a matching `ACK`. `SYNC` returns\n`ACK SYNC <seq> RX_US <micros_at_command_newline> TX_US <micros_before_reply>`.\nThe PC brackets the exchange with `time.monotonic_ns()` to estimate\nclock offset and drift. USB delay asymmetry remains an uncertainty.\nAt START the firmware
generates a framed 16-bit Gray counter at 10 Hz on D7 while reporting
the full counter over USB serial. Never run the serial monitor and
Python experiment against the same port simultaneously.

## Bench wiring and experiments

See [Gray timecode scope experiment](../docs/gray_timecode_scope_experiment.md)
for the CH4 marker wiring, CH2 PROBE COMP trigger, single and repeated
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
