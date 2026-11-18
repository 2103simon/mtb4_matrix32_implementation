"""Non-stimulating Matrix32 BLE lifecycle regression test.

Use this only when validating the transport path or investigating a regression.
Normal hardware operation should use nerve_stimulation/e_matrix32.py.
"""

import argparse
import asyncio
import subprocess
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

# Allow this diagnostic to be run directly by path, e.g.
# python nerve_stimulation/test_connection.py, without requiring an editable install.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bleak import BleakClient, BleakScanner

from matrix32.ble import query_battery_report


TARGET_DEVICE = "E-Matrix32"
TARGET_ADDRESS = "00:3C:84:16:9B:26"
SERVICE_UUID_FRAGMENT = "9b22"
SCAN_TIMEOUT_SECONDS = 10.0
POST_SCAN_SETTLE_SECONDS = 0.0
CONNECT_ATTEMPTS = 20
CONNECT_RETRY_DELAY_SECONDS = 1.5
CONNECTION_STABILIZATION_SECONDS = 3.0
DISCONNECT_SETTLE_SECONDS = 2.0
DEFAULT_TEST_ROUNDS = 10
SCAN_BUSY_RETRIES = 3
SCAN_BUSY_RETRY_DELAY_SECONDS = 1.0


def format_payload(data):
    """Return lossless diagnostic views without interpreting protocol fields."""
    payload = bytes(data)
    views = [f"hex={payload.hex(' ')}"]

    try:
        ascii_text = payload.decode("ascii")
    except UnicodeDecodeError:
        ascii_text = None

    if ascii_text is not None and all(
        char.isprintable() or char in "\r\n\t" for char in ascii_text
    ):
        views.append(f"ascii={ascii_text!r} (lossless; protocol meaning not inferred)")

    return ", ".join(views)


def callback(sender, data):
    print(f"[notify] {sender}: {format_payload(data)}")


def disconnected_callback(_client):
    print("[ble] Device disconnected.")


def run_diagnostic_command(command):
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unavailable"

    output = (result.stdout or result.stderr).strip()
    return output or f"exit code {result.returncode}"


def print_environment_diagnostics():
    try:
        bleak_version = version("bleak")
    except PackageNotFoundError:
        bleak_version = "unknown"

    print("=== BLE environment ===")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Bleak: {bleak_version}")
    print(f"BlueZ: {run_diagnostic_command(['bluetoothctl', '--version'])}")
    print(
        "Bluetooth service: "
        f"{run_diagnostic_command(['systemctl', 'is-active', 'bluetooth'])}"
    )
    print(f"Wi-Fi radio: {run_diagnostic_command(['nmcli', 'radio', 'wifi'])}")
    show = run_diagnostic_command(["bluetoothctl", "show"])
    discovering = next(
        (
            line.strip()
            for line in show.splitlines()
            if line.strip().startswith("Discovering:")
        ),
        "Discovering: unknown",
    )
    print(f"BlueZ adapter: {discovering}")
    print("=======================")


def print_bluez_target_state(stage):
    info = run_diagnostic_command(["bluetoothctl", "info", TARGET_ADDRESS])
    interesting = []
    for line in info.splitlines():
        stripped = line.strip()
        if stripped.startswith(
            (
                "Name:",
                "Alias:",
                "Paired:",
                "Bonded:",
                "Trusted:",
                "Connected:",
                "ServicesResolved:",
                "RSSI:",
            )
        ):
            interesting.append(stripped)

    if interesting:
        print(f"[bluez:{stage}] " + " | ".join(interesting))
    else:
        print(f"[bluez:{stage}] {info}")


def is_bluez_in_progress_error(exc):
    message = str(exc)
    return (
        "org.bluez.Error.InProgress" in message
        or "Operation already in progress" in message
    )


def print_bluez_discovery_state(stage):
    show = run_diagnostic_command(["bluetoothctl", "show"])
    discovering = next(
        (
            line.strip()
            for line in show.splitlines()
            if line.strip().startswith("Discovering:")
        ),
        "Discovering: unknown",
    )
    print(f"[bluez:{stage}] {discovering}")


