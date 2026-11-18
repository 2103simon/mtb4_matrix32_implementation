"""Low-level Matrix32 BLE diagnostic/regression utility.

The normal hardware-control path does not need this script. Keep it for future
BLE regressions or for analyzing btmon captures with --analyze-btsnoop.
"""

import argparse
import asyncio
import csv
import struct
import sys
import time
from pathlib import Path

# Allow this diagnostic to be run directly by path, e.g.
# python nerve_stimulation/ble_link_stress.py, without requiring an editable install.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bleak import BleakClient, BleakScanner

from matrix32.ble import query_battery_report


TARGET_ADDRESS = "00:3C:84:16:9B:26"
TARGET_NAME = "E-Matrix32"
SCAN_TIMEOUT_SECONDS = 10.0
POST_DISCONNECT_DELAY_SECONDS = 2.0
CONNECTION_STABILIZATION_SECONDS = 3.0
SERVICE_UUID_FRAGMENT = "9b22"

REASON_CONNECTION_TIMEOUT = 0x08
REASON_LOCAL_HOST = 0x16


def _u16_le(data, offset):
    return data[offset] | (data[offset + 1] << 8)


def analyze_btsnoop(path):
    """Summarize low-level HCI connection attempts in a btmon btsnoop capture."""
    raw = Path(path).read_bytes()
    if len(raw) < 16 or raw[:8] != b"btsnoop\x00":
        raise ValueError(f"{path} is not a btsnoop file.")

    attempts = []
    offset = 16
    first_timestamp = None

    while offset + 24 <= len(raw):
        (
            original_length,
            included_length,
            flags,
            drops,
            timestamp,
        ) = struct.unpack_from(">IIIIQ", raw, offset)
        offset += 24

        packet = raw[offset : offset + included_length]
        offset += included_length

        if first_timestamp is None:
            first_timestamp = timestamp
        relative_seconds = (timestamp - first_timestamp) / 1_000_000.0

        # LE Enhanced Connection Complete event.
        if len(packet) >= 32 and packet[0] == 0x3E and packet[2] == 0x0A:
            attempts.append(
                {
                    "number": len(attempts) + 1,
                    "start": relative_seconds,
                    "handle": _u16_le(packet, 4),
                    "initial_interval_ms": _u16_le(packet, 26) * 1.25,
                    "initial_timeout_ms": _u16_le(packet, 30) * 10,
                    "update": None,
                    "update_complete": None,
                    "disconnect": None,
                }
            )
            continue

        if not attempts:
            continue

        # LE Connection Update command (opcode 0x2013).
        if len(packet) >= 13 and packet[0] == 0x13 and packet[1] == 0x20:
            handle = _u16_le(packet, 3)
            attempt = next(
                (
                    item
                    for item in reversed(attempts)
                    if item["handle"] == handle and item["disconnect"] is None
                ),
                None,
            )
            if attempt is not None:
                attempt["update"] = {
                    "time": relative_seconds,
                    "min_interval_ms": _u16_le(packet, 5) * 1.25,
                    "max_interval_ms": _u16_le(packet, 7) * 1.25,
                    "timeout_ms": _u16_le(packet, 11) * 10,
                }
            continue

        # LE Connection Update Complete event.
        if len(packet) >= 12 and packet[0] == 0x3E and packet[2] == 0x03:
            handle = _u16_le(packet, 4)
            attempt = next(
                (
                    item
                    for item in reversed(attempts)
                    if item["handle"] == handle and item["disconnect"] is None
                ),
                None,
            )
            if attempt is not None:
                attempt["update_complete"] = {
                    "time": relative_seconds,
                    "status": packet[3],
                    "interval_ms": _u16_le(packet, 6) * 1.25,
                    "timeout_ms": _u16_le(packet, 10) * 10,
                }
            continue

        # Disconnect Complete event.
        if len(packet) >= 6 and packet[0] == 0x05:
            handle = _u16_le(packet, 3)
            attempt = next(
                (
                    item
                    for item in reversed(attempts)
                    if item["handle"] == handle and item["disconnect"] is None
                ),
                None,
            )
            if attempt is not None:
                attempt["disconnect"] = {
                    "time": relative_seconds,
                    "status": packet[2],
                    "reason": packet[5],
                }

    if not attempts:
        print(f"No LE Enhanced Connection Complete events found in {path}.")
        return

    stabilized = sum(
        1
        for item in attempts
        if item["update_complete"] is not None
        and item["update_complete"]["status"] == 0
    )
    timeouts = sum(
        1
        for item in attempts
        if item["disconnect"] is not None
        and item["disconnect"]["reason"] == REASON_CONNECTION_TIMEOUT
    )
    local_disconnects = sum(
        1
        for item in attempts
        if item["disconnect"] is not None
        and item["disconnect"]["reason"] == REASON_LOCAL_HOST
    )

    print(f"=== Controller-level BLE analysis: {path} ===")
    print(
        f"Physical HCI links: {len(attempts)} | "
        f"connection-update completed: {stabilized} | "
        f"timeout 0x08: {timeouts} | "
        f"local disconnect 0x16: {local_disconnects}"
    )
    print()
    print(
        " #   initial params       requested update        result"
    )

    for item in attempts:
        initial = (
            f"{item['initial_interval_ms']:.1f}ms/"
            f"{item['initial_timeout_ms']:.0f}ms"
        )

        if item["update"] is None:
            requested = "-"
        else:
            requested = (
                f"{item['update']['min_interval_ms']:.1f}-"
                f"{item['update']['max_interval_ms']:.1f}ms/"
                f"{item['update']['timeout_ms']:.0f}ms"
            )

        if (
            item["update_complete"] is not None
            and item["update_complete"]["status"] == 0
        ):
            delta = item["update_complete"]["time"] - item["start"]
            result = f"STABILIZED update-complete at +{delta:.3f}s"
        elif item["disconnect"] is not None:
            delta = item["disconnect"]["time"] - item["start"]
            reason = item["disconnect"]["reason"]
            result = f"DISCONNECT reason=0x{reason:02x} at +{delta:.3f}s"
        else:
            result = "INCOMPLETE"

        print(
            f"{item['number']:2d}   "
            f"{initial:18s} "
            f"{requested:23s} "
            f"{result}"
        )

    print()
    if timeouts:
        print(
            "Interpretation: physical links that do not complete the Matrix32 "
            "connection-parameter update usually time out with HCI reason 0x08. "
            "When analyzing test_connection.py, these physical failures correspond "
            "to explicit application retry attempts; do not infer hidden BlueZ "
            "retries from this summary."
        )


