"""Gate-frequency search for minimum input power at a held Vds peak.

The tank is built by choosing L and C to land near a nominal frequency, but the
real resonance differs from nominal and moves during the voltage sweep, because
GaN output capacitance is bus-voltage dependent. The nominal gate frequency is
therefore rarely the one that minimises losses at any given operating point.

Two things shape the design.

**The objective is input power, not input current.** The bus voltage varies
along the constant-peak curve, so with ``P_in = V_bus * I_dc``::

    dP_in/df = V_bus * dI_dc/df + I_dc * dV_bus/df

At the DC-current minimum the first term vanishes but the second does not.
Minimising current therefore biases towards higher-bus operating points that
can draw more total power.

**The objective is not unimodal.** Circulating current peaks at resonance while
switching loss falls towards it, so measured ``P_in(f)`` has more than one local
minimum. Any local descent finds whichever basin it started in, so the window is
swept for coverage and every minimum is recorded.

Frequency is the outer variable; the existing peak controller is the inner loop,
so every evaluated point is measured on target rather than interpolated.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from gan_fet.core.safety import SafetyMonitor
from gan_fet.core.voltage_control import PeakControlError, PeakVoltageController
from gan_fet.instruments.base import (
    OscilloscopeInterface,
    SmuInterface,
    WavegenInterface,
)
from gan_fet.settings import FrequencyTuneSettings, SafetySettings

log = logging.getLogger(__name__)


class FrequencyTuneError(RuntimeError):
    """The frequency search could not produce a usable result."""


@dataclass
class TunePoint:
    """One evaluated frequency, measured on target."""

    frequency_hz: float
    bus_voltage_v: float
    vds_peak_v: float
    dc_current_a: float
    reachable: bool = True
    anomaly: bool = False
    #: Peak seen on arrival at this frequency, before the bus was re-converged.
    #: The excursion it represents is what drives the adaptive step size.
    arrival_peak_v: Optional[float] = None

    @property
    def input_power_w(self) -> float:
        return self.bus_voltage_v * self.dc_current_a


@dataclass
class TuneResult:
    frequency_hz: float
    input_power_w: float
    bus_voltage_v: float
    direction: str
    points: list[TunePoint] = field(default_factory=list)
    minima_hz: list[float] = field(default_factory=list)
    survey_resonance_hz: Optional[float] = None
    clipped_at_edge: bool = False
    anomalies: int = 0


class FrequencyTuner:
    """Coarse-then-fine search for the gate frequency minimising ``P_in``."""

    def __init__(
        self,
        wavegen: WavegenInterface,
        smu: SmuInterface,
        scope: OscilloscopeInterface,
        peak_controller: PeakVoltageController,
        settings: FrequencyTuneSettings,
        safety_settings: SafetySettings,
        safety: SafetyMonitor,
    ):
        self.wavegen = wavegen
        self.smu = smu
        self.scope = scope
        self.peak_controller = peak_controller
        self.settings = settings
        self.safety_settings = safety_settings
        self.safety = safety
        # Gain here and now, which sets the running bus cap; and the largest
        # seen, kept only for reporting.
        self._observed_gain = 0.0
        self._observed_max_gain = 0.0
        #: Gain at each converged point, used to extrapolate one step ahead.
        self._gain_history: list[float] = []

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _cancelled(cancel_check: Optional[Callable[[], bool]]) -> bool:
        return cancel_check is not None and cancel_check()

    @staticmethod
    def _finite(value: object) -> Optional[float]:
        try:
            numeric = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return numeric if math.isfinite(numeric) else None

    def _wait(
        self, seconds: float, cancel_check: Optional[Callable[[], bool]]
    ) -> None:
        """Settle in short slices so cancellation is not hidden by the wait."""
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            if self._cancelled(cancel_check):
                raise FrequencyTuneError("cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.05, remaining))

    def _read_peak(self) -> Optional[float]:
        try:
            return self._finite(self.scope.peak_voltage())
        except Exception as exc:
            log.warning("scope peak read failed during frequency tune: %s", exc)
            return None

    def _read_current(self) -> Optional[float]:
        try:
            return self._finite(self.smu.measure_dc_current())
        except Exception as exc:
            log.warning("SMU current read failed during frequency tune: %s", exc)
            return None

    def reachability_cap_v(self, small_signal_gain: float) -> float:
        """Largest bus voltage that cannot breach the ceiling if gain grows.

        Gain rises as the sweep approaches resonance, and it can rise faster
        between iterations than polling follows. Bounding the bus makes an
        over-voltage operating point unreachable rather than something to be
        detected after the fact.

        Note how little room there is at the top of the matrix: a 400 V target
        under a 450 V ceiling leaves only 12 % headroom, so
        ``gain_growth_factor`` must stay near unity or the target itself
        becomes unreachable. The protection comes from tracking the gain
        actually observed, not from a large blanket margin.
        """
        cfg = self.settings
        ceiling = self.safety_settings.max_vds_peak_v * cfg.ceiling_margin_frac
        gain = max(1e-6, small_signal_gain * cfg.gain_growth_factor)
        return max(0.0, ceiling / gain)

    def _note_gain(self, peak_v: Optional[float], bus_v: float) -> None:
        """Track the gain here and now.

        Deliberately the *latest* value, not the largest seen. Gain varies by
        several times across the window, so a cap sized for the highest-gain
        frequency would make perfectly safe lower-gain frequencies unreachable.
        The margin against a sudden rise comes from ``gain_growth_factor``, and
        gradual growth is handled by the adaptive step size.
        """
        if peak_v is None or bus_v <= 1e-6:
            return
        gain = peak_v / bus_v
        if math.isfinite(gain) and gain > 0.0:
            self._observed_gain = gain
            self._observed_max_gain = max(self._observed_max_gain, gain)

    @property
    def _peak_ceiling_v(self) -> float:
        """Search ceiling, held below the interlock so a sweep never trips it."""
        return (
            self.safety_settings.max_vds_peak_v
            * self.settings.ceiling_margin_frac
        )

    @property
    def _current_cap_v(self) -> Optional[float]:
        if self._observed_gain <= 0.0:
            return None
        return self.reachability_cap_v(self._observed_gain)

    def _predicted_next_gain(self) -> Optional[float]:
        """Extrapolate the gain one step ahead of where the sweep is now.

        Sizing the pre-step back-off from the *present* gain is always one step
        behind: the excursion happens on arrival, when the gain is already
        higher. Two consecutive gains give the growth per step, which is what
        the bus must be backed off against. Only upward growth is extrapolated
        — a falling gain needs no protection.
        """
        if len(self._gain_history) >= 2:
            previous, latest = self._gain_history[-2], self._gain_history[-1]
            if previous > 0.0:
                ratio = min(max(latest / previous, 1.0), 3.0)
                return latest * ratio
            return latest
        if self._gain_history:
            return self._gain_history[-1]
        return None

    def _prestep_cap_v(self) -> Optional[float]:
        predicted = self._predicted_next_gain()
        if predicted is None or predicted <= 0.0:
            return None
        return self.reachability_cap_v(predicted)

    def _prepare_for_jump(
        self, cancel_check: Optional[Callable[[], bool]]
    ) -> None:
        """Back the bus off before a discontinuous move in frequency.

        Step-to-step extrapolation is only valid for small steps. Starting a
        new sweep jumps from wherever the last one ended — possibly a low-gain
        edge of the window — to somewhere near resonance, where gain may be
        several times higher. With no basis for predicting the destination, the
        bus is reduced to what the *highest* gain seen anywhere would still
        keep under the ceiling. The first point of the sweep then converges
        back up under the normal guards.
        """
        if self._observed_max_gain <= 0.0:
            return
        safe_bus = self.reachability_cap_v(self._observed_max_gain)
        if float(self.smu.setpoint_v) > safe_bus:
            log.debug(
                "backing bus off to %.1f V before a frequency jump", safe_bus
            )
            self.smu.ramp_to(safe_bus, cancel_check=cancel_check)

    # -- stage 0: window selection ----------------------------------------

    def survey(
        self,
        nominal_hz: float,
        target_peak_v: float,
        config: str,
        *,
        cancel_check: Optional[Callable[[], bool]] = None,
        status: Optional[Callable[[str], None]] = None,
    ) -> tuple[float, float]:
        """Locate the small-signal resonance and gain at reduced amplitude.

        Run well below the working peak, where Coss varies little and the tank
        is close to linear: safe, quick (no bus convergence per point), and
        wide enough to find a bank that did not land near nominal.

        Returns ``(resonance_hz, small_signal_gain)``.
        """
        cfg = self.settings
        dual = self.wavegen.is_dual(config)
        span = nominal_hz * cfg.survey_window_frac
        low = max(cfg.survey_step_hz, nominal_hz - span)
        high = nominal_hz + span

        if status is not None:
            status(
                f"Survey: locating resonance across "
                f"{low / 1e6:.2f}–{high / 1e6:.2f} MHz"
            )

        # Establish the reduced working amplitude once, at nominal, then hold
        # the bus fixed for the sweep. Gain is what is being measured, so the
        # peak is deliberately allowed to vary with it.
        self.wavegen.ramp_to_frequency(nominal_hz, dual, cancel_check=cancel_check)
        self.safety.note_frequency(nominal_hz)
        survey_target = max(1.0, target_peak_v * cfg.survey_peak_frac)
        try:
            self.peak_controller.achieve_peak(
                survey_target,
                cancel_check=cancel_check,
                status=status,
                peak_ceiling_v=self._peak_ceiling_v,
            )
        except PeakControlError as exc:
            # Nominal may be far from resonance — that is the case the survey
            # exists to diagnose. Sweep from wherever the bus reached.
            log.info("survey could not reach its reduced peak at nominal: %s", exc)

        best_gain = 0.0
        best_hz = nominal_hz
        frequency = low
        points = 0
        while frequency <= high + 1.0 and points < cfg.max_points:
            if self._cancelled(cancel_check):
                raise FrequencyTuneError("cancelled during survey")
            self.wavegen.ramp_to_frequency(
                frequency, dual, cancel_check=cancel_check
            )
            self.safety.note_frequency(frequency)
            self._wait(cfg.settle_s, cancel_check)
            peak = self._read_peak()
            current = self._read_current()
            self.safety.check_sample(dc_current=current, vds_peak=peak)
            self.safety.check_compliance()
            bus = max(1e-6, float(self.smu.setpoint_v))
            if peak is not None:
                gain = peak / bus
                self._note_gain(peak, bus)
                if gain > best_gain:
                    best_gain, best_hz = gain, frequency
            frequency += cfg.survey_step_hz
            points += 1

        if best_gain <= 0.0:
            raise FrequencyTuneError(
                "Survey found no measurable tank response; check the scope "
                "measurement table and the gate drive"
            )

        offset = abs(best_hz - nominal_hz) / nominal_hz
        if offset > cfg.window_frac:
            log.warning(
                "Measured resonance %.3f MHz is %.0f%% from nominal %.3f MHz — "
                "the L/C bank is off design",
                best_hz / 1e6,
                offset * 100.0,
                nominal_hz / 1e6,
            )
        log.info(
            "Survey resonance %.4f MHz, small-signal gain %.2f",
            best_hz / 1e6,
            best_gain,
        )
        return best_hz, best_gain

    def select_window(
        self,
        nominal_hz: float,
        *,
        warm_start_hz: Optional[float] = None,
        survey_resonance_hz: Optional[float] = None,
    ) -> tuple[float, float]:
        """Return the working ``(low, high)`` frequency window.

        A survey measures the resonance at low amplitude, which sits *below*
        the working-amplitude one because Coss is larger at low Vds. The window
        is therefore biased upward from the survey peak rather than centred on
        it.
        """
        cfg = self.settings
        if warm_start_hz is not None and warm_start_hz > 0.0:
            return (
                max(1.0, warm_start_hz - cfg.warm_window_hz),
                warm_start_hz + cfg.warm_window_hz,
            )
        if survey_resonance_hz is not None and survey_resonance_hz > 0.0:
            centre = survey_resonance_hz * (1.0 + cfg.survey_upward_bias_frac)
        else:
            centre = nominal_hz
        span = centre * cfg.window_frac
        return max(1.0, centre - span), centre + span

    # -- stages 1 and 3: swept evaluation ---------------------------------

    def _evaluate(
        self,
        frequency_hz: float,
        target_peak_v: float,
        dual: bool,
        *,
        cancel_check: Optional[Callable[[], bool]],
        status: Optional[Callable[[str], None]],
    ) -> TunePoint:
        """Move to a frequency, restore the target peak, and measure there."""
        # Bring the bus within the cap *before* the frequency moves. Far from
        # resonance the gain is low, so the previous point may have left the
        # bus high; stepping towards resonance then raises the gain with that
        # bus still applied, and the peak excursion is far larger than the
        # allowance for smooth stepping. Backing off first makes the excursion
        # impossible rather than something to be caught afterwards.
        cap = self._prestep_cap_v()
        if cap is not None and float(self.smu.setpoint_v) > cap:
            self.smu.ramp_to(cap, cancel_check=cancel_check)

        self.wavegen.ramp_to_frequency(
            frequency_hz, dual, cancel_check=cancel_check
        )
        self.safety.note_frequency(frequency_hz)
        self._wait(self.settings.settle_s, cancel_check)

        # The peak on arrival, before re-converging. Its distance from target
        # is the excursion the step actually caused, and is what sizes the
        # next one.
        arrival_peak = self._read_peak()
        self._note_gain(arrival_peak, float(self.smu.setpoint_v))
        self.safety.check_sample(vds_peak=arrival_peak)

        reachable = True
        try:
            self.peak_controller.achieve_peak(
                target_peak_v,
                cancel_check=cancel_check,
                status=status,
                max_setpoint_v=self._current_cap_v,
                peak_ceiling_v=self._peak_ceiling_v,
            )
        except PeakControlError as exc:
            # Too little gain here to make the target within the cap. That is
            # a result about this frequency, not a failure of the search.
            log.info(
                "peak unreachable at %.4f MHz: %s", frequency_hz / 1e6, exc
            )
            reachable = False

        peak = self._read_peak()
        current = self._read_current()
        bus_now = float(self.smu.setpoint_v)
        self._note_gain(peak, bus_now)
        if peak is not None and bus_now > 1e-6:
            gain_here = peak / bus_now
            if math.isfinite(gain_here) and gain_here > 0.0:
                self._gain_history.append(gain_here)
        self.safety.check_sample(dc_current=current, vds_peak=peak)
        self.safety.check_compliance()
        return TunePoint(
            arrival_peak_v=arrival_peak,
            frequency_hz=frequency_hz,
            bus_voltage_v=float(self.smu.setpoint_v),
            vds_peak_v=peak if peak is not None else float("nan"),
            dc_current_a=current if current is not None else float("nan"),
            reachable=reachable,
        )

    def _flag_anomaly(self, points: list[TunePoint]) -> bool:
        """Mark a step whose change far exceeds the local trend.

        The design assumes the plant is effectively single-valued. This is how
        that assumption reports itself broken rather than silently corrupting a
        sweep, and it costs one comparison per point.
        """
        if len(points) < 3:
            return False
        previous, current = points[-2], points[-1]
        reference = points[-3]
        expected = abs(previous.vds_peak_v - reference.vds_peak_v)
        observed = abs(current.vds_peak_v - previous.vds_peak_v)
        if not math.isfinite(expected) or not math.isfinite(observed):
            return False
        threshold = max(
            self.settings.soft_band_v, expected * self.settings.anomaly_ratio
        )
        if observed > threshold:
            current.anomaly = True
            log.warning(
                "anomalous Vds peak step at %.4f MHz: %.1f V (expected ~%.1f V)",
                current.frequency_hz / 1e6,
                observed,
                expected,
            )
            return True
        return False

    def sweep(
        self,
        low_hz: float,
        high_hz: float,
        step_hz: float,
        target_peak_v: float,
        config: str,
        *,
        cancel_check: Optional[Callable[[], bool]] = None,
        status: Optional[Callable[[str], None]] = None,
    ) -> tuple[list[TunePoint], int]:
        """Sweep ascending, converging the peak at every frequency.

        The step is adaptive. Tank gain grows steeply on the approach to
        resonance, so a fixed step that is harmless far out can throw the peak
        tens of volts on arrival closer in. Each step is therefore sized from
        the excursion the previous one actually produced, holding the arrival
        peak inside the soft band. ``step_hz`` is the widest step allowed, not
        a fixed increment.
        """
        cfg = self.settings
        dual = self.wavegen.is_dual(config)
        points: list[TunePoint] = []
        anomalies = 0
        # Entering a sweep is a discontinuous jump from wherever the previous
        # one ended, so per-step extrapolation does not apply here.
        self._prepare_for_jump(cancel_check)
        self._gain_history.clear()
        frequency = low_hz
        step = step_hz
        min_step = max(1.0, step_hz / 16.0)
        while frequency <= high_hz + 1.0 and len(points) < cfg.max_points:
            if self._cancelled(cancel_check):
                raise FrequencyTuneError("cancelled during frequency sweep")
            if status is not None:
                status(
                    f"Frequency search: {frequency / 1e6:.3f} MHz "
                    f"({len(points) + 1} points)"
                )
            point = self._evaluate(
                frequency,
                target_peak_v,
                dual,
                cancel_check=cancel_check,
                status=None,
            )
            points.append(point)
            if self._flag_anomaly(points):
                anomalies += 1

            excursion = 0.0
            if point.arrival_peak_v is not None and math.isfinite(
                point.arrival_peak_v
            ):
                excursion = abs(point.arrival_peak_v - target_peak_v)
            if excursion > cfg.soft_band_v:
                # Overshot the allowance: shrink in proportion to the overshoot
                # so the next arrival lands inside it.
                step = max(min_step, step * cfg.soft_band_v / excursion)
            elif excursion < cfg.soft_band_v * 0.4:
                # Comfortably inside: widen again, but never past the ceiling
                # the caller asked for.
                step = min(step_hz, step * 1.5)
            frequency += step
        return points, anomalies

    # -- stage 2: minimum selection ---------------------------------------

    @staticmethod
    def local_minima(points: list[TunePoint]) -> list[int]:
        """Indices of every interior local minimum in ``P_in``.

        All of them are kept, not just the best: which minima exist is a
        property of the device and tank worth recording, and comparing them is
        the only way to know the chosen one is global rather than nearest.
        """
        usable = [
            index
            for index, point in enumerate(points)
            if point.reachable and math.isfinite(point.input_power_w)
        ]
        minima: list[int] = []
        for position, index in enumerate(usable):
            if position == 0 or position == len(usable) - 1:
                continue
            before = points[usable[position - 1]].input_power_w
            after = points[usable[position + 1]].input_power_w
            if points[index].input_power_w < before and (
                points[index].input_power_w < after
            ):
                minima.append(index)
        return minima

    @staticmethod
    def _best_index(points: list[TunePoint]) -> Optional[int]:
        best: Optional[int] = None
        for index, point in enumerate(points):
            if not point.reachable or not math.isfinite(point.input_power_w):
                continue
            if best is None or point.input_power_w < points[best].input_power_w:
                best = index
        return best

    # -- orchestration ------------------------------------------------------

    def find_minimum(
        self,
        nominal_hz: float,
        target_peak_v: float,
        config: str,
        *,
        warm_start_hz: Optional[float] = None,
        run_survey: bool = True,
        cancel_check: Optional[Callable[[], bool]] = None,
        status: Optional[Callable[[str], None]] = None,
    ) -> TuneResult:
        """Run the full search and leave the wavegen at the chosen frequency."""
        cfg = self.settings
        dual = self.wavegen.is_dual(config)

        survey_hz: Optional[float] = None
        if warm_start_hz is None and run_survey:
            survey_hz, _gain = self.survey(
                nominal_hz,
                target_peak_v,
                config,
                cancel_check=cancel_check,
                status=status,
            )

        low, high = self.select_window(
            nominal_hz,
            warm_start_hz=warm_start_hz,
            survey_resonance_hz=survey_hz,
        )
        coarse, anomalies = self.sweep(
            low,
            high,
            cfg.coarse_step_hz,
            target_peak_v,
            config,
            cancel_check=cancel_check,
            status=status,
        )
        best = self._best_index(coarse)
        if best is None:
            raise FrequencyTuneError(
                "Vds peak was unreachable across the entire window — the tank "
                "gain is too low everywhere, so resonance lies outside it"
            )

        minima = self.local_minima(coarse)
        clipped = best in (0, len(coarse) - 1)
        if clipped:
            log.warning(
                "best frequency %.4f MHz is at a window edge; the true "
                "minimum probably lies outside the searched window",
                coarse[best].frequency_hz / 1e6,
            )

        # Refine inside the winning basin. Unimodality holds locally even
        # though it does not across the window.
        centre = coarse[best].frequency_hz
        fine, fine_anomalies = self.sweep(
            max(1.0, centre - cfg.fine_span_hz),
            centre + cfg.fine_span_hz,
            cfg.fine_step_hz,
            target_peak_v,
            config,
            cancel_check=cancel_check,
            status=status,
        )
        anomalies += fine_anomalies

        combined = coarse + fine
        winner_index = self._best_index(combined)
        if winner_index is None:  # pragma: no cover - coarse already succeeded
            raise FrequencyTuneError("no usable frequency found during refinement")
        winner = combined[winner_index]

        # Returning to the winner is another discontinuous jump.
        self._prepare_for_jump(cancel_check)
        self.wavegen.ramp_to_frequency(
            winner.frequency_hz, dual, cancel_check=cancel_check
        )
        self.safety.note_frequency(winner.frequency_hz)

        # Size the cap from the gain measured *at the winner*. The running
        # value belongs to wherever the fine sweep happened to finish, and on
        # the low side of resonance gain falls as amplitude rises — so a cap
        # taken elsewhere can forbid a bus the winner demonstrably reached.
        settle_cap = self._current_cap_v
        if winner.bus_voltage_v > 1e-6 and math.isfinite(winner.vds_peak_v):
            winner_gain = winner.vds_peak_v / winner.bus_voltage_v
            if math.isfinite(winner_gain) and winner_gain > 0.0:
                settle_cap = self.reachability_cap_v(winner_gain)
        try:
            self.peak_controller.achieve_peak(
                target_peak_v,
                cancel_check=cancel_check,
                status=status,
                max_setpoint_v=settle_cap,
                peak_ceiling_v=self._peak_ceiling_v,
            )
        except PeakControlError as exc:
            # The point was measured as reachable during the sweep, so failing
            # to return to it is a real anomaly rather than a routine outcome.
            raise FrequencyTuneError(
                f"could not return to the chosen frequency "
                f"{winner.frequency_hz / 1e6:.4f} MHz: {exc}"
            ) from exc

        log.info(
            "Frequency search: %.4f MHz, P_in %.3f W at bus %.1f V "
            "(%d minima, %d anomalies)",
            winner.frequency_hz / 1e6,
            winner.input_power_w,
            winner.bus_voltage_v,
            len(minima),
            anomalies,
        )
        return TuneResult(
            frequency_hz=winner.frequency_hz,
            input_power_w=winner.input_power_w,
            bus_voltage_v=float(self.smu.setpoint_v),
            direction="ascending",
            points=combined,
            minima_hz=[coarse[index].frequency_hz for index in minima],
            survey_resonance_hz=survey_hz,
            clipped_at_edge=clipped,
            anomalies=anomalies,
        )
