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
from typing import Callable, Optional

from gan_fet.core.control_loop import finite_float, is_cancelled, settle
from gan_fet.core.events import MeasurementEvent, bus
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
        return is_cancelled(cancel_check)

    def _wait(
        self,
        seconds: float,
        cancel_check: Optional[Callable[[], bool]],
    ) -> None:
        """Settle, converting an interrupted wait into this loop's error."""
        if not settle(seconds, cancel_check):
            raise PeakControlError("cancelled")

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
        numeric_peak = finite_float(peak)
        if numeric_peak is None and peak is not None:
            log.warning("Scope returned an unusable peak reading: %r", peak)
        return numeric_peak

    def _publish_measurement(
        self, peak: float, current: Optional[float]
    ) -> None:
        """Offer the reading to whatever is watching, and never fail for it.

        ``EventBus.publish`` already isolates each subscriber, so a broken
        listener cannot reach here. What this guard is actually for is the
        argument list: it reads ``smu.setpoint_v``, which talks to an
        instrument that can be gone. The bus ramp is the longest stretch in
        which the rig is live, and showing it must not be able to stop it.
        """
        try:
            bus.publish(
                MeasurementEvent(
                    source="peak control",
                    bus_voltage=float(self.smu.setpoint_v),
                    vds_peak=peak,
                    dc_current=current,
                )
            )
        except Exception:  # pragma: no cover - defensive
            log.exception("Publishing a peak-control measurement failed")

    def _check_input_current(self) -> Optional[float]:
        """Enforce the independent DC-current ceiling during peak control.

        Returns the reading so it can be displayed, but the return value is
        incidental: this exists to check the ceiling, and it does that whether
        or not anyone is looking at the number.
        """
        try:
            current = self.smu.measure_dc_current()
        except Exception as exc:
            log.warning("SMU current query failed during peak control: %s", exc)
            current = None
        numeric_current = finite_float(current)
        if numeric_current is None:
            self.safety.record_read_failure("SMU current (peak control)")
        else:
            self.safety.record_read_success("SMU current (peak control)")
            self.safety.check_sample(dc_current=numeric_current)
        return numeric_current

    def _gain_estimate(
        self,
        history: Optional[tuple[float, float]],
        setpoint: float,
        peak: float,
    ) -> Optional[float]:
        """Local ``dVds_peak/dV_bus``, or ``None`` when it cannot be trusted.

        Prefers the secant slope between the last two points. Falls back to the
        chord through the origin, which is available from the very first
        reading — the step *before* any history exists is exactly the one that
        can overshoot, so some estimate is needed immediately.
        """
        cfg = self.settings
        if history is not None:
            previous_setpoint, previous_peak = history
            delta_v = setpoint - previous_setpoint
            if abs(delta_v) >= cfg.secant_min_delta_v:
                gain = (peak - previous_peak) / delta_v
                if math.isfinite(gain) and gain > 0.0:
                    return gain
        if setpoint > cfg.secant_min_delta_v and peak > 0.0:
            chord = peak / setpoint
            if math.isfinite(chord) and chord > 0.0:
                return chord
        return None

    def _secant_step_v(
        self,
        error: float,
        history: Optional[tuple[float, float]],
        setpoint: float,
        peak: float,
        max_step: float,
    ) -> float:
        """Signed bus correction from a local gain estimate.

        Loop gain ``dVds_peak/dV_bus`` varies by an order of magnitude across
        the resonance curve, so a fixed proportional gain either crawls or
        hunts. The secant estimate adapts to whatever the local slope is, and
        falls back to proportional control whenever that estimate cannot be
        trusted: too small a bus separation (mostly measurement noise), a
        non-positive slope, or no history yet.
        """
        cfg = self.settings
        proportional = max(
            cfg.min_step_v, min(max_step, cfg.proportional_gain * abs(error))
        )
        if history is not None:
            previous_setpoint, previous_peak = history
            delta_v = setpoint - previous_setpoint
            delta_peak = peak - previous_peak
            if abs(delta_v) >= cfg.secant_min_delta_v:
                gain = delta_peak / delta_v
                # A non-positive slope means the plant is not responding the
                # way the secant assumes; proportional control is safer there.
                if gain > 0.0 and math.isfinite(gain):
                    magnitude = abs(error) / gain
                    if math.isfinite(magnitude) and magnitude > 0.0:
                        bounded = min(
                            magnitude, cfg.secant_max_step_v, max_step
                        )
                        return math.copysign(
                            max(cfg.min_step_v, bounded), error
                        )
        return math.copysign(proportional, error)

    def achieve_peak(
        self,
        target_v: float,
        *,
        cancel_check: Optional[Callable[[], bool]] = None,
        status: Optional[Callable[[str], None]] = None,
        max_setpoint_v: Optional[float] = None,
        peak_ceiling_v: Optional[float] = None,
    ) -> float:
        """Drive the SMU until Vds peak = target ±tolerance.

        ``max_setpoint_v`` bounds what the loop may command. Callers use it to
        make an over-voltage operating point *unreachable* rather than relying
        on detecting one after the fact: tank gain can rise faster between
        iterations than polling can follow. Hitting the bound raises
        PeakControlError, which callers treat as "not reachable here" rather
        than as a failure.

        Returns the final SMU setpoint. Raises PeakControlError when it cannot
        converge, SafetyTrip if a limit is hit along the way.
        """
        cfg = self.settings
        max_step = self.smu.ramp_step_v
        history: Optional[tuple[float, float]] = None
        ceiling_v = self.smu.max_voltage_v
        if max_setpoint_v is not None:
            ceiling_v = min(ceiling_v, max(0.0, max_setpoint_v))

        for iteration in range(cfg.max_iterations):
            if self._cancelled(cancel_check):
                raise PeakControlError("cancelled")

            peak = self._read_peak(cancel_check)
            # Current and compliance are independent of the scope read.  They
            # must still be checked when peak telemetry is unavailable.
            current = self._check_input_current()
            self.safety.check_compliance()
            if peak is None:
                self.safety.record_read_failure("scope peak voltage")
                self._wait(cfg.settle_s, cancel_check)
                continue
            self.safety.record_read_success("scope peak voltage")
            self.safety.check_sample(vds_peak=peak)
            # After the safety checks, never before: a reading about to trip
            # the rig belongs to the interlock first.
            self._publish_measurement(peak, current)

            error = target_v - peak
            if abs(error) <= cfg.tolerance_v:
                log.info(
                    "Peak control converged: Vds_pk %.1f V @ bus %.2f V (%d iterations)",
                    peak, self.smu.setpoint_v, iteration,
                )
                return self.smu.setpoint_v

            setpoint = self.smu.setpoint_v
            step = self._secant_step_v(error, history, setpoint, peak, max_step)

            # Predictive ceiling guard. Near resonance one bus step can move
            # the peak by tens of volts, so a step is only safe if the peak it
            # is predicted to produce stays below the ceiling. Checking after
            # the fact would already have applied the excursion.
            if peak_ceiling_v is not None and step > 0.0:
                gain = self._gain_estimate(history, setpoint, peak)
                headroom = peak_ceiling_v - peak
                if headroom <= 0.0:
                    raise PeakControlError(
                        f"Vds peak {peak:.1f} V is already at the "
                        f"{peak_ceiling_v:.1f} V search ceiling"
                    )
                if gain is not None:
                    step = min(step, headroom / gain)

            history = (setpoint, peak)
            new_setpoint = setpoint + step
            if new_setpoint > ceiling_v:
                if setpoint >= ceiling_v - 1e-9:
                    raise PeakControlError(
                        f"Vds peak {peak:.1f} V needs more bus than the "
                        f"{ceiling_v:.1f} V reachability cap allows"
                    )
                new_setpoint = ceiling_v

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
