"""Frequency-search regressions against the simulated resonant plant.

The plant is not a circuit model. Most of these tests assert control behaviour
and safety ordering — that the search covers its window, compares every minimum
rather than descending into the nearest one, holds Vds peak on target, honours
cancellation, and never reaches the hard ceiling. Which *exact* frequency it
picks remains a bench question, not a test question.

One physical relationship is asserted, at the end of the file: minimum input
power coincides with ZVS. That is not a modelling preference but the operator's
measured experience of this rig, and the earlier model contradicted it — which
let the search minimise its objective perfectly while landing somewhere with no
ZVS at all, and let every simulated run report success while doing it. Anything
the simulator is allowed to get wrong about the objective, it will.
"""

from __future__ import annotations

import math

import pytest

from gan_fet.core.frequency_tune import FrequencyTuneError, FrequencyTuner, TunePoint
from gan_fet.core.safety import SafetyMonitor
from gan_fet.core.voltage_control import PeakVoltageController
from gan_fet.instruments.mock_scpi import SimulatedRigPlant
from gan_fet.settings import (
    FrequencyTuneSettings,
    PeakControlSettings,
    SafetySettings,
)


class _Scope:
    def __init__(self, plant: SimulatedRigPlant) -> None:
        self.plant = plant

    def peak_voltage(self):
        return self.plant.vds_peak_v()

    def zvs_dwell_fraction(self):
        return self.plant.zvs_dwell_fraction()


class _Smu:
    """Minimal SMU standing in for the driver, backed by the shared plant."""

    def __init__(self, plant: SimulatedRigPlant, max_voltage_v: float = 600.0) -> None:
        self.plant = plant
        self._max = max_voltage_v
        self.ramp_step_v = 5.0
        self.peak_commanded_v = 0.0

    @property
    def setpoint_v(self) -> float:
        return self.plant.smu_voltage_setpoint_v

    @property
    def max_voltage_v(self) -> float:
        return self._max

    def ramp_to(self, volts: float, *, cancel_check=None) -> None:
        target = max(0.0, min(self._max, float(volts)))
        self.plant.smu_voltage_setpoint_v = target
        self.peak_commanded_v = max(self.peak_commanded_v, target)

    def measure_dc_current(self):
        return self.plant.dc_current_a()

    def compliance_tripped(self):
        return False

    def emergency_off(self) -> bool:
        self.plant.smu_output_on = False
        return True


class _Wavegen:
    def __init__(self, plant: SimulatedRigPlant) -> None:
        self.plant = plant
        self.outputs_armed = True
        self.frequencies: list[float] = []

    @staticmethod
    def is_dual(config: str) -> bool:
        return "Dual" in config

    def ramp_to_frequency(
        self, target_freq_hz: float, dual: bool, *, rate_khz_s=None, cancel_check=None
    ) -> float:
        self.plant.wavegen_frequency_hz = float(target_freq_hz)
        self.frequencies.append(float(target_freq_hz))
        return float(target_freq_hz)

    def outputs_off(self) -> None:
        self.plant.wavegen_c1_on = False
        self.plant.wavegen_c2_on = False
        self.outputs_armed = False


class _Db:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def add_safety_event(self, run_id, kind, detail, context=None) -> None:
        self.events.append((run_id, kind, detail, context))


def _build(plant: SimulatedRigPlant, **tune_kwargs):
    smu = _Smu(plant)
    scope = _Scope(plant)
    wavegen = _Wavegen(plant)
    safety_settings = SafetySettings(max_dc_current_a=1.0, max_vds_peak_v=450.0)
    safety = SafetyMonitor(safety_settings, smu, wavegen, _Db())
    peak = PeakVoltageController(
        smu,
        scope,
        PeakControlSettings(settle_s=0.0, tolerance_v=2.0, max_iterations=80),
        safety,
    )
    settings = FrequencyTuneSettings(settle_s=0.0, **tune_kwargs)
    tuner = FrequencyTuner(
        wavegen, smu, scope, peak, settings, safety_settings, safety
    )
    return tuner, smu, wavegen, safety


