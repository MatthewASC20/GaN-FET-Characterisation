"""Closed-loop Vds peak control.

The selected test "voltage" (200/300/400 V) is the scope-measured Vds peak,
not the bus voltage. v1 asked the operator to twiddle a bench supply until
the peak matched (±2 V) and then merely validated it. With the SMU as the
bus source, this controller closes the loop automatically: adjust the SMU
setpoint, wait, read the HDO4054 P1 measurement, and repeat until
|target - peak| <= tolerance.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Callable, Optional

from gan_fet.core.safety import SafetyMonitor
from gan_fet.instruments.base import OscilloscopeInterface, SmuInterface
from gan_fet.instruments.smu import SmuLimitError
from gan_fet.settings import PeakControlSettings

log = logging.getLogger(__name__)


class PeakControlError(RuntimeError):
    pass


class PeakVoltageController:
    def __init__(
        self,
        smu: SmuInterface,
        scope: OscilloscopeInterface,
        settings: PeakControlSettings,
        safety: SafetyMonitor,
    ):
        self.smu = smu
        self.scope = scope
        self.settings = settings
        self.safety = safety

    @staticmethod
    def _cancelled(cancel_check: Optional[Callable[[], bool]]) -> bool:
        return cancel_check is not None and cancel_check()

    def _wait(
        self,
        seconds: float,
        cancel_check: Optional[Callable[[], bool]],
    ) -> None:
        """Wait in short slices so cancellation is not hidden by settling."""
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            if self._cancelled(cancel_check):
                raise PeakControlError("cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.05, remaining))

    def _read_peak(
        self,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Optional[float]:
        try:
            peak = self.scope.peak_voltage()
        except Exception as exc:
            log.warning("Scope peak query failed during peak control: %s", exc)
            peak = None
        if peak is None:
            # Stale/failed read — retry once after a short delay (v1 behaviour).
            self._wait(0.3, cancel_check)
            try:
                peak = self.scope.peak_voltage()
            except Exception as exc:
                log.warning(
                    "Scope peak retry failed during peak control: %s", exc
                )
                peak = None
        if peak is None:
            return None
        try:
            numeric_peak = float(peak)
        except (TypeError, ValueError):
            log.warning("Scope returned a non-numeric peak reading: %r", peak)
            return None
        if not math.isfinite(numeric_peak):
            log.warning("Scope returned a non-finite peak-voltage reading: %r", peak)
            return None
        return numeric_peak

    def _check_input_current(self) -> None:
        """Enforce the independent DC-current ceiling during peak control."""
        try:
            current = self.smu.measure_dc_current()
        except Exception as exc:
            log.warning("SMU current query failed during peak control: %s", exc)
            current = None
        try:
            numeric_current = float(current) if current is not None else None
        except (TypeError, ValueError):
            numeric_current = None
        if numeric_current is None or not math.isfinite(numeric_current):
            self.safety.record_read_failure("SMU current (peak control)")
        else:
            self.safety.record_read_success("SMU current (peak control)")
            self.safety.check_sample(dc_current=numeric_current)

    def achieve_peak(
        self,
        target_v: float,
        *,
        cancel_check: Optional[Callable[[], bool]] = None,
        status: Optional[Callable[[str], None]] = None,
    ) -> float:
        """Drive the SMU until Vds peak = target ±tolerance.

        Returns the final SMU setpoint. Raises PeakControlError when it cannot
        converge, SafetyTrip if a limit is hit along the way.
        """
        cfg = self.settings
        max_step = self.smu.ramp_step_v

        for iteration in range(cfg.max_iterations):
            if self._cancelled(cancel_check):
                raise PeakControlError("cancelled")

            peak = self._read_peak(cancel_check)
            # Current and compliance are independent of the scope read.  They
            # must still be checked when peak telemetry is unavailable.
            self._check_input_current()
            self.safety.check_compliance()
            if peak is None:
                self.safety.record_read_failure("scope peak voltage")
                self._wait(cfg.settle_s, cancel_check)
                continue
            self.safety.record_read_success("scope peak voltage")
            self.safety.check_sample(vds_peak=peak)

            error = target_v - peak
            if abs(error) <= cfg.tolerance_v:
                log.info(
                    "Peak control converged: Vds_pk %.1f V @ bus %.2f V (%d iterations)",
                    peak, self.smu.setpoint_v, iteration,
                )
                return self.smu.setpoint_v

            # Clamped proportional step, always smaller than the remaining error
            # scale so we approach the target without hunting.
            step = max(cfg.min_step_v, min(max_step, cfg.proportional_gain * abs(error)))
            new_setpoint = self.smu.setpoint_v + (step if error > 0 else -step)

            if status is not None:
                status(
                    f"Peak control: Vds_pk {peak:.1f} V / target {target_v:.0f} V, "
                    f"bus → {new_setpoint:.1f} V"
                )
            try:
                self.smu.ramp_to(
                    max(0.0, new_setpoint),
                    cancel_check=cancel_check,
                )
            except SmuLimitError as exc:
                raise PeakControlError(
                    f"SMU limit reached before hitting {target_v:.0f} V peak: {exc}"
                ) from exc
            self._wait(cfg.settle_s, cancel_check)

        raise PeakControlError(
            f"Vds peak did not converge to {target_v:.0f} V "
            f"within {cfg.max_iterations} iterations"
        )