async def run_cycle(cycle_number):
    started = time.monotonic()
    device = None
    client = None
    outcome = "unknown"
    detail = ""

    try:
        device = await BleakScanner.find_device_by_address(
            TARGET_ADDRESS,
            timeout=SCAN_TIMEOUT_SECONDS,
        )
        if device is None:
            outcome = "scan-miss"
            detail = "target not found"
            return outcome, detail, time.monotonic() - started

        client = BleakClient(device)
        connect_started = time.monotonic()
        await client.connect(dangerous_use_bleak_cache=True)
        connect_elapsed = time.monotonic() - connect_started

        if not client.is_connected:
            outcome = "connect-failed"
            detail = "connect returned but is_connected is false"
            return outcome, detail, time.monotonic() - started

        await asyncio.sleep(CONNECTION_STABILIZATION_SECONDS)
        if not client.is_connected:
            outcome = "unstable-link"
            detail = (
                f"disconnected within {CONNECTION_STABILIZATION_SECONDS:.1f}s "
                "stabilization window"
            )
            return outcome, detail, time.monotonic() - started

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
            outcome = "healthcheck-failed"
            detail = "Matrix32 command/indication characteristics not found"
            return outcome, detail, time.monotonic() - started

        battery_level, battery_payload = await query_battery_report(
            client,
            write_char_uuid,
            read_char_uuid,
        )
        if not client.is_connected:
            outcome = "healthcheck-failed"
            detail = "disconnected during battery command/response health check"
            return outcome, detail, time.monotonic() - started

        outcome = "stable-success"
        detail = (
            f"connect() {connect_elapsed:.3f}s; survived "
            f"{CONNECTION_STABILIZATION_SECONDS:.1f}s; "
            f">RB< -> battery report succeeded ({battery_level}%, "
            f"{battery_payload.hex(' ')})"
        )
        return outcome, detail, time.monotonic() - started

    except Exception as exc:
        outcome = "connect-failed"
        detail = f"{type(exc).__name__}: {exc}"
        return outcome, detail, time.monotonic() - started

    finally:
        if client is not None:
            try:
                if client.is_connected:
                    await client.disconnect()
            except Exception:
                pass
        await asyncio.sleep(POST_DISCONNECT_DELAY_SECONDS)


async def run_stress_test(args):
    rows = []
    successes = 0

    print(
        "NOTE: these are APPLICATION-LEVEL Bleak/BlueZ cycles. Correlate them "
        "with --analyze-btsnoop on the matching btmon capture to measure whether "
        "each physical link completed the Matrix32 connection-parameter update."
    )
    print()

    for cycle in range(1, args.cycles + 1):
        outcome, detail, elapsed = await run_cycle(cycle)
        if outcome == "stable-success":
            successes += 1

        print(
            f"[{cycle:02d}/{args.cycles:02d}] "
            f"{outcome:14s} {elapsed:6.2f}s  {detail}"
        )
        rows.append(
            {
                "cycle": cycle,
                "application_outcome": outcome,
                "elapsed_seconds": f"{elapsed:.3f}",
                "detail": detail,
            }
        )

    failures = args.cycles - successes
    success_rate = 100.0 * successes / args.cycles if args.cycles else 0.0

    print()
    print(
        f"Stable application-level result: {successes}/{args.cycles} successful "
        f"({success_rate:.1f}%), {failures} failed."
    )
    print(
        "A success here means GATT resolved, the link survived the stabilization "
        "window, and the non-stimulating >RB< -> >BAT:x< (or documented >BAT;x<) "
        "battery round trip succeeded. "
        "Analyze the btmon capture "
        "separately for controller-level timing and disconnect reasons."
    )

    if args.csv is not None:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "cycle",
                    "application_outcome",
                    "elapsed_seconds",
                    "detail",
                ],
            )
            writer.writeheader()
            writer.writerows(rows)
        print(f"Saved results to {args.csv}")


async def main():
    parser = argparse.ArgumentParser(
        description=(
            "Run repeated Matrix32 BLE connect/disconnect cycles, or analyze "
            "the matching btmon btsnoop capture at controller level."
        )
    )
    parser.add_argument("--cycles", type=int, default=20)
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Optional CSV file for application-level per-cycle results.",
    )
    parser.add_argument(
        "--analyze-btsnoop",
        type=Path,
        default=None,
        help=(
            "Analyze an existing btmon -w .btsnoop capture instead of running "
            "new BLE cycles."
        ),
    )
    args = parser.parse_args()

    if args.analyze_btsnoop is not None:
        analyze_btsnoop(args.analyze_btsnoop)
        return

    await run_stress_test(args)


if __name__ == "__main__":
    asyncio.run(main())