@pytest.fixture()
def plant() -> SimulatedRigPlant:
    p = SimulatedRigPlant(resonant_model=True)
    p.wavegen_frequency_hz = 6_000_000.0
    p.wavegen_c1_on = True
    p.latch_tank_nominal()
    p.smu_output_on = True
    return p


def test_local_minima_reports_every_basin_not_just_the_best():
    points = [
        TunePoint(1.0, 10.0, 200.0, 1.0),   # P = 10
        TunePoint(2.0, 10.0, 200.0, 0.4),   # P = 4   <- minimum
        TunePoint(3.0, 10.0, 200.0, 0.9),   # P = 9
        TunePoint(4.0, 10.0, 200.0, 0.2),   # P = 2   <- deeper minimum
        TunePoint(5.0, 10.0, 200.0, 0.8),   # P = 8
    ]
    assert FrequencyTuner.local_minima(points) == [1, 3]


def test_unreachable_points_are_excluded_from_minimum_selection():
    points = [
        TunePoint(1.0, 10.0, 200.0, 1.0),
        TunePoint(2.0, 1.0, 5.0, 0.01, reachable=False),  # cheapest, but invalid
        TunePoint(3.0, 10.0, 200.0, 0.5),
        TunePoint(4.0, 10.0, 200.0, 0.9),
    ]
    assert FrequencyTuner.local_minima(points) == [2]


def test_search_finds_the_global_minimum_not_the_nearest(plant):
    """The plant has two basins; a local descent from nominal finds the wrong one."""
    tuner, _smu, _wavegen, _safety = _build(plant)
    result = tuner.find_minimum(
        6_000_000.0, 200.0, "Single Device", run_survey=False
    )

    assert len(result.minima_hz) >= 2, "plant should present more than one basin"
    best = min(
        (p for p in result.points if p.reachable),
        key=lambda p: p.input_power_w,
    )
    assert result.frequency_hz == pytest.approx(best.frequency_hz)
    # Every recorded minimum must be at least as costly as the chosen one.
    chosen = result.input_power_w
    for point in result.points:
        if point.reachable and math.isfinite(point.input_power_w):
            assert point.input_power_w >= chosen - 1e-9


def test_every_evaluated_point_is_measured_on_target(plant):
    tuner, _smu, _wavegen, _safety = _build(plant)
    result = tuner.find_minimum(
        6_000_000.0, 200.0, "Single Device", run_survey=False
    )
    for point in result.points:
        if point.reachable:
            assert point.vds_peak_v == pytest.approx(200.0, abs=2.5)


def test_search_never_reaches_the_hard_ceiling(plant):
    tuner, _smu, _wavegen, safety = _build(plant)
    result = tuner.find_minimum(
        6_000_000.0, 400.0, "Single Device", run_survey=False
    )
    assert not safety.is_tripped
    for point in result.points:
        if math.isfinite(point.vds_peak_v):
            assert point.vds_peak_v < 450.0
    assert result.frequency_hz > 0


def test_reachability_cap_keeps_a_gain_excursion_below_the_ceiling(plant):
    tuner, _smu, _wavegen, _safety = _build(plant)
    cap = tuner.guard.cap_for(4.0)
    # Even if gain doubled beyond the measured small-signal value, the peak
    # stays under the interlock: that is the point of bounding reachability
    # rather than trying to react to a transition faster than it happens.
    worst_case_peak = cap * 4.0 * tuner.guard.growth_factor
    assert worst_case_peak <= 450.0


