"""Wavegen state tracking and frequency autotune.

The rig's resonant point drifts between builds, so a previously measured
switching frequency (stored on completed runs) is a better starting point
than the nominal one. Search order for a prior tuned frequency, unchanged
from v1: same temperature + same config first, then same temperature +
other configs, then the 25 °C equivalents.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from gan_fet.core.models import MatrixPoint
from gan_fet.instruments.wavegen import Sdg6022x
from gan_fet.storage.db import Database

log = logging.getLogger(__name__)

AUTOTUNE_STEP_HZ = 100_000       # 0.1 MHz per step
AUTOTUNE_STEP_DELAY_S = 0.5


def tuned_frequency_search_order(
    temperature_c: int, config: str, all_configs: list[str]
) -> list[tuple[int, str]]:
    order = [(temperature_c, config)]
    order += [(temperature_c, c) for c in all_configs if c != config]
    if temperature_c != 25:
        order.append((25, config))
        order += [(25, c) for c in all_configs if c != config]
    return order


def find_prior_tuned_frequency(
    db: Database, point: MatrixPoint, all_configs: list[str]
) -> Optional[tuple[float, str, int]]:
    """Returns (fsw_hz, source_config, source_temp) or None."""
    order = tuned_frequency_search_order(point.temperature_c, point.config, all_configs)
    return db.prior_tuned_frequency(
        point.device_name, point.frequency_hz, point.duty_pct, point.voltage_v, order
    )


class WavegenController:
    """Tracks what has actually been applied to the SDG6022X so the UI can
    show pending-change state, and performs gradual frequency ramps."""

    def __init__(self, wavegen: Sdg6022x):
        self.wavegen = wavegen
        self._lock = threading.Lock()
        self.applied_config: Optional[str] = None
        self.applied_freq_hz: Optional[float] = None   # nominal selection
        self.applied_duty: Optional[int] = None
        self.tuned_freq_hz: Optional[float] = None     # actual (possibly tuned)

    def has_pending_changes(self, config: str, freq_hz: int, duty: int) -> bool:
        with self._lock:
            if self.applied_config is None:
                return True
            return (
                config != self.applied_config
                or freq_hz != self.applied_freq_hz
                or duty != self.applied_duty
            )

    def invalidate_tuning(self) -> None:
        with self._lock:
            self.tuned_freq_hz = None

    def apply(self, config: str, freq_hz: int, duty: int) -> None:
        """Apply selection to the instrument (full reconfigure on config
        change, minimal updates otherwise — v1 semantics)."""
        with self._lock:
            dual = self.wavegen.is_dual(config)
            if config != self.applied_config:
                self.wavegen.configure(config, freq_hz, duty)
            else:
                if freq_hz != self.applied_freq_hz:
                    self.wavegen.set_frequency(freq_hz, dual)
                if duty != self.applied_duty:
                    self.wavegen.set_duty(duty, dual)
            self.applied_config = config
            self.applied_freq_hz = freq_hz
            self.applied_duty = duty
            self.tuned_freq_hz = float(freq_hz)

    def ramp_to_frequency(
        self,
        target_hz: float,
        config: str,
        *,
        cancel_check: Optional[Callable[[], bool]] = None,
        status: Optional[Callable[[str], None]] = None,
    ) -> float:
        """Step the output frequency gradually to target_hz (resonant rigs
        dislike jumps). Returns the start frequency."""
        dual = self.wavegen.is_dual(config)

        start = self.wavegen.read_frequency()
        if start is None:
            with self._lock:
                start = self.tuned_freq_hz or self.applied_freq_hz
        if start is None:
            raise RuntimeError("Cannot determine current wavegen frequency")

        current = float(start)
        target = float(target_hz)
        step = AUTOTUNE_STEP_HZ if target > current else -AUTOTUNE_STEP_HZ

        while abs(target - current) > 1:
            if cancel_check is not None and cancel_check():
                break
            current = target if abs(target - current) <= abs(step) else current + step
            self.wavegen.set_frequency(current, dual)
            if status is not None:
                status(f"Autotune: {current / 1e6:.2f} MHz")
            if current != target:
                time.sleep(AUTOTUNE_STEP_DELAY_S)

        with self._lock:
            self.tuned_freq_hz = target
        log.info("Autotune ramp complete: %.0f Hz → %.0f Hz", start, target)
        return float(start)
