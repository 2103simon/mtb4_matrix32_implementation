"""Connect to E-Matrix32, verify BLE readiness, and hold until Ctrl+C.

This is a connection-only diagnostic: it never configures, starts, or
commands stimulation. The existing Matrix32Device.connect() performs GATT
discovery, BLE stabilization, and a non-stimulating battery round trip.

Run from repository root:
    python connect_matrix32.py
"""

import asyncio

from matrix32 import Matrix32Device


HEARTBEAT_SECONDS = 5.0
DISCONNECT_TIMEOUT_SECONDS = 5.0


async def hold_connection(device, heartbeat_seconds=HEARTBEAT_SECONDS):
    """Wait with the BLE event loop active; exit if the link disappears."""
    print("Matrix32 connected and ready.")
    print(f"Battery: {device.last_battery_level}%")
    print("Stimulation: OFF (this script never enables output)")
    print("Press Ctrl+C to disconnect.\n")
    while True:
        if not device.connected:
            raise ConnectionError("Matrix32 BLE link lost.")
        print("[Matrix32] BLE connection active; stimulation OFF.")
        await asyncio.sleep(heartbeat_seconds)


async def run_connection(device=None, heartbeat_seconds=HEARTBEAT_SECONDS):
    """Connect only, hold, then disconnect on exit or failure."""
    device = Matrix32Device() if device is None else device
    try:
        print("Connecting to Matrix32 (no stimulation commands)...")
        await device.connect()
        if not device.connected or device.last_battery_level is None:
            raise RuntimeError("Matrix32 did not pass the BLE readiness check.")
        await hold_connection(device, heartbeat_seconds=heartbeat_seconds)
    finally:
        if device.client is not None:
            print("\nDisconnecting Matrix32...")
            try:
                await asyncio.wait_for(
                    device.disconnect(), timeout=DISCONNECT_TIMEOUT_SECONDS
                )
            except Exception as exc:
                print(
                    f"[WARNING] BLE disconnect could not be verified: "
                    f"{type(exc).__name__}: {exc}"
                )
            else:
                print("Matrix32 disconnected.")


def main():
    try:
        asyncio.run(run_connection())
    except KeyboardInterrupt:
        print("\nStopped by operator (Ctrl+C).")
    except (ConnectionError, RuntimeError) as exc:
        print(f"[ERROR] {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
