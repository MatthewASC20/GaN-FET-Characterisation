"""Wavegen state tracking and frequency autotune.

The rig's resonant point drifts between builds, so a previously measured
switching frequency (stored on completed runs) is a better starting point
than the nominal one. Search order for a prior tuned frequency, unchanged
from v1: same temperature + same config first, then same temperature +
other configs, then the 25 °C equivalents.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Callable, Optional

from gan_fet.core.models import MatrixPoint
from gan_fet.instruments.base import OscilloscopeInterface, WavegenInterface
from gan_fet.settings import WavegenSettings
from gan_fet.storage.db import Database

log = logging.getLogger(__name__)

#: Vds peak is polled at least this often during a frequency ramp, counted in
#: driver steps rather than seconds. Risk scales with how far the frequency has
#: moved, not with elapsed time: a time-based throttle alone is starved by a
#: fast ramp, which is exactly when the guard is needed most.
PEAK_POLL_STEPS = 5
#: A wall-clock bound as well, so a slow ramp is still watched frequently.
PEAK_POLL_INTERVAL_S = 0.05


class FrequencyRampAborted(RuntimeError):
    """A frequency ramp was stopped because Vds peak approached the ceiling.

    Distinct from a safety trip: the ramp is halted *before* the interlock
    would fire, so the rig stays energised and under operator control.
    """

    def __init__(self, peak_v: float, ceiling_v: float, frequency_hz: float):
        super().__init__(
            f"frequency ramp stopped at {frequency_hz / 1e6:.4f} MHz: "
            f"Vds peak {peak_v:.1f} V reached the {ceiling_v:.1f} V ramp ceiling"
        )
        self.peak_v = peak_v
        self.ceiling_v = ceiling_v
        self.frequency_hz = frequency_hz

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

    def __init__(
        self,
        wavegen: WavegenInterface,
        settings: Optional[WavegenSettings] = None,
        *,
        scope: Optional[OscilloscopeInterface] = None,
        peak_ceiling_v: Optional[float] = None,
    ):
        self.wavegen = wavegen
        self.settings = settings or WavegenSettings()
        # Optional Vds peak guard. Moving the gate frequency shifts the
        # resonant operating point, so with the bus live the peak can climb
        # with nothing controlling it — this ramp has no closed loop behind it,
        # unlike the in-run frequency search. When a scope and ceiling are
        # supplied the ramp is halted before the interlock would trip.
        self.scope = scope
        self.peak_ceiling_v = peak_ceiling_v
        # State reads are short and never encompass transport I/O.  The
        # operation lock serializes compound hardware changes without causing
        # the nested state-lock deadlocks that the previous implementation had.
        self._state_lock = threading.Lock()
        self._operation_lock = threading.RLock()
        self.applied_config: Optional[str] = None
        self.applied_freq_hz: Optional[float] = None   # nominal selection
        self.applied_duty: Optional[float] = None
        self.tuned_freq_hz: Optional[float] = None     # actual (possibly tuned)

    def _peak_guard(
        self,
        cancel_check: Optional[Callable[[], bool]],
        breach: list[float],
    ) -> Optional[Callable[[], bool]]:
        """Wrap ``cancel_check`` so the ramp also stops on a peak excursion.

        The driver already consults a cancel check on every step, which makes
        it the natural place to hook monitoring without changing the transport
        layer. Reads are throttled: the driver steps far more often than the
        tank can respond, and a scope query per 10 kHz would dominate the ramp.

        A failed read is *not* treated as safe-to-continue by this guard alone;
        it is left to the safety monitor's read-failure watchdog, which already
        trips after repeated failures.
        """
        ceiling = self.peak_ceiling_v
        if self.scope is None or ceiling is None or ceiling <= 0.0:
            return cancel_check

        last_poll = 0.0
        steps_since_poll = PEAK_POLL_STEPS

        def guarded() -> bool:
            nonlocal last_poll, steps_since_poll
            if cancel_check is not None and cancel_check():
                return True
            steps_since_poll += 1
            now = time.monotonic()
            due = (
                steps_since_poll >= PEAK_POLL_STEPS
                or now - last_poll >= PEAK_POLL_INTERVAL_S
            )
            if not due:
                return False
            last_poll = now
            steps_since_poll = 0
            try:
                raw = self.scope.peak_voltage()  # type: ignore[union-attr]
            except Exception as exc:
                log.warning("peak read failed during frequency ramp: %s", exc)
                return False
            if raw is None:
                return False
            try:
                peak = float(raw)
            except (TypeError, ValueError):
                return False
            if not math.isfinite(peak):
                return False
            if peak > ceiling:
                breach.append(peak)
                return True
            return False

        return guarded

    def has_pending_changes(self, config: str, freq_hz: int, duty: int) -> bool:
        with self._state_lock:
            if self.applied_config is None:
                return True
            return (
                config != self.applied_config
                or freq_hz != self.applied_freq_hz
                or duty != self.applied_duty
            )

    def invalidate_tuning(self) -> None:
        with self._state_lock:
            self.tuned_freq_hz = None

    @property
    def outputs_armed(self) -> bool:
        return self.wavegen.outputs_armed

    def arm_outputs(self, config: Optional[str] = None) -> bool:
        """Arm the channel set for ``config`` after configuration is complete."""
        with self._operation_lock:
            if config is None:
                with self._state_lock:
                    config = self.applied_config
            if config is None:
                raise RuntimeError("Cannot arm wavegen before a configuration is applied")
            return self.wavegen.arm_outputs(config)

    def disarm_outputs(self) -> None:
        """Synchronously turn off both gate outputs."""
        with self._operation_lock:
            self.wavegen.outputs_off()

    def apply(
        self,
        config: str,
        freq_hz: int,
        duty: int,
        duty_rate_pct_s: Optional[float] = None,
        freq_rate_khz_s: Optional[float] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        status: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Apply selection to the instrument (full reconfigure on config
        change, rate-dependent gradual ramping for parameter adjustments)."""
        with self._operation_lock:
            if cancel_check is not None and cancel_check():
                return
            with self._state_lock:
                applied_config = self.applied_config
                applied_duty = self.applied_duty
                cached_actual_freq = (
                    self.tuned_freq_hz
                    if self.tuned_freq_hz is not None
                    else self.applied_freq_hz
                )

            dual = self.wavegen.is_dual(config)
            if config != applied_config:
                self.wavegen.configure(config, freq_hz, duty)
                with self._state_lock:
                    self.applied_config = config
                    self.applied_freq_hz = float(freq_hz)
                    self.applied_duty = float(duty)
                    self.tuned_freq_hz = float(freq_hz)
                return

            # Autotune deliberately moves away from the nominal selection.
            # Read the instrument before every same-config apply so the next
            # sequence point is restored to nominal even when its selected
            # frequency did not change.
            actual_before = self.wavegen.read_frequency()
            if actual_before is None:
                actual_before = cached_actual_freq
            if actual_before is None or abs(freq_hz - actual_before) >= 1.0:
                actual_freq = self.wavegen.ramp_to_frequency(
                    freq_hz,
                    dual,
                    rate_khz_s=(
                        freq_rate_khz_s
                        if freq_rate_khz_s is not None
                        else self.settings.freq_ramp_rate_khz_s
                    ),
                    cancel_check=cancel_check,
                )
                with self._state_lock:
                    self.applied_freq_hz = (
                        float(freq_hz)
                        if abs(actual_freq - freq_hz) < 1.0
                        else None
                    )
                    self.tuned_freq_hz = actual_freq
                if status is not None:
                    status(f"Frequency Ramp: {actual_freq / 1e6:.3f} MHz")
            else:
                with self._state_lock:
                    self.applied_freq_hz = float(freq_hz)
                    self.tuned_freq_hz = float(actual_before)

            if cancel_check is not None and cancel_check():
                return

            if duty != applied_duty:
                actual_duty = self.wavegen.ramp_to_duty(
                    duty,
                    dual,
                    rate_pct_s=(
                        duty_rate_pct_s
                        if duty_rate_pct_s is not None
                        else self.settings.duty_ramp_rate_pct_s
                    ),
                    cancel_check=cancel_check,
                )
                with self._state_lock:
                    self.applied_duty = actual_duty
                if status is not None:
                    status(f"Duty Ramp: {actual_duty:.1f}%")

    def ramp_to_duty(
        self,
        target_duty: float,
        config: str,
        *,
        rate_pct_s: Optional[float] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        status: Optional[Callable[[str], None]] = None,
    ) -> float:
        """Step duty cycle gradually and return the last applied value."""
        with self._operation_lock:
            dual = self.wavegen.is_dual(config)
            with self._state_lock:
                start = float(
                    self.applied_duty
                    if self.applied_duty is not None
                    else target_duty
                )
            actual = self.wavegen.ramp_to_duty(
                target_duty,
                dual,
                rate_pct_s=(
                    rate_pct_s
                    if rate_pct_s is not None
                    else self.settings.duty_ramp_rate_pct_s
                ),
                cancel_check=cancel_check,
            )
            with self._state_lock:
                self.applied_duty = actual
            if status is not None:
                status(f"Duty Ramp: {actual:.1f}%")
            log.info("Duty ramp complete: %.1f%% → %.1f%%", start, actual)
            return actual

    def ramp_to_frequency(
        self,
        target_hz: float,
        config: str,
        *,
        rate_khz_s: Optional[float] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        status: Optional[Callable[[str], None]] = None,
    ) -> float:
        """Step the output frequency gradually to target_hz (resonant rigs
        dislike jumps). Returns the start frequency."""
        with self._operation_lock:
            dual = self.wavegen.is_dual(config)

            start = self.wavegen.read_frequency()
            if start is None:
                with self._state_lock:
                    start = self.tuned_freq_hz or self.applied_freq_hz
            if start is None:
                raise RuntimeError("Cannot determine current wavegen frequency")

            breach: list[float] = []
            actual = self.wavegen.ramp_to_frequency(
                target_hz,
                dual,
                rate_khz_s=(
                    rate_khz_s
                    if rate_khz_s is not None
                    else self.settings.freq_ramp_rate_khz_s
                ),
                cancel_check=self._peak_guard(cancel_check, breach),
            )
            # The ramp stops where it is rather than continuing to target, so
            # the recorded tuned frequency must be what was actually applied.
            with self._state_lock:
                self.tuned_freq_hz = actual
            if breach:
                log.error(
                    "Frequency ramp aborted at %.4f MHz: Vds peak %.1f V "
                    "reached the %.1f V ramp ceiling",
                    actual / 1e6,
                    breach[0],
                    self.peak_ceiling_v or 0.0,
                )
                raise FrequencyRampAborted(
                    breach[0], self.peak_ceiling_v or 0.0, float(actual)
                )
            if status is not None:
                status(f"Gate frequency: {actual / 1e6:.2f} MHz")
            log.info("Autotune ramp complete: %.0f Hz → %.0f Hz", start, actual)
            return float(start)
