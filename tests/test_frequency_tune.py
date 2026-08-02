"""Frequency-search regressions against the simulated resonant plant.

The plant is not a circuit model. These tests assert control behaviour and
safety ordering — that the search covers its window, compares every minimum
rather than descending into the nearest one, holds Vds peak on target, honours
cancellation, and never reaches the hard ceiling. Whether the frequency it
picks is *physically* right is a bench question, not a test question.
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


def test_edge_clipping_is_reported(plant):
    """A window that excludes the true optimum must say so rather than
    silently returning its boundary."""
    tuner, _smu, _wavegen, _safety = _build(plant)
    # A window entirely below the lowest basin, so P_in is still falling at
    # the upper edge and the best point is the boundary itself.
    result = tuner.find_minimum(
        6_000_000.0,
        200.0,
        "Single Device",
        warm_start_hz=4_600_000.0,
        run_survey=False,
    )
    assert result.clipped_at_edge


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
