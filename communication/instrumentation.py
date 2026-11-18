"""
Lightweight performance instrumentation for UI cadence and stream latency tracking.

This module provides non-intrusive timing utilities for measuring:
- UI update cadence (target ~50Hz for status updates and playback progress)
- Stream operation latency (send-to-receive roundtrip time)

All measurements are optional and can be enabled/disabled via environment variable
or configuration flag. Aggregated stats are logged periodically to avoid spam.
"""

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CadenceSample:
    """Snapshot of cadence measurement at one point in time."""

    timestamp_ms: float
    interval_ms: float
    """Time since last sample in milliseconds."""

    frequency_hz: Optional[float] = None
    """Measured frequency in Hz (only if interval > 0)."""


class CadenceTracker:
    """
    Measures frequency of recurring events (e.g., UI updates).

    Non-blocking tracker that stores samples and periodically reports aggregated
    stats (min/max/avg frequency) to avoid per-event logging overhead.
    """

    def __init__(self, label: str, aggregation_interval: int = 100):
        """
        Initialize cadence tracker.

        Args:
            label: Human-readable name (e.g., "status_timer", "playback_progress")
            aggregation_interval: Number of samples before logging aggregated stats
        """
        self.label = label
        self.aggregation_interval = aggregation_interval
        self.sample_count = 0
        self.last_timestamp_ms: Optional[float] = None
        self.intervals_ms: list[float] = []

    def record_sample(self) -> CadenceSample:
        """
        Record one occurrence of the tracked event.

        Returns:
            Sample containing interval since last sample and computed frequency.
        """
        now_ms = time.time() * 1000.0
        self.sample_count += 1

        interval_ms = (
            now_ms - self.last_timestamp_ms if self.last_timestamp_ms else 0.0
        )
        self.last_timestamp_ms = now_ms

        sample = CadenceSample(
            timestamp_ms=now_ms,
            interval_ms=interval_ms,
            frequency_hz=1000.0 / interval_ms if interval_ms > 0 else None,
        )

        # Track interval for stats
        if interval_ms > 0:
            self.intervals_ms.append(interval_ms)

        # Report aggregated stats when threshold reached
        if len(self.intervals_ms) >= self.aggregation_interval:
            self._report_stats()

        return sample

    def _report_stats(self) -> None:
        """Log aggregated statistics and reset tracking."""
        if not self.intervals_ms:
            return

        min_interval = min(self.intervals_ms)
        max_interval = max(self.intervals_ms)
        avg_interval = sum(self.intervals_ms) / len(self.intervals_ms)

        min_hz = 1000.0 / max_interval  # Max interval → min frequency
        max_hz = 1000.0 / min_interval  # Min interval → max frequency
        avg_hz = 1000.0 / avg_interval

        logger.info(
            "[CADENCE] %s: %d samples | freq (min/avg/max): %.1f/%.1f/%.1f Hz | "
            "interval (min/avg/max): %.2f/%.2f/%.2f ms",
            self.label,
            len(self.intervals_ms),
            min_hz,
            avg_hz,
            max_hz,
            min_interval,
            avg_interval,
            max_interval,
        )

        self.intervals_ms.clear()


class LatencyTracker:
    """
    Measures roundtrip latency for named operations.

    Tracks send-time and receive-time markers, computing latency and
    aggregating stats over a window of measurements.
    """

    def __init__(self, label: str, aggregation_interval: int = 100):
        """
        Initialize latency tracker.

        Args:
            label: Human-readable name (e.g., "stream_send_recv")
            aggregation_interval: Number of samples before logging aggregated stats
        """
        self.label = label
        self.aggregation_interval = aggregation_interval
        self.pending_sends: dict[str, float] = {}  # op_id → send_time_ms
        self.latencies_ms: list[float] = []

    def mark_send(self, operation_id: str) -> None:
        """Record send timestamp for operation."""
        self.pending_sends[operation_id] = time.time() * 1000.0

    def mark_recv(self, operation_id: str) -> Optional[float]:
        """
        Record receive timestamp and compute latency.

        Args:
            operation_id: Identifier matching a prior send

        Returns:
            Latency in milliseconds, or None if no matching send
        """
        if operation_id not in self.pending_sends:
            logger.warning(
                "[LATENCY] %s: received callback for unknown operation %s",
                self.label,
                operation_id,
            )
            return None

        send_time_ms = self.pending_sends.pop(operation_id)
        latency_ms = time.time() * 1000.0 - send_time_ms
        self.latencies_ms.append(latency_ms)

        # Report aggregated stats when threshold reached
        if len(self.latencies_ms) >= self.aggregation_interval:
            self._report_stats()

        return latency_ms

    def _report_stats(self) -> None:
        """Log aggregated statistics and reset tracking."""
        if not self.latencies_ms:
            return

        min_latency = min(self.latencies_ms)
        max_latency = max(self.latencies_ms)
        avg_latency = sum(self.latencies_ms) / len(self.latencies_ms)

        logger.info(
            "[LATENCY] %s: %d samples | latency (min/avg/max): %.2f/%.2f/%.2f ms",
            self.label,
            len(self.latencies_ms),
            min_latency,
            avg_latency,
            max_latency,
        )

        self.latencies_ms.clear()


@contextmanager
def measure_latency(label: str, operation_id: Optional[str] = None):
    """
    Context manager for inline latency measurement (non-tracked).

    Logs latency of a code block for debugging purposes.

    Args:
        label: Human-readable operation name
        operation_id: Optional operation identifier for logging
    """
    start_ms = time.time() * 1000.0
    try:
        yield
    finally:
        end_ms = time.time() * 1000.0
        latency_ms = end_ms - start_ms
        op_suffix = f" ({operation_id})" if operation_id else ""
        logger.debug("[LATENCY] %s%s: %.2f ms", label, op_suffix, latency_ms)


def get_enabled() -> bool:
    """
    Check if instrumentation is enabled.

    Enabled via FALANGE_INSTRUMENTATION=1 environment variable or debug config.
    """
    import os

    return os.environ.get("FALANGE_INSTRUMENTATION", "0") == "1"
