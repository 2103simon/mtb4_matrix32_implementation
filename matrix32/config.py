import argparse
import copy
from pathlib import Path

import yaml


SAFE_DEFAULTS = {
    "sa": [],
    "sc": 100,
    "sf": 50,
    "pw": 50,
    "sw": 50,
    "st": 10,
    "sv": 50,
}

GLOBAL_SETTINGS = {"sv", "sf", "pw", "st"}


def _ensure_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def load_config(path="config/stim_config.yaml"):
    config_path = Path(path)
    try:
        with config_path.open(encoding="utf-8") as stream:
            loaded = yaml.safe_load(stream) or {}
    except FileNotFoundError:
        print(f"Config file not found: {config_path}. Using built-in defaults.")
        loaded = {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {config_path}: {exc}") from exc
    return {**SAFE_DEFAULTS, **loaded}


def _parser(add_help=True):
    parser = argparse.ArgumentParser(
        description="E-Matrix32 stimulation configuration",
        add_help=add_help,
    )
    parser.add_argument("-sa", type=int, nargs="+", help="Active channels")
    parser.add_argument("-sc", type=int, nargs="+", help="Stimulation current")
    parser.add_argument("-sf", type=int, help="Stimulation frequency [Hz]")
    parser.add_argument("-pw", type=int, help="Global pulse width [us]")
    parser.add_argument("-sw", type=int, nargs="+", help="Per-channel pulse width [us]")
    parser.add_argument("-st", type=int, help="Time-between-pulses setting. Historical host note assumed 100 us/count; M02 waveform data did not support that exact onset-to-onset scaling.")
    parser.add_argument("-sv", type=int, help="Output voltage [V]")
    return parser


def parse_config_args(config, argv=None):
    args = _parser().parse_args(argv)
    updates = {key: value for key, value in vars(args).items() if value is not None}
    return _merge_updates(config, updates)


def update_config(config, command):
    normalized = command.replace(",", " ")
    try:
        args, unknown = _parser(add_help=False).parse_known_args(normalized.split())
    except SystemExit:
        print("[Update error] Invalid configuration command.")
        return config

    if unknown:
        print(f"[Update error] Unknown arguments: {' '.join(unknown)}")
        return config

    updates = {key: value for key, value in vars(args).items() if value is not None}
    if not updates:
        print("[Update error] No configuration values supplied.")
        return config

    try:
        updated = _merge_updates(config, updates)
    except ValueError as exc:
        print(f"[Update error] {exc}")
        return config

    print(f"Old config: {config}")
    print(f"New config: {updated}")
    return updated


def _merge_updates(config, updates):
    result = copy.deepcopy(config)
    old_channels = _ensure_list(result.get("sa"))
    new_channels = _ensure_list(updates.get("sa", old_channels))

    invalid_channels = [channel for channel in new_channels if not 1 <= channel <= 32]
    if invalid_channels:
        raise ValueError(f"Channels must be between 1 and 32: {invalid_channels}")

    result.update({key: value for key, value in updates.items() if key in GLOBAL_SETTINGS})
    if not isinstance(result.get("sf"), int) or not 1 <= result["sf"] <= 400:
        raise ValueError("Manufacturer global SF frequency must be integer 1..400 Hz")
    result["sa"] = new_channels

    old_currents = _expand_per_channel(result.get("sc"), len(old_channels), SAFE_DEFAULTS["sc"])
    old_widths = _expand_per_channel(result.get("sw"), len(old_channels), SAFE_DEFAULTS["sw"])
    current_by_channel = dict(zip(old_channels, old_currents))
    width_by_channel = dict(zip(old_channels, old_widths))

    if "sc" in updates:
        currents = _expand_per_channel(updates["sc"], len(new_channels), SAFE_DEFAULTS["sc"])
    else:
        currents = [current_by_channel.get(ch, SAFE_DEFAULTS["sc"]) for ch in new_channels]

    if "sw" in updates:
        widths = _expand_per_channel(updates["sw"], len(new_channels), SAFE_DEFAULTS["sw"])
    else:
        widths = [width_by_channel.get(ch, SAFE_DEFAULTS["sw"]) for ch in new_channels]

    ordered = sorted(zip(new_channels, currents, widths), key=lambda item: item[0])
    result["sa"] = [item[0] for item in ordered]
    result["sc"] = [item[1] for item in ordered]
    result["sw"] = [item[2] for item in ordered]
    return result


def _expand_per_channel(value, count, default):
    values = _ensure_list(value)
    if count == 0:
        return []
    if not values:
        values = [default]
    if len(values) < count:
        values = values + [default] * (count - len(values))
    return values[:count]


def build_config_messages(config):
    channels = _ensure_list(config["sa"])
    currents = _expand_per_channel(config.get("sc"), len(channels), SAFE_DEFAULTS["sc"])
    widths = _expand_per_channel(config.get("sw"), len(channels), SAFE_DEFAULTS["sw"])

    channel_bitmask = 0
    anode_bitmask = 0
    for channel in channels:
        if not 1 <= channel <= 32:
            raise ValueError(f"Channel out of range: {channel}")
        channel_bitmask |= 1 << (channel - 1)
        anode_bitmask |= 1 << ((channel - 1) // 8)

    msg_sa = b">SA;" + channel_bitmask.to_bytes(4, "big") + b"<"
    msg_an = b">AN;" + anode_bitmask.to_bytes(1, "big") + b"<"

    amplitudes = [0] * 32
    for channel, current in zip(channels, currents):
        amplitudes[channel - 1] = current
    msg_sc = b">SC;" + b"".join(value.to_bytes(2, "big") for value in amplitudes) + b"<"

    frequency = config["sf"]
    if not isinstance(frequency, int) or not 1 <= frequency <= 400:
        raise ValueError("Manufacturer global SF frequency must be integer 1..400 Hz")
    msg_sf = b">SF;" + frequency.to_bytes(2, "big") + b"<"

    if widths:
        pulses = [0] * 32
        for channel, width in zip(channels, widths):
            pulses[channel - 1] = width
        msg_pulse = b">SW;" + b"".join(value.to_bytes(2, "big") for value in pulses) + b"<"
    else:
        msg_pulse = b">PW;" + int(config["pw"]).to_bytes(2, "big") + b"<"

    msg_sv = b">SV;" + int(config["sv"]).to_bytes(1, "big") + b"<"
    msg_st = b">ST;" + int(config["st"]).to_bytes(1, "big") + b"<"
    return [msg_sa, msg_an, msg_sc, msg_sf, msg_pulse, msg_sv, msg_st]
