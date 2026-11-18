"""Clock drift monitoring helpers for live MCU vs PC timeline comparison."""

from __future__ import annotations

import csv
import logging
import math
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional


@dataclass(frozen=True)
class DriftSample:
    """Single drift sample expressed as elapsed times from the first frame."""

    pc_elapsed_ms: float
    mcu_elapsed_ms: float
    drift_ms: float


class ClockDriftMonitor:
    """Collects and summarizes MCU-vs-PC drift samples with bounded memory usage."""

    def __init__(self, max_buffer_samples: int = 20000, logger: Optional[logging.Logger] = None) -> None:
        self.logger = logger or logging.getLogger(__name__)
        self.max_buffer_samples = max(1, int(max_buffer_samples))
        self.samples: Deque[DriftSample] = deque(maxlen=self.max_buffer_samples)
        self._mcu_anchor_ms: Optional[int] = None
        self._pc_anchor_ns: Optional[int] = None
        self._total_samples = 0
        self._sum_x = 0.0
        self._sum_y = 0.0
        self._sum_xx = 0.0
        self._sum_yy = 0.0
        self._sum_xy = 0.0
        self._first_drift_ms = 0.0
        self._last_pc_elapsed_ms = 0.0
        self._last_drift_ms = 0.0

    def reset(self) -> None:
        self.samples.clear()
        self._mcu_anchor_ms = None
        self._pc_anchor_ns = None
        self._total_samples = 0
        self._sum_x = 0.0
        self._sum_y = 0.0
        self._sum_xx = 0.0
        self._sum_yy = 0.0
        self._sum_xy = 0.0
        self._first_drift_ms = 0.0
        self._last_pc_elapsed_ms = 0.0
        self._last_drift_ms = 0.0

    def add_sample(self, mcu_timestamp_ms: int, pc_receive_ns: int) -> None:
        """Add one sample pair and compute elapsed drift against the first sample."""
        mcu_ms = int(mcu_timestamp_ms)
        pc_ns = int(pc_receive_ns)

        if self._mcu_anchor_ms is None or self._pc_anchor_ns is None:
            self._mcu_anchor_ms = mcu_ms
            self._pc_anchor_ns = pc_ns

        pc_elapsed_ms = (pc_ns - self._pc_anchor_ns) / 1_000_000.0
        mcu_elapsed_ms = float(mcu_ms - self._mcu_anchor_ms)
        drift_ms = mcu_elapsed_ms - pc_elapsed_ms

        self.samples.append(
            DriftSample(
                pc_elapsed_ms=pc_elapsed_ms,
                mcu_elapsed_ms=mcu_elapsed_ms,
                drift_ms=drift_ms,
            )
        )
        if self._total_samples == 0:
            self._first_drift_ms = drift_ms
        self._sum_x += pc_elapsed_ms
        self._sum_y += drift_ms
        self._sum_xx += pc_elapsed_ms * pc_elapsed_ms
        self._sum_yy += drift_ms * drift_ms
        self._sum_xy += pc_elapsed_ms * drift_ms
        self._last_pc_elapsed_ms = pc_elapsed_ms
        self._last_drift_ms = drift_ms
        self._total_samples += 1

    def _linear_fit_full_session(self) -> tuple[float, float, float]:
        """Return slope(ms/ms), intercept(ms), and R^2 for all samples seen."""
        n = float(self._total_samples)
        if n < 2.0:
            return 0.0, 0.0, 0.0

        den = n * self._sum_xx - self._sum_x * self._sum_x
        if den == 0.0:
            return 0.0, 0.0, 0.0

        slope = (n * self._sum_xy - self._sum_x * self._sum_y) / den
        intercept = (self._sum_y - slope * self._sum_x) / n

        den_corr = (n * self._sum_xx - self._sum_x * self._sum_x) * (
            n * self._sum_yy - self._sum_y * self._sum_y
        )
        if den_corr <= 0.0:
            r2 = 0.0
        else:
            corr = (n * self._sum_xy - self._sum_x * self._sum_y) / math.sqrt(den_corr)
            r2 = corr * corr

        return slope, intercept, r2

    def summary(self) -> dict[str, float]:
        """Return compact session and retained-window drift diagnostics."""
        if not self.samples:
            return {
                "sample_count": 0.0,
                "total_samples_seen": 0.0,
                "dropped_samples": 0.0,
                "duration_ms": 0.0,
                "drift_start_ms": 0.0,
                "drift_end_ms": 0.0,
                "drift_rate_ppm": 0.0,
                "drift_span_ms": 0.0,
                "session_duration_ms": 0.0,
                "session_drift_span_ms": 0.0,
                "session_drift_rate_ppm": 0.0,
                "session_fit_intercept_ms": 0.0,
                "session_fit_r2": 0.0,
            }

        first = self.samples[0]
        last = self.samples[-1]
        duration_ms = max(0.0, last.pc_elapsed_ms - first.pc_elapsed_ms)
        drift_start_ms = first.drift_ms
        drift_end_ms = last.drift_ms
        drift_span_ms = drift_end_ms - drift_start_ms
        drift_rate_ppm = (drift_span_ms / duration_ms * 1_000_000.0) if duration_ms > 0.0 else 0.0
        slope_full, intercept_full, r2_full = self._linear_fit_full_session()
        retained = float(len(self.samples))
        total_seen = float(self._total_samples)
        dropped = max(0.0, total_seen - retained)

        return {
            "sample_count": retained,
            "total_samples_seen": total_seen,
            "dropped_samples": dropped,
            "duration_ms": duration_ms,
            "drift_start_ms": drift_start_ms,
            "drift_end_ms": drift_end_ms,
            "drift_rate_ppm": drift_rate_ppm,
            "drift_span_ms": drift_span_ms,
            "session_duration_ms": max(0.0, self._last_pc_elapsed_ms),
            "session_drift_span_ms": self._last_drift_ms - self._first_drift_ms,
            "session_drift_rate_ppm": slope_full * 1_000_000.0,
            "session_fit_intercept_ms": intercept_full,
            "session_fit_r2": r2_full,
        }

    def total_samples_seen(self) -> int:
        return int(self._total_samples)

    def export_samples_csv(self, file_path: str) -> None:
        """Export retained samples to CSV for offline drift analysis."""
        with open(file_path, "w", newline="") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(["sample_index", "pc_elapsed_ms", "mcu_elapsed_ms", "drift_ms"])
            for idx, sample in enumerate(self.samples):
                writer.writerow([idx, sample.pc_elapsed_ms, sample.mcu_elapsed_ms, sample.drift_ms])
