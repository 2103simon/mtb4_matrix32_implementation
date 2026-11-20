# Standard serial-device discovery (Nano Every and Feather M4)

## Policy for current and future Python scripts

**Identify USB serial hardware by device role, never by ttyACM number.**
Use `communication.serial_ports.discover_port(role)` where `role` is
`"nano"` or `"feather"`. This inspects `pyserial` port metadata and
**does not open, reset, or command** the device.

```python
from communication.serial_ports import discover_port, resolve_port

nano_port = discover_port("nano")
feather_port = discover_port("feather")

# For CLI/config overrides, use None when omitted:
nano_port = resolve_port(optional_port, "nano")
```

No `"auto"` sentinel is used. Omit a CLI serial-port argument by
default; if a configuration schema needs a port key, use JSON `null`.
An explicit device path may be supplied only for troubleshooting or
unusual USB descriptors.

### Applications already covered

- `oscilloscope/test_gray_timecode.py`: Nano Every found automatically;
  `--nano-port /dev/serial/by-id/...` remains an optional override.
- `matrix32/timing_marker.py`: `discover_timing_marker_port()` and
  `SerialTimingMarker()` use Nano Every identity discovery.
- `communication/serial_manager.py`: `start_comm()` defaults to
  Feather M4 discovery; its explicit port argument still works.
- `read_mcu.py`, `run_tactile_stimulation.py`: both use
  `SerialManager`; `config/config.json` has `serial.port: null`.
- `adafruit_feather_m4/test/smoke/read_from_fingertip.py`: uses Feather
  discovery by default when run directly.
- Other scope scripts communicate with a Tektronix **USBTMC**
  instrument, not a serial port; their independent scope-device
  discovery is unaffected.
- PlatformIO firmware uploading uses PlatformIO's own device selection;
  specifying `--upload-port` is an upload-time choice, not part of
  runtime Python auto-identification.

### Diagnostics

From repository root:

```bash
/home/icub/.virtualenvs/matrix32/bin/python -m communication.serial_ports
/home/icub/.virtualenvs/matrix32/bin/python -m serial.tools.list_ports -v
```

The resolver fails closed if no board can be identified, or if two
devices are equally plausible. It does not select the first matching
`/dev/ttyACM*`, does not probe other attached devices, and does not
send test commands merely for discovery. For ambiguity, use a
stable `/dev/serial/by-id/...` path if available, or update the USB
identity rules after verifying the new hardware.

The discovered path is logged on connection, but is not assumed to
remain stable after disconnect/reconnect. Re-run discovery on the next
connection.