def test_cancellation_stops_the_sweep_promptly(plant):
    tuner, _smu, wavegen, _safety = _build(plant)
    calls = {"n": 0}

    def cancel_after_a_few() -> bool:
        calls["n"] += 1
        return calls["n"] > 12

    with pytest.raises(FrequencyTuneError):
        tuner.find_minimum(
            6_000_000.0,
            200.0,
            "Single Device",
            run_survey=False,
            cancel_check=cancel_after_a_few,
        )
    # It stopped early rather than completing the window.
    assert len(wavegen.frequencies) < 25


def test_window_is_biased_upward_from_the_survey_resonance(plant):
    """Small-signal resonance sits below the working one, so centring on it
    would search the wrong side."""
    tuner, _smu, _wavegen, _safety = _build(plant)
    low, high = tuner.select_window(6_000_000.0, survey_resonance_hz=6_000_000.0)
    centre = (low + high) / 2.0
    assert centre > 6_000_000.0


def test_warm_start_narrows_the_window(plant):
    tuner, _smu, _wavegen, _safety = _build(plant)
    wide_low, wide_high = tuner.select_window(6_000_000.0)
    warm_low, warm_high = tuner.select_window(
        6_000_000.0, warm_start_hz=6_500_000.0
    )
    assert (warm_high - warm_low) < (wide_high - wide_low)


def test_survey_locates_a_resonance_away_from_nominal(plant):
    """The bank deliberately does not land on nominal — that is why the survey
    exists."""
    tuner, _smu, _wavegen, _safety = _build(plant)
    resonance, gain = tuner.survey(6_000_000.0, 200.0, "Single Device")
    assert gain > 0
    assert resonance == pytest.approx(6_390_000.0, rel=0.05)


def test_boundary_detection_flags_only_the_ends():
    """What ``clipped_at_edge`` is built on. A best point in the interior means
    the window bracketed its minimum; one on either end means it did not."""
    points = [TunePoint(float(i), 10.0, 200.0, 1.0) for i in range(4)]
    assert FrequencyTuner._is_edge(0, points) is True
    assert FrequencyTuner._is_edge(3, points) is True
    assert FrequencyTuner._is_edge(1, points) is False
    assert FrequencyTuner._is_edge(None, points) is False
    assert FrequencyTuner._is_edge(0, []) is False


def test_anomalous_step_is_flagged_not_silently_accepted():
    points = [
        TunePoint(1.0, 10.0, 200.0, 0.1),
        TunePoint(2.0, 10.0, 201.0, 0.1),
        TunePoint(3.0, 10.0, 260.0, 0.1),  # far beyond the local trend
    ]
    plant = SimulatedRigPlant(resonant_model=True)
    tuner, _smu, _wavegen, _safety = _build(plant)
    assert tuner._flag_anomaly(points) is True
    assert points[-1].anomaly is True


def test_tank_gain_never_falls_below_unity(plant):
    """The bus must never have to exceed the peak it is producing.

    With the switch off the choke holds current and the drain flies up to at
    least the bus, so detuning erodes the resonant boost but not that floor.
    A bare Lorentzian decays to zero and invents a regime the rig cannot
    reach - which then feeds an unphysically loose reachability bound, since
    the bound is ceiling/gain.
    """
    frequency = 4_000_000.0
    while frequency <= 10_000_000.0:
        plant.wavegen_frequency_hz = frequency
        plant.smu_voltage_setpoint_v = 50.0
        gain, _detuning = plant._tank_state()
        assert gain >= 1.0, f"gain {gain:.3f} below unity at {frequency / 1e6:.2f} MHz"
        assert plant.vds_peak_v() >= plant.bus_voltage_v
        frequency += 250_000.0


def test_gain_still_peaks_near_the_built_resonance(plant):
    """The floor must not flatten the response it is protecting."""
    plant.smu_voltage_setpoint_v = 50.0
    gains = {}
    for mhz in (5.0, 6.4, 8.0):
        plant.wavegen_frequency_hz = mhz * 1e6
        gains[mhz], _ = plant._tank_state()
    assert gains[6.4] > gains[5.0]
    assert gains[6.4] > gains[8.0]


