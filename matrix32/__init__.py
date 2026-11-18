"""E-Matrix32 control API with a validated stable-BLE readiness gate."""

from .config import build_config_messages, load_config, update_config
from .device import (
    LIVE_FREQUENCY_MIN_ACK_TO_WRITE_MS,
    LiveFrequencyUpdateError,
    Matrix32Device,
)
from .timing_marker import (
    SerialTimingMarker,
    TimingMarkerError,
    discover_timing_marker_port,
)

__all__ = [
    "Matrix32Device",
    "LiveFrequencyUpdateError",
    "LIVE_FREQUENCY_MIN_ACK_TO_WRITE_MS",
    "SerialTimingMarker",
    "TimingMarkerError",
    "discover_timing_marker_port",
    "build_config_messages",
    "load_config",
    "update_config",
]
