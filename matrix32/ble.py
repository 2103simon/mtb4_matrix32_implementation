import asyncio
import subprocess

from bleak import BleakScanner


def check_bluetooth_service():
    """Return True when the Linux BlueZ Bluetooth service is available."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "bluetooth"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            print("Bluetooth service is not running. Try:")
            print("  sudo systemctl start bluetooth")
            print("  sudo systemctl enable bluetooth")
            return False

        result = subprocess.run(
            [
                "dbus-send",
                "--system",
                "--print-reply",
                "--dest=org.bluez",
                "/",
                "org.freedesktop.DBus.Introspectable.Introspect",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            print("BlueZ D-Bus service is not accessible. Try:")
            print("  sudo systemctl restart dbus")
            print("  sudo systemctl restart bluetooth")
            return False
        return True
    except subprocess.TimeoutExpired:
        print("Timeout while checking the Bluetooth service.")
    except FileNotFoundError:
        print("systemctl or dbus-send was not found. This helper currently expects Linux/BlueZ.")
    except Exception as exc:
        print(f"Error checking Bluetooth service: {exc}")
    return False


async def scan_ble_device(target_device, attempts=3, delay=2, timeout=10.0):
    """Find a BLE device by advertised name and stop scanning immediately."""
    if not check_bluetooth_service():
        print("Bluetooth service issues detected. Fix them before continuing.")
        return None

    for attempt in range(1, attempts + 1):
        print(
            f"[Scan attempt {attempt}/{attempts}] Waiting up to "
            f"{timeout:.1f}s for {target_device}..."
        )
        try:
            device = await BleakScanner.find_device_by_filter(
                lambda candidate, _adv: bool(
                    candidate.name and target_device in candidate.name
                ),
                timeout=timeout,
            )
            if device is not None:
                print(f"Found target device: {device.name} - {device.address}")
                return device
        except Exception as exc:
            print(f"Error during BLE scan attempt {attempt}: {exc}")
            message = str(exc)
            if "org.bluez" in message:
                print("Try: sudo systemctl restart bluetooth")
                break
            if "Permission denied" in message:
                print("Bluetooth permission denied. Check adapter/user permissions.")
                break
            if "No such device" in message:
                print("Bluetooth adapter not found.")
                break

        if attempt < attempts:
            await asyncio.sleep(delay)

    print(f"Failed to find {target_device} after {attempts} attempts.")
    return None


BATTERY_REPORT_COMMAND = b">RB<"
BATTERY_RESPONSE_PREFIXES = (b">BAT;", b">BAT:")
BATTERY_RESPONSE_SUFFIX = b"<"
BATTERY_REPORT_TIMEOUT_SECONDS = 2.0


def parse_battery_report(data):
    """Validate a Matrix32 battery response and return battery percentage.

    The manual documents >BAT;x<; device S/N 0069 has been observed returning
    >BAT:x<. In both forms x is one raw byte in the range 0..100.
    """
    payload = bytes(data)
    prefix = next(
        (candidate for candidate in BATTERY_RESPONSE_PREFIXES if payload.startswith(candidate)),
        None,
    )
    if prefix is None:
        raise ValueError(f"Invalid battery response prefix: {payload!r}.")

    expected_length = len(prefix) + 1 + len(BATTERY_RESPONSE_SUFFIX)

    if len(payload) != expected_length:
        raise ValueError(
            f"Invalid battery response length {len(payload)}; "
            f"expected {expected_length} bytes."
        )
    if not payload.endswith(BATTERY_RESPONSE_SUFFIX):
        raise ValueError(
            f"Invalid battery response terminator: {payload!r}."
        )

    level = payload[len(prefix)]
    if not 0 <= level <= 100:
        raise ValueError(
            f"Battery level byte is outside documented range 0..100: {level}."
        )

    return level


async def query_battery_report(
    client,
    write_char_uuid,
    read_char_uuid,
    timeout=BATTERY_REPORT_TIMEOUT_SECONDS,
):
    """Send >RB< and await a valid Matrix32 battery indication.

    This is the non-stimulating two-way communication check used by the
    production readiness gate.
    """
    loop = asyncio.get_running_loop()
    response_future = loop.create_future()

    def on_indication(_sender, data):
        payload = bytes(data)
        if (
            any(payload.startswith(prefix) for prefix in BATTERY_RESPONSE_PREFIXES)
            and not response_future.done()
        ):
            response_future.set_result(payload)

    await client.start_notify(read_char_uuid, on_indication)
    try:
        await client.write_gatt_char(
            write_char_uuid,
            BATTERY_REPORT_COMMAND,
            response=False,
        )
        payload = await asyncio.wait_for(response_future, timeout=timeout)
        level = parse_battery_report(payload)
        return level, payload
    finally:
        if client.is_connected:
            try:
                await client.stop_notify(read_char_uuid)
            except Exception:
                # The health-check result is determined by the command/response
                # round trip. Teardown failures are handled by the caller's
                # connection cleanup path.
                pass


def handle_indication(sender, data):
    try:
        text = data.decode("utf-8").strip()
        print(f"Indication from {sender}: {text}")
    except UnicodeDecodeError:
        print(f"Indication from {sender}: (binary) {data.hex()}")


async def stim_stop(client, write_char_uuid):
    await client.write_gatt_char(write_char_uuid, b">R<")
    print("Stimulation is stopped.")
    await client.write_gatt_char(write_char_uuid, b">OFF<")


async def stim_start(client, write_char_uuid):
    await client.write_gatt_char(write_char_uuid, b">ON<")
    await client.write_gatt_char(write_char_uuid, b">S<")
    print("Stimulation is started.")