async def scan_for_target(attempt):
    prefix = f"[scan {attempt}/{CONNECT_ATTEMPTS}]"

    for busy_try in range(1, SCAN_BUSY_RETRIES + 1):
        print(
            f"{prefix} Waiting up to {SCAN_TIMEOUT_SECONDS:.1f}s for "
            f"{TARGET_DEVICE} ({TARGET_ADDRESS})..."
        )
        started = time.monotonic()

        try:
            # Stop scanning as soon as the known device advertises. This
            # shortens the scan-to-connect path instead of running a fixed
            # duration scan.
            device = await BleakScanner.find_device_by_address(
                TARGET_ADDRESS,
                timeout=SCAN_TIMEOUT_SECONDS,
            )

            if device is None:
                device = await BleakScanner.find_device_by_filter(
                    lambda candidate, _adv: bool(
                        candidate.name and TARGET_DEVICE in candidate.name
                    ),
                    timeout=SCAN_TIMEOUT_SECONDS,
                )
        except Exception as exc:
            if not is_bluez_in_progress_error(exc):
                raise

            print(
                f"{prefix} BlueZ discovery is already in progress "
                f"(busy retry {busy_try}/{SCAN_BUSY_RETRIES})."
            )
            print_bluez_discovery_state("scan-busy")

            if busy_try == SCAN_BUSY_RETRIES:
                raise RuntimeError(
                    "BlueZ discovery remained busy. Close any bluetoothctl "
                    "session with 'scan on', close Bluetooth settings/device "
                    "discovery, or run 'bluetoothctl scan off' before retrying."
                ) from exc

            await asyncio.sleep(SCAN_BUSY_RETRY_DELAY_SECONDS)
            continue

        elapsed = time.monotonic() - started
        if device is None:
            print(f"{prefix} Target was not found after {elapsed:.2f}s.")
            print_bluez_target_state("scan-miss")
            return None

        print(
            f"{prefix} Found target in {elapsed:.2f}s: "
            f"{device.name or TARGET_DEVICE} ({device.address})"
        )
        print_bluez_target_state("post-scan")
        return device

    return None


async def connect_with_retries():
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        device = await scan_for_target(attempt)

        if device is None:
            if attempt < CONNECT_ATTEMPTS:
                print(
                    f"[connect] Waiting {CONNECT_RETRY_DELAY_SECONDS:.1f}s "
                    "before a fresh scan..."
                )
                await asyncio.sleep(CONNECT_RETRY_DELAY_SECONDS)
            continue

        if POST_SCAN_SETTLE_SECONDS:
            print(
                f"[scan {attempt}/{CONNECT_ATTEMPTS}] Waiting "
                f"{POST_SCAN_SETTLE_SECONDS:.1f}s before connecting..."
            )
            await asyncio.sleep(POST_SCAN_SETTLE_SECONDS)

        print(
            f"[connect {attempt}/{CONNECT_ATTEMPTS}] "
            f"Connecting to {device.name or TARGET_DEVICE} ({device.address})..."
        )
        client = BleakClient(device, disconnected_callback=disconnected_callback)
        started = time.monotonic()
        connection_accepted = False

        try:
            await client.connect(dangerous_use_bleak_cache=True)
            elapsed = time.monotonic() - started
            if not client.is_connected:
                print(
                    f"[connect] connect() returned after {elapsed:.2f}s, "
                    "but client is not connected."
                )
                raise RuntimeError("connect() returned without an active link")

            print(
                f"[connect] GATT resolved in {elapsed:.2f}s; proving link "
                f"stability for {CONNECTION_STABILIZATION_SECONDS:.1f}s..."
            )
            await asyncio.sleep(CONNECTION_STABILIZATION_SECONDS)

            if not client.is_connected:
                raise RuntimeError(
                    "BLE link disconnected during stabilization window"
                )

            write_char_uuid = None
            read_char_uuid = None
            for service in client.services:
                if SERVICE_UUID_FRAGMENT not in service.uuid:
                    continue
                for char in service.characteristics:
                    if "write-without-response" in char.properties:
                        write_char_uuid = char.uuid
                    if "indicate" in char.properties:
                        read_char_uuid = char.uuid

            if write_char_uuid is None or read_char_uuid is None:
                raise RuntimeError(
                    "Matrix32 command/indication characteristics not found"
                )

            battery_level, battery_payload = await query_battery_report(
                client,
                write_char_uuid,
                read_char_uuid,
            )

            if not client.is_connected:
                raise RuntimeError(
                    "BLE link disconnected during battery health check"
                )

            total_elapsed = time.monotonic() - started
            print(
                f"[connect] Stable two-way BLE link proven in "
                f"{total_elapsed:.2f}s: battery={battery_level}% "
                f"({format_payload(battery_payload)})."
            )
            print_bluez_target_state("stable")
            connection_accepted = True
            return client
        except Exception as exc:
            elapsed = time.monotonic() - started
            print(
                f"[connect] Failed after {elapsed:.2f}s: "
                f"{type(exc).__name__}: {exc}"
            )
            print_bluez_target_state("connect-failed")
        finally:
            # Failed readiness checks must never leak a live connection into the
            # next scan/retry cycle. Successful links are returned still connected
            # so the lifecycle caller can perform and verify the clean disconnect.
            if not connection_accepted:
                try:
                    if client.is_connected:
                        await asyncio.wait_for(client.disconnect(), timeout=5.0)
                    else:
                        await client.disconnect()
                except Exception:
                    pass

        if attempt < CONNECT_ATTEMPTS:
            print(
                f"[connect] Waiting {CONNECT_RETRY_DELAY_SECONDS:.1f}s "
                "before a fresh scan and retry..."
            )
            await asyncio.sleep(CONNECT_RETRY_DELAY_SECONDS)

    return None