def test_a_clipped_warm_start_is_discarded_and_widened(plant):
    """Warm starting must not be able to ratchet the answer run after run.

    A clipped result seeds the next run's window centre, so accepting an edge
    would walk the reported frequency in one direction indefinitely without
    ever reaching the true minimum. Seen on the bench: successive runs drifting
    down ~5% each time, reporting 0 minima and an edge every time.
    """
    tuner, _smu, wavegen, _safety = _build(plant)
    # A warm start well above the lowest basin, narrow enough to exclude it.
    result = tuner.find_minimum(
        6_000_000.0,
        200.0,
        "Single Device",
        warm_start_hz=5_900_000.0,
        run_survey=False,
    )
    span = max(wavegen.frequencies) - min(wavegen.frequencies)
    warm_span = 2 * 5_900_000.0 * tuner.settings.warm_window_frac
    assert span > warm_span * 1.5, "the search should have widened past the warm window"
    assert not result.clipped_at_edge
    assert result.minima_hz, "a widened window should contain a real minimum"


def test_an_unclipped_warm_start_is_kept(plant):
    """Widening is an escalation, not the normal path: a warm start that
    brackets its minimum must stay narrow, which is what makes it cheap."""
    tuner, _smu, wavegen, _safety = _build(plant)
    tuner.find_minimum(
        6_000_000.0,
        200.0,
        "Single Device",
        warm_start_hz=6_950_000.0,
        run_survey=False,
    )
    span = max(wavegen.frequencies) - min(wavegen.frequencies)
    warm_span = 2 * 6_950_000.0 * tuner.settings.warm_window_frac
    assert span <= warm_span * 1.5


# -- ZVS dwell at the chosen frequency ---------------------------------------
#
# On this rig the minimum-input-power point and the ZVS point coincide: the
# operator's own method is to find ZVS near the target peak and minimise
# current around it. So a winner with no measured ZVS is evidence the search
# converged on a shoulder outside the resonant basin, and it has to be visible
# rather than filed away in the point list.


class _DwellScope(_Scope):
    """A scope whose ZVS dwell can be pinned or made unreadable."""

    def __init__(self, plant, dwell) -> None:
        super().__init__(plant)
        self.dwell = dwell

    def zvs_dwell_fraction(self):
        if self.dwell == "unreadable":
            raise OSError("P4 not configured")
        return self.dwell


def _tune_with_dwell(plant, dwell):
    tuner, _smu, _wavegen, _safety = _build(plant)
    tuner.scope = _DwellScope(plant, dwell)
    tuner.peak_controller.scope = tuner.scope
    messages: list[str] = []
    result = tuner.find_minimum(
        6_000_000.0,
        200.0,
        "Single Device",
        warm_start_hz=6_950_000.0,
        run_survey=False,
        status=messages.append,
    )
    return result, messages


def test_the_dwell_at_the_chosen_frequency_is_reported(plant):
    result, _messages = _tune_with_dwell(plant, 0.18)
    assert result.zvs_dwell_fraction == pytest.approx(0.18)
    assert not result.no_zvs_at_winner


def test_a_winner_with_no_zvs_is_flagged(plant):
    result, messages = _tune_with_dwell(plant, 0.0)
    assert result.zvs_dwell_fraction == 0.0
    assert result.no_zvs_at_winner
    assert any("no ZVS" in message for message in messages), (
        "a zero-dwell winner must reach the operator, not just the log"
    )


def test_an_unmeasurable_dwell_is_not_reported_as_no_zvs(plant):
    """None and 0.0 are different answers. A scope that cannot measure dwell
    says nothing about ZVS, and must not be turned into an accusation that the
    search converged in the wrong place."""
    result, messages = _tune_with_dwell(plant, "unreadable")
    assert result.zvs_dwell_fraction is None
    assert not result.no_zvs_at_winner
    assert not any("no ZVS" in message for message in messages)


