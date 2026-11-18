"""Adaptive MCU timestamp compensation based on PC receive-time observations."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ClockCompensationSettings:
    enabled: bool = False
    initial_ppm: float = 0.0
    adaptive_enabled: bool = True
    sample_source: str = "taxel"
    update_interval_samples: int = 2000
    min_fit_duration_ms: int = 30000
    min_fit_samples: int = 5000
    min_fit_r2: float = 0.995
    ema_alpha: float = 0.15
    max_step_ppm_per_update: float = 100.0
    log_updates: bool = True


class ClockCompensator:
    """Compensates MCU timestamps and optionally refines scale during runtime."""

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self.logger = logger or logging.getLogger(__name__)
        self.settings = ClockCompensationSettings()
        self._scale = 1.0
        self._anchor_raw_ms: Optional[int] = None
        self._anchor_corrected_ms: float = 0.0

        self._fit_raw_anchor_ms: Optional[int] = None
        self._fit_pc_anchor_ns: Optional[int] = None
        self._fit_samples = 0
        self._sum_x = 0.0
        self._sum_y = 0.0
        self._sum_xx = 0.0
        self._sum_yy = 0.0
        self._sum_xy = 0.0

    def configure(self, settings: ClockCompensationSettings) -> None:
        self.settings = settings
        self.reset_session()

    def reset_session(self) -> None:
        self._scale = self._scale_from_ppm(self.settings.initial_ppm)
        self._anchor_raw_ms = None
        self._anchor_corrected_ms = 0.0
        self._fit_raw_anchor_ms = None
        self._fit_pc_anchor_ns = None
        self._fit_samples = 0
        self._sum_x = 0.0
        self._sum_y = 0.0
        self._sum_xx = 0.0
        self._sum_yy = 0.0
        self._sum_xy = 0.0

    def current_scale(self) -> float:
        return float(self._scale)

    def current_ppm(self) -> float:
        if self._scale <= 0.0:
            return 0.0
        return (1.0 / self._scale - 1.0) * 1_000_000.0

    def compensate(self, raw_mcu_ms: int, pc_receive_ns: Optional[int] = None, *, allow_adaptive_update: bool = False) -> int:
        raw_ms = int(raw_mcu_ms)

        if self._anchor_raw_ms is None:
            self._anchor_raw_ms = raw_ms
            self._anchor_corrected_ms = float(raw_ms)

        if self.settings.enabled and self.settings.adaptive_enabled and allow_adaptive_update and pc_receive_ns is not None:
            self._update_fit_stats(raw_ms, int(pc_receive_ns))
            self._maybe_update_scale(raw_ms)

        if not self.settings.enabled:
            return raw_ms

        corrected = self._anchor_corrected_ms + self._scale * (raw_ms - self._anchor_raw_ms)
        return int(round(corrected))

    def _update_fit_stats(self, raw_ms: int, pc_ns: int) -> None:
        if self._fit_raw_anchor_ms is None or self._fit_pc_anchor_ns is None:
            self._fit_raw_anchor_ms = raw_ms
            self._fit_pc_anchor_ns = pc_ns

        x = float(raw_ms - self._fit_raw_anchor_ms)
        y = float(pc_ns - self._fit_pc_anchor_ns) / 1_000_000.0

        self._fit_samples += 1
        self._sum_x += x
        self._sum_y += y
        self._sum_xx += x * x
        self._sum_yy += y * y
        self._sum_xy += x * y

    def _maybe_update_scale(self, current_raw_ms: int) -> None:
        interval = max(1, int(self.settings.update_interval_samples))
        if self._fit_samples < max(2, int(self.settings.min_fit_samples)):
            return
        if self._fit_samples % interval != 0:
            return

        raw_anchor = self._fit_raw_anchor_ms
        if raw_anchor is None:
            return
        duration_ms = float(current_raw_ms - raw_anchor)
        if duration_ms < float(max(1, int(self.settings.min_fit_duration_ms))):
            return

        fit = self._linear_fit()
        if fit is None:
            return
        beta_fit, r2 = fit

        if r2 < float(self.settings.min_fit_r2):
            return
        if beta_fit <= 0.0:
            return

        max_step_scale = float(self.settings.max_step_ppm_per_update) * 1e-6
        target = beta_fit
        delta = target - self._scale
        if delta > max_step_scale:
            target = self._scale + max_step_scale
        elif delta < -max_step_scale:
            target = self._scale - max_step_scale

        alpha = min(1.0, max(0.0, float(self.settings.ema_alpha)))
        new_scale = (1.0 - alpha) * self._scale + alpha * target

        if abs(new_scale - self._scale) < 1e-12:
            return

        current_corrected = self._anchor_corrected_ms + self._scale * (current_raw_ms - self._anchor_raw_ms)
        previous_scale = self._scale
        self._scale = new_scale
        self._anchor_raw_ms = int(current_raw_ms)
        self._anchor_corrected_ms = float(current_corrected)

        if self.settings.log_updates:
            self.logger.info(
                "Clock compensation update: fit_r2=%.6f scale=%.9f->%.9f ppm=%.2f",
                r2,
                previous_scale,
                self._scale,
                self.current_ppm(),
            )

    def _linear_fit(self) -> Optional[tuple[float, float]]:
        n = float(self._fit_samples)
        den = n * self._sum_xx - self._sum_x * self._sum_x
        if den == 0.0:
            return None

        slope = (n * self._sum_xy - self._sum_x * self._sum_y) / den
        den_corr = (n * self._sum_xx - self._sum_x * self._sum_x) * (n * self._sum_yy - self._sum_y * self._sum_y)
        if den_corr <= 0.0:
            r2 = 0.0
        else:
            corr = (n * self._sum_xy - self._sum_x * self._sum_y) / math.sqrt(den_corr)
            r2 = corr * corr

        return slope, r2

    @staticmethod
    def _scale_from_ppm(ppm: float) -> float:
        correction = 1.0 + (float(ppm) * 1e-6)
        if correction <= 0.0:
            return 1.0
        return 1.0 / correction
