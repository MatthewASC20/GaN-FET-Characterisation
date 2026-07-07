"""ZVS voltage tuning.

In this resonant rig the DC input current dips at the zero-voltage-switching
operating point (switching loss is minimal there, and the GaN output
capacitance is bus-voltage dependent, so the ZVS condition moves with
voltage). The tuner hill-descends the SMU bus voltage inside a bounded
window to find the current minimum.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from gan_fet.core.safety import SafetyMonitor
from gan_fet.instruments.smu import Keithley2400
from gan_fet.settings import ZvsSettings

log = logging.getLogger(__name__)


@dataclass
class ZvsResult:
    v_zvs: float
    i_min: float
    steps: int


class ZvsTuner:
    def __init__(self, smu: Keithley2400, settings: ZvsSettings, safety: SafetyMonitor):
        self.smu = smu
        self.settings = settings
        self.safety = safety

    def _avg_current(self) -> Optional[float]:
        readings = []
        for _ in range(max(1, self.settings.samples_per_point)):
            value = self.smu.measure_dc_current()
            if value is None:
                self.safety.record_read_failure("SMU current (ZVS)")
            else:
                self.safety.record_read_success()
                self.safety.check_sample(dc_current=value)
                readings.append(value)
            time.sleep(0.05)
        if not readings:
            return None
        return sum(readings) / len(readings)

    def _measure_at(self, volts: float) -> Optional[float]:
        self.smu.ramp_to(volts)
        time.sleep(self.settings.settle_s)
        self.safety.check_compliance()
        return self._avg_current()

    def find_minimum(
        self,
        *,
        cancel_check: Optional[Callable[[], bool]] = None,
        status: Optional[Callable[[str], None]] = None,
    ) -> Optional[ZvsResult]:
        """Hill-descend I(V) from the present setpoint. Ends at the found
        minimum (the SMU is left at v_zvs). Returns None if no direction
        improves or measurements fail."""
        cfg = self.settings
        v0 = self.smu.setpoint_v
        lo = max(0.0, v0 - cfg.window_v)
        hi = min(self.smu.settings.max_voltage_v, v0 + cfg.window_v)

        def cancelled() -> bool:
            return cancel_check is not None and cancel_check()

        i0 = self._measure_at(v0)
        if i0 is None:
            return None

        best_v, best_i = v0, i0
        steps = 0

        # Pick the descending direction by probing both neighbours.
        direction = 0
        for candidate in (+1, -1):
            probe_v = v0 + candidate * cfg.step_v
            if not (lo <= probe_v <= hi) or cancelled():
                continue
            probe_i = self._measure_at(probe_v)
            steps += 1
            if probe_i is not None and probe_i < best_i - cfg.min_improvement_a:
                direction, best_v, best_i = candidate, probe_v, probe_i
                break

        if direction == 0:
            self.smu.ramp_to(best_v)
            log.info("ZVS: already at minimum (%.2f V, %.4f A)", best_v, best_i)
            return ZvsResult(best_v, best_i, steps)

        # Walk downhill until the current stops improving or a bound is hit.
        while steps < cfg.max_steps and not cancelled():
            next_v = best_v + direction * cfg.step_v
            if not (lo <= next_v <= hi):
                break
            next_i = self._measure_at(next_v)
            steps += 1
            if next_i is None:
                break
            if status is not None:
                status(f"ZVS search: {next_v:.1f} V → {next_i * 1000:.2f} mA")
            if next_i < best_i - cfg.min_improvement_a:
                best_v, best_i = next_v, next_i
            else:
                break  # passed the minimum

        self.smu.ramp_to(best_v)
        time.sleep(cfg.settle_s)
        log.info("ZVS minimum: %.2f V @ %.4f A (%d steps)", best_v, best_i, steps)
        return ZvsResult(v_zvs=best_v, i_min=best_i, steps=steps)