def test_dwell_is_measured_at_the_settled_point_not_reused_from_the_sweep(plant):
    """The reported dwell describes the frequency and bus the run will use.

    The sweep reads dwell mid-convergence, before the bus has settled back onto
    the target peak. Marking every swept point with a sentinel that the scope
    never returns shows which of the two the result carries.
    """
    tuner, _smu, _wavegen, _safety = _build(plant)
    scope = _DwellScope(plant, 0.77)
    tuner.scope = scope
    tuner.peak_controller.scope = scope

    original = tuner._evaluate

    def stamp(*args, **kwargs):
        point = original(*args, **kwargs)
        point.zvs_dwell_fraction = 0.11  # never produced by the scope
        return point

    tuner._evaluate = stamp  # type: ignore[method-assign]
    result = tuner.find_minimum(
        6_000_000.0, 200.0, "Single Device",
        warm_start_hz=6_950_000.0, run_survey=False,
    )
    assert result.zvs_dwell_fraction == pytest.approx(0.77)


def test_a_failed_final_read_falls_back_to_what_the_sweep_saw(plant):
    """One unlucky read must not erase a good measurement."""
    tuner, _smu, _wavegen, _safety = _build(plant)
    scope = _DwellScope(plant, 0.31)
    tuner.scope = scope
    tuner.peak_controller.scope = scope

    original = tuner._evaluate

    def stamp_then_break(*args, **kwargs):
        point = original(*args, **kwargs)
        point.zvs_dwell_fraction = 0.11
        return point

    tuner._evaluate = stamp_then_break  # type: ignore[method-assign]
    tuner.find_minimum(
        6_000_000.0, 200.0, "Single Device",
        warm_start_hz=6_950_000.0, run_survey=False,
    )
    # Same search, but with the scope failing by the time the winner settles.
    tuner2, _s2, _w2, _sa2 = _build(plant)
    scope2 = _DwellScope(plant, 0.31)
    tuner2.scope = scope2
    tuner2.peak_controller.scope = scope2
    original2 = tuner2._evaluate

    def stamp_and_disable(*args, **kwargs):
        point = original2(*args, **kwargs)
        point.zvs_dwell_fraction = 0.11
        scope2.dwell = "unreadable"
        return point

    tuner2._evaluate = stamp_and_disable  # type: ignore[method-assign]
    result = tuner2.find_minimum(
        6_000_000.0, 200.0, "Single Device",
        warm_start_hz=6_950_000.0, run_survey=False,
    )
    assert result.zvs_dwell_fraction == pytest.approx(0.11)
    assert not result.no_zvs_at_winner


# -- the plant's loss model must agree with its own ZVS -----------------------
#
# An earlier model shaped switching loss with a Lorentzian and a tilt,
# independently of the dwell it reported. The two ended up in different places:
# minimum P_in sat at 5.30 MHz with dwell exactly zero, while the best ZVS at
# 6.50 MHz cost 39% more power. The search then correctly minimised its
# objective and landed somewhere with no ZVS, and every simulated run looked
# successful while doing it. Nothing failed, because nothing asserted the two
# agreed. These do.


def _loss_landscape(plant, tuner, low_hz=4.6e6, high_hz=7.4e6, step_hz=50_000.0):
    """Measured P_in and dwell across the window, peak held on target."""
    points = []
    frequency = low_hz
    while frequency <= high_hz:
        point = tuner._evaluate(
            frequency, 200.0, False, cancel_check=None, status=None
        )
        if point.reachable and math.isfinite(point.input_power_w):
            points.append(point)
        frequency += step_hz
    return points


def _basins(points):
    return [
        points[i]
        for i in range(1, len(points) - 1)
        if points[i].input_power_w < points[i - 1].input_power_w
        and points[i].input_power_w < points[i + 1].input_power_w
    ]


@pytest.fixture()
def landscape(plant):
    tuner, _smu, _wavegen, _safety = _build(plant)
    return _loss_landscape(plant, tuner)


