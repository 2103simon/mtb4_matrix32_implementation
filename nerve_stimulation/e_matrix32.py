"""Interactive E-Matrix32 hardware controller.

This is the primary application entry point. It uses Matrix32Device.connect(),
which does not expose the device as ready until the BLE link survives the
stabilization window and completes a non-stimulating battery request/response.

Connecting does not start stimulation automatically; the operator must
explicitly request it.
"""

import asyncio
from pathlib import Path
import sys

from prompt_toolkit import PromptSession

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from matrix32 import Matrix32Device
from matrix32.config import load_config, parse_config_args, update_config


DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "stim_config.yaml"

# Hardware-debug pacing: use the original 20 ms spacing between configuration
# writes now that the output path has been verified to produce stimulation.
HARDWARE_DEBUG_APPLY_SETTLE_SECONDS = 0.02


def print_help():
    print(
        "\nCommands:\n"
        "  status              show connection, battery, run state, and config\n"
        "  battery             request a fresh battery report\n"
        "  apply               send the current configuration to the device\n"
        "  start               apply the current configuration, then start stimulation\n"
        "  stop                stop stimulation and switch the output stage off\n"
        "  set <options>       update the in-memory config (does not auto-apply)\n"
        "  reconnect           stop if possible, disconnect, and obtain a new stable link\n"
        "  help                show this help\n"
        "  q / quit / exit     stop if possible, disconnect, and exit\n"
        "\nConfiguration options:\n"
        "  Per-channel / channel-selection:\n"
        "    -sa CH [CH ...]   active channels, 1..32\n"
        "                      example: -sa 1 2\n"
        "    -sc VAL [VAL ...] stimulation current, one value per active channel\n"
        "                      current scale: 100 = 1 mA\n"
        "                      example: -sc 50 75\n"
        "    -sw US [US ...]  pulse width [us], one value per active channel\n"
        "                      example: -sw 100 150\n"
        "\n"
        "  Global (applies to all active channels):\n"
        "    -sf HZ            stimulation frequency [Hz]\n"
        "    -sv V             output voltage [V]\n"
        "    -st TICKS         timing between pulses [100 us per tick]\n"
        "    -pw US            global pulse width [us]; fallback when per-channel\n"
        "                      -sw values are not being used\n"
        "\n"
        "  Example:\n"
        "    set -sa 1 2 -sc 50 75 -sw 100 150 -sf 10 -sv 50 -st 10\n"
        "\n"
        "  Note: 'set' changes only the in-memory configuration. Use 'apply'\n"
        "  to send it without starting, or 'start' to apply and start.\n"
    )


def print_status(stim):
    print("\n=== Matrix32 status ===")
    print(f"Connected: {'yes' if stim.connected else 'no'}")
    print(f"Stimulation running: {'yes' if stim.running else 'no'}")
    battery = (
        f"{stim.last_battery_level}%"
        if stim.last_battery_level is not None
        else "unknown"
    )
    print(f"Last battery report: {battery}")
    print(f"Configuration: {stim.config}")
    print("=======================")


async def main():
    session = PromptSession()
    config = parse_config_args(load_config(DEFAULT_CONFIG))
    stim = Matrix32Device(config=config)

    print("E-Matrix32 hardware controller")
    print(
        "Connecting with the validated BLE readiness gate. "
        "No stimulation command is sent during connection."
    )

    try:
        await stim.connect()
        print(
            f"\n[ready] Stable two-way BLE session established. "
            f"Battery: {stim.last_battery_level}%."
        )
        print("[ready] Stimulation is NOT running.")
        print(
            f"[debug] Configuration writes are paced at "
            f"{HARDWARE_DEBUG_APPLY_SETTLE_SECONDS:.1f}s between commands."
        )
        print_status(stim)
        print_help()

        while True:
            command = (await session.prompt_async("Matrix32> ")).strip()
            if not command:
                continue

            lowered = command.lower()

            if lowered in {"q", "quit", "exit"}:
                break

            if lowered in {"help", "?"}:
                print_help()
                continue

            if lowered == "status":
                print_status(stim)
                continue

            if lowered == "battery":
                level = await stim.read_battery()
                print(f"Battery: {level}%")
                continue

            if lowered == "apply":
                records = await stim.apply(
                    settle_time=HARDWARE_DEBUG_APPLY_SETTLE_SECONDS,
                    require_ack=True,
                )
                print("Firmware ACKs:", [(x["command"], x["status"]) for x in records])
                print("Configuration verified by firmware indications.")
                continue

            if lowered == "start":
                if stim.running:
                    print("Stimulation is already running.")
                    continue
                print(f"Applying configuration: {stim.config}")
                await stim.apply(
                    settle_time=HARDWARE_DEBUG_APPLY_SETTLE_SECONDS,
                    require_ack=True,
                )
                print("Configuration applied.")
                await stim.start()
                continue

            if lowered == "stop":
                if not stim.running:
                    print("Stimulation is not marked as running.")
                    continue
                await stim.stop()
                continue

            if lowered == "reconnect":
                was_running = stim.running
                if was_running:
                    print(
                        "Stopping stimulation before reconnect. "
                        "It will NOT restart automatically."
                    )
                await stim.reconnect()
                print(
                    f"[ready] Reconnected with battery "
                    f"{stim.last_battery_level}%."
                )
                print(
                    "[ready] Configuration remains in memory, but stimulation "
                    "is stopped. Use 'start' to apply it and start again."
                )
                continue

            if lowered.startswith("set "):
                new_config = update_config(stim.config, command[4:].strip())
                if new_config == stim.config:
                    print("Configuration unchanged.")
                    continue
                stim.set_config(new_config)
                print(
                    "Configuration updated in memory. "
                    "Use 'apply' to send it, or 'start' to apply and start."
                )
                continue

            print("[Error] Unknown command. Type 'help' for available commands.")

    finally:
        if stim.client is not None:
            print("\n[shutdown] Stopping/disconnecting Matrix32...")
            await stim.disconnect()
        print("[shutdown] Done.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
    except Exception as exc:
        print(f"[Fatal error] {type(exc).__name__}: {exc}")