async def disconnect_cleanly(client):
    if client is None:
        return

    print(f"[ble] Disconnect requested (is_connected={client.is_connected}).")

    if client.is_connected:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=5.0)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("disconnect() timed out after 5.0s") from exc
        except Exception as exc:
            raise RuntimeError(
                f"disconnect() failed: {type(exc).__name__}: {exc}"
            ) from exc

    deadline = time.monotonic() + 5.0
    while client.is_connected and time.monotonic() < deadline:
        await asyncio.sleep(0.1)

    print_bluez_target_state("post-disconnect")

    if client.is_connected:
        raise RuntimeError(
            "BLE client still reports connected 5 seconds after disconnect()."
        )

    print("[ble] Clean disconnect confirmed.")
    print(
        f"[ble] Waiting {DISCONNECT_SETTLE_SECONDS:.1f}s before the next round..."
    )
    await asyncio.sleep(DISCONNECT_SETTLE_SECONDS)


async def main(rounds):
    print_environment_diagnostics()
    print(
        f"[test] Running {rounds} complete BLE lifecycle rounds. "
        "A round passes only after connect -> 3s stabilization -> "
        ">RB< -> valid >BAT;x< -> clean disconnect."
    )

    passed = 0
    failed = 0

    for round_number in range(1, rounds + 1):
        print(f"\n=== BLE lifecycle round {round_number}/{rounds} ===")
        client = None
        round_passed = False

        try:
            client = await connect_with_retries()
            if client is None:
                raise RuntimeError(
                    f"Could not establish a battery-validated connection "
                    f"after {CONNECT_ATTEMPTS} attempts."
                )

            # connect_with_retries() only returns after the 3 second
            # stabilization window and a valid >RB< -> >BAT;x< round trip.
            await disconnect_cleanly(client)
            client = None
            round_passed = True
            passed += 1
            print(f"[round {round_number}/{rounds}] PASS")
        except Exception as exc:
            failed += 1
            print(
                f"[round {round_number}/{rounds}] FAIL: "
                f"{type(exc).__name__}: {exc}"
            )

            if client is not None:
                try:
                    await disconnect_cleanly(client)
                except Exception as cleanup_exc:
                    print(
                        f"[round {round_number}/{rounds}] "
                        f"Cleanup also failed: {type(cleanup_exc).__name__}: "
                        f"{cleanup_exc}"
                    )

        if not round_passed and round_number < rounds:
            print(
                f"[round {round_number}/{rounds}] Starting a fresh lifecycle "
                "round after failure."
            )
            await asyncio.sleep(DISCONNECT_SETTLE_SECONDS)

    print()
    print("=== BLE lifecycle test summary ===")
    print(f"Rounds requested: {rounds}")
    print(f"PASS: {passed}")
    print(f"FAIL: {failed}")
    if rounds:
        print(f"Success rate: {100.0 * passed / rounds:.1f}%")

    if failed:
        raise SystemExit(1)


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Repeatedly prove the full Matrix32 BLE lifecycle: connect until "
            "stable, exchange the non-stimulating battery command/response, "
            "cleanly disconnect, then start a fresh round."
        )
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=DEFAULT_TEST_ROUNDS,
        help=f"Number of complete lifecycle rounds (default: {DEFAULT_TEST_ROUNDS}).",
    )
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds must be at least 1")
    return args


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(args.rounds))
