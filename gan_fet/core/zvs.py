"""ZVS voltage tuning.

In this resonant rig the DC input current dips at the zero-voltage-switching
operating point (switching loss is minimal there, and the GaN output
capacitance is bus-voltage dependent, so the ZVS condition moves with
voltage). The tuner hill-descends the SMU bus voltage inside a bounded
window to find the current minimum.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from gan_fet.core.safety import SafetyMonitor
from gan_fet.instruments.base import OscilloscopeInterface, SmuInterface
from gan_fet.settings import ZvsSettings

log = logging.getLogger(__name__)


@dataclass
class ZvsResult:
    v_zvs: float
    i_min: float
    steps: int


class ZvsMeasurementError(RuntimeError):
    """Required ZVS measurements were unavailable or invalid."""


class ZvsTuner:
    def __init__(
        self,
        smu: SmuInterface,
        settings: ZvsSettings,
        safety: SafetyMonitor,
        scope: Optional[OscilloscopeInterface] = None,
    ):
        self.smu = smu
        self.settings = settings
        self.safety = safety
        self.scope = scope

    @staticmethod
    def _cancelled(cancel_check: Optional[Callable[[], bool]]) -> bool:
        return cancel_check is not None and cancel_check()

    @staticmethod
    def _finite_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        return numeric if math.isfinite(numeric) else None

    def _wait(
        self,
        seconds: float,
        cancel_check: Optional[Callable[[], bool]],
    ) -> bool:
        """Return ``False`` when cancellation interrupts a settling wait."""
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            if self._cancelled(cancel_check):
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(0.05, remaining))

    def _avg_current(
        self,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Optional[float]:
        readings = []
        for _ in range(max(1, self.settings.samples_per_point)):
            if self._cancelled(cancel_check):
                return None
            try:
                value = self.smu.measure_dc_current()
            except Exception as exc:
                log.warning("SMU current query failed during ZVS search: %s", exc)
                value = None
            try:
                vds_pk = (
                    self.scope.peak_voltage() if self.scope is not None else None
                )
            except Exception as exc:
                log.warning("Scope peak query failed during ZVS search: %s", exc)
                vds_pk = None

            numeric_peak = self._finite_float(vds_pk)
            if self.scope is not None:
                if numeric_peak is None:
                    self.safety.record_read_failure("scope peak voltage (ZVS)")
                else:
                    self.safety.record_read_success("scope peak voltage (ZVS)")
                    self.safety.check_sample(vds_peak=numeric_peak)

            current = self._finite_float(value)
            if current is None:
                self.safety.record_read_failure("SMU current (ZVS)")
            else:
                self.safety.record_read_success("SMU current (ZVS)")
                self.safety.check_sample(dc_current=current)
                readings.append(current)
            self.safety.check_compliance()
            if not self._wait(0.05, cancel_check):
                return None
        if not readings:
            return None
        return sum(readings) / len(readings)

    def _measure_at(
        self,
        volts: float,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Optional[float]:
        self.smu.ramp_to(volts, cancel_check=cancel_check)
        if self._cancelled(cancel_check):
            return None
        if not self._wait(self.settings.settle_s, cancel_check):
            return None
        self.safety.check_compliance()
        current = self._avg_current(cancel_check)
        if current is not None:
            self.safety.check_compliance()
        if current is None and not (
            cancel_check is not None and cancel_check()
        ):
            raise ZvsMeasurementError(
                f"Could not measure input current at {volts:.2f} V"
            )
        return current

    def find_minimum(
        self,
        *,
        cancel_check: Optional[Callable[[], bool]] = None,
        status: Optional[Callable[[str], None]] = None,
    ) -> Optional[ZvsResult]:
        """Hill-descend I(V) from the present setpoint. Ends at the found
        minimum (the SMU is left at v_zvs). Returns ``None`` only when
        cancelled and raises :class:`ZvsMeasurementError` on bad telemetry."""
        cfg = self.settings
        v0 = self.smu.setpoint_v
        lo = max(0.0, v0 - cfg.window_v)
        hi = min(self.smu.max_voltage_v, v0 + cfg.window_v)

        def cancelled() -> bool:
            return self._cancelled(cancel_check)

        i0 = self._measure_at(v0, cancel_check)
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
            probe_i = self._measure_at(probe_v, cancel_check)
            steps += 1
            if probe_i is not None and probe_i < best_i - cfg.min_improvement_a:
                direction, best_v, best_i = candidate, probe_v, probe_i
                break

        if direction == 0:
            if cancelled():
                return None
            self.smu.ramp_to(best_v, cancel_check=cancel_check)
            if cancelled():
                return None
            if not self._wait(cfg.settle_s, cancel_check):
                return None
            self.safety.check_compliance()
            log.info("ZVS: already at minimum (%.2f V, %.4f A)", best_v, best_i)
            return ZvsResult(best_v, best_i, steps)

        # Walk downhill until the current stops improving or a bound is hit.
        while steps < cfg.max_steps and not cancelled():
            next_v = best_v + direction * cfg.step_v
            if not (lo <= next_v <= hi):
                break
            next_i = self._measure_at(next_v, cancel_check)
            steps += 1
            if next_i is None:
                break
            if status is not None:
                status(f"ZVS search: {next_v:.1f} V → {next_i * 1000:.2f} mA")
            if next_i < best_i - cfg.min_improvement_a:
                best_v, best_i = next_v, next_i
            else:
                break  # passed the minimum

        if cancelled():
            return None
        self.smu.ramp_to(best_v, cancel_check=cancel_check)
        if cancelled():
            return None
        if not self._wait(cfg.settle_s, cancel_check):
            return None
        self.safety.check_compliance()
        log.info("ZVS minimum: %.2f V @ %.4f A (%d steps)", best_v, best_i, steps)
        return ZvsResult(v_zvs=best_v, i_min=best_i, steps=steps)