def test_minimum_input_power_coincides_with_zvs(landscape):
    """The property the whole model exists for, and the one it used to fail.

    On the bench the operator finds ZVS near the target peak and minimises
    current around it, so the two must not be separable in simulation either.
    """
    best = min(landscape, key=lambda point: point.input_power_w)
    assert best.zvs_dwell_fraction, (
        f"minimum P_in at {best.frequency_hz / 1e6:.2f} MHz has no ZVS "
        "— the loss model and the dwell model disagree again"
    )


def test_the_minimum_sits_near_zvs_onset_not_at_maximum_dwell(landscape):
    """Just enough circulating current to complete the transition, no more.

    Driving past onset buys nothing and costs conduction loss, which is the
    class-E design point. A minimum sitting at maximum dwell would mean
    circulating current had been made free.
    """
    best = min(landscape, key=lambda point: point.input_power_w)
    with_zvs = [p for p in landscape if p.zvs_dwell_fraction]
    onset = min(with_zvs, key=lambda point: point.frequency_hz)
    deepest = max(landscape, key=lambda point: point.zvs_dwell_fraction or 0.0)

    assert abs(best.frequency_hz - onset.frequency_hz) < 300_000.0
    assert best.frequency_hz != deepest.frequency_hz
    assert best.input_power_w < deepest.input_power_w


def test_more_than_one_basin_survives(landscape):
    """The search must compare minima rather than descend into the nearest.

    A single smooth bowl would let a local descent pass every test here and
    still be the wrong algorithm for the bench, where multiple minima were
    observed directly.
    """
    basins = _basins(landscape)
    assert len(basins) > 1
    assert all(point.zvs_dwell_fraction for point in basins), (
        "a basin without ZVS is the pathology this model was fixed to remove"
    )


def test_hard_switching_far_from_resonance_is_expensive(landscape):
    """What makes the minimum findable at all.

    Switching loss goes as the residual drain voltage squared, and off
    resonance the bus is highest exactly where the tank helps least. Without
    that the bus term dominates and P_in simply falls toward resonance.
    """
    best = min(landscape, key=lambda point: point.input_power_w)
    worst = max(landscape, key=lambda point: point.input_power_w)
    assert not worst.zvs_dwell_fraction
    assert worst.input_power_w > best.input_power_w * 1.25


def test_losses_vanish_with_the_bus_rather_than_dividing_by_it(plant):
    """Losses are watts, so the model divides by the bus to get a current.

    Both terms scale with voltage squared, which is what keeps that division
    finite: the implied current falls to zero with the bus instead of
    exploding as the ramp passes through zero. Every run starts there.
    """
    for bus in (0.0, 0.001, 0.5, 2.0):
        plant.smu_voltage_setpoint_v = bus
        assert math.isfinite(plant.dc_current_a())
        assert plant.dc_current_a() < 0.1

    # At a bus of zero the resonant terms contribute nothing at all, so the
    # plant reads exactly as it would with the resonant model switched off.
    plant.smu_voltage_setpoint_v = 0.0
    assert plant._resonant_loss_a() == 0.0
    quiet = SimulatedRigPlant(resonant_model=False)
    quiet.smu_output_on = True
    quiet.smu_voltage_setpoint_v = 0.0
    assert plant.dc_current_a() == pytest.approx(quiet.dc_current_a())


def test_a_ramp_from_zero_never_trips_the_overcurrent_interlock(plant):
    """The bus climbs from zero on every run, so a loss model that blew up at
    small bus voltages would trip the interlock before the rig did anything."""
    tuner, _smu, _wavegen, safety = _build(plant)
    for bus in range(0, 120, 2):
        plant.smu_voltage_setpoint_v = float(bus)
        safety.check_sample(
            dc_current=plant.dc_current_a(), vds_peak=plant.vds_peak_v()
        )
    assert not safety.is_tripped
