"""Closed-loop Vds peak control.

The selected test "voltage" (200/300/400 V) is the scope-measured Vds peak,
not the bus voltage. v1 asked the operator to twiddle a bench supply until
the peak matched (±2 V) and then merely validated it. With the SMU as the
bus source, this controller closes the loop automatically: adjust the SMU
setpoint, wait, read MEAS6, repeat until |target - peak| <= tolerance.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from gan_fet.core.safety import SafetyMonitor
from gan_fet.instruments.oscilloscope import Mso44
from gan_fet.instruments.smu import Keithley2400, SmuLimitError
from gan_fet.settings import PeakControlSettings

log = logging.getLogger(__name__)


class PeakControlError(RuntimeError):
    pass


class PeakVoltageController:
    def __init__(
        self,
        smu: Keithley2400,
        scope: Mso44,
        settings: PeakControlSettings,
        safety: SafetyMonitor,
    ):
        self.smu = smu
        self.scope = scope
        self.settings = settings
        self.safety = safety

    def _read_peak(self) -> Optional[float]:
        peak = self.scope.peak_voltage()
        if peak is None:
            # Stale/failed read — retry once after a short delay (v1 behaviour).
            time.sleep(0.3)
            peak = self.scope.peak_voltage()
        return peak

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
        max_step = self.smu.settings.ramp_step_v

        for iteration in range(cfg.max_iterations):
            if cancel_check is not None and cancel_check():
                raise PeakControlError("cancelled")

            peak = self._read_peak()
            if peak is None:
                self.safety.record_read_failure("scope peak voltage")
                time.sleep(cfg.settle_s)
                continue
            self.safety.record_read_success()
            self.safety.check_sample(vds_peak=peak)
            self.safety.check_compliance()

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
                self.smu.set_voltage(max(0.0, new_setpoint))
            except SmuLimitError as exc:
                raise PeakControlError(
                    f"SMU limit reached before hitting {target_v:.0f} V peak: {exc}"
                ) from exc
            time.sleep(cfg.settle_s)

        raise PeakControlError(
            f"Vds peak did not converge to {target_v:.0f} V "
            f"within {cfg.max_iterations} iterations"
        )
