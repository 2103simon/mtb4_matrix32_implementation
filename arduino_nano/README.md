# Arduino Nano Every — canonical 10 Hz hardware-timed Gray reference

**One build, one firmware.** Open the `arduino_nano/` folder as a PlatformIO project in VS Code (File → Open Folder) if the project/tasks are not visible in the sidebar.

`arduino_nano/platformio.ini` contains only
`[env:nano_every_10hz_hardware_timer]` and sets `default_envs = nano_every_10hz_hardware_timer`. VS Code's
PlatformIO **Upload** button builds/uploads this exact firmware once.
Close serial monitors and the experiment runner before flashing.

From the repository root:

```bash
cd arduino_nano
pio run -e nano_every_10hz_hardware_timer
pio run -e nano_every_10hz_hardware_timer -t upload
cd ..
```

If more than one PlatformIO project is open, use **Project Tasks →
arduino_nano → nano_every_10hz_hardware_timer → General → Upload**, not an ambiguous
workspace-wide upload action. Confirm a successful Build and Upload log.
Port discovery: `pio device list`; an explicit port may be supplied
with `--upload-port /dev/ttyACM1` when necessary.

## Hardware waveform and timestamp semantics

- Board: Arduino Nano Every, ATmega4809; scope probe D7 = PA1/TCA0-WO1
  on CH2. No stimulation electrodes or direct connections to Matrix32.
- Gray frame: **10 Hz (100 ms)**; START HIGH 0–4 ms; DATA 16 × 5 ms
  slots, 1 ms HIGH for each set bit, beginning at 7 ms; END HIGH
  90–97 ms. Full frame counter is carried in the post-acquisition log.
- TCA0 single-slope PWM hardware generates output transitions; its
  overflow ISR programs the **next** 1 ms cell using buffered compare.
  The ISR reads `micros()` near each physical START edge, but **does
  not hardware-latch the physical edge**. The Nano↔scope clock
  relationship remains provisional and can be nonlinear.
- Fixed SRAM buffer: 256 entries × 12 bytes, enough for **25.6 s
  at 10 Hz**. Each holds `FRAME` counter, `EDGE_US` and overruns.
  It never overwrites earlier entries; overflow is reported and
  disqualifies clock fitting for that record.

## Binary SYNC and buffered acquisition

The host uses CRC32-protected binary SYNC messages (12-byte request,
20-byte reply, A5 5A header), and verifies:

```text
FW GRAY_TC_V1 PIN 7 FRAME_US 100000 ... FRAME_MODE 10HZ_HW_TCA_BUFFERED
```

`PING`, `INFO`, `START`, `STOP` and text `SYNC` also exist for
diagnostics. During a scope acquisition, **do not continuously print
FRAME lines**:

1. Send 30 pre-acquisition binary SYNC requests.
2. Send `BUFFER_BEGIN`; verify its ACK/capacity.
3. Arm/acquire the oscilloscope's finite waveform.
4. Once the scope has completed acquisition, send `BUFFER_END`.
5. Send `BUFFER_DUMP` and verify exact frame count and `LOST=0`.
6. Send 30 post-acquisition binary SYNC requests, then transfer the
   frozen scope channels. The next record receives a fresh buffer.

The host implementation is
[`record_integrated_matrix32.py`](../oscilloscope/record_integrated_matrix32.py)
for integrated work and
[`test_gray_10hz_hardware_timer.py`](../oscilloscope/test_gray_10hz_hardware_timer.py)
for bench-only Gray checks. Both require the same exact firmware mode.

For the next planned **single 20-second 10M-sample-per-channel**
integrated session, follow
[the integrated bench procedure](../docs/integrated_matrix32_scope_recording.md).
The script is a dry run until `--run`; Matrix32 output additionally
requires the operator's two explicit arm/confirmed-dummy-load flags.
USB/ACK timestamps do not certify when physical stimulation changed.

**Historical firmware comparisons** remain in Git history and existing
offline recording datasets. They are not buildable alternatives from
this canonical PlatformIO project.
