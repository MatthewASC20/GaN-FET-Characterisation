"""Live telemetry while the search is running.

A tuned run spends almost all of its wall-clock time in the peak ramp and the
frequency search — three and a half minutes against six seconds of sampling in
one recorded simulate run. Both measure the same quantities a run does, and
before these events neither reported them, so the telemetry panel sat frozen
while the rig was at its busiest.
"""

from __future__ import annotations

import math

import pytest

from gan_fet.core.autotune import WavegenController
from gan_fet.core.events import MeasurementEvent, SampleAcquiredEvent, bus
from gan_fet.instruments.mock_scpi import MockScpiTcpClient, SimulatedRigPlant
from gan_fet.instruments.wavegen import Sdg6022x
from gan_fet.settings import WavegenSettings

import sys

sys.path.insert(0, "tests")
from test_frequency_tune import _build  # noqa: E402


@pytest.fixture()
def seen():
    """Collect measurement events published during the test."""
    events: list[MeasurementEvent] = []
    unsubscribe = bus.subscribe(MeasurementEvent, events.append)
    try:
        yield events
    finally:
        unsubscribe()


@pytest.fixture()
def plant() -> SimulatedRigPlant:
    p = SimulatedRigPlant(resonant_model=True)
    p.wavegen_frequency_hz = 6_000_000.0
    p.wavegen_c1_on = True
    p.latch_tank_nominal()
    p.smu_output_on = True
    return p


def test_the_peak_ramp_reports_as_it_climbs(plant, seen):
    """The longest stretch in which the rig is live and nothing moves on
    screen."""
    tuner, _smu, _wavegen, _safety = _build(plant)
    tuner.peak_controller.achieve_peak(200.0)
    ramp = [e for e in seen if e.source == "peak control"]
    assert len(ramp) > 3, "a multi-step ramp should report more than once"
    assert all(e.vds_peak is not None for e in ramp)
    assert all(e.bus_voltage is not None for e in ramp)
    # It climbs, which is the point of watching it.
    assert ramp[-1].vds_peak > ramp[0].vds_peak


def test_every_evaluated_frequency_reports_its_operating_point(plant, seen):
    tuner, _smu, _wavegen, _safety = _build(plant)
    result = tuner.find_minimum(
        6_000_000.0, 200.0, "Single Device", run_survey=False
    )
    search = [e for e in seen if e.source == "frequency search"]
    assert len(search) >= len(result.points)
    for event in search:
        assert event.frequency_hz is not None
        assert event.bus_voltage is not None


def test_the_reported_frequency_tracks_the_sweep(plant, seen):
    """Otherwise the panel would show a frequency the gate is not at."""
    tuner, _smu, _wavegen, _safety = _build(plant)
    tuner.find_minimum(6_000_000.0, 200.0, "Single Device", run_survey=False)
    frequencies = [
        e.frequency_hz for e in seen if e.source == "frequency search"
    ]
    assert len(set(frequencies)) > 3, "the sweep visits more than a few points"


def test_measurements_are_not_samples(plant, seen):
    """These points are the search finding its operating point, and several
    are on the way to somewhere else. Treating them as run data would put
    them in the database and on the run plot."""
    samples: list[SampleAcquiredEvent] = []
    unsubscribe = bus.subscribe(SampleAcquiredEvent, samples.append)
    try:
        tuner, _smu, _wavegen, _safety = _build(plant)
        tuner.find_minimum(
            6_000_000.0, 200.0, "Single Device", run_survey=False
        )
    finally:
        unsubscribe()
    assert seen, "the search should have reported measurements"
    assert samples == [], "a tuning point is not a sample"


def test_a_failing_subscriber_cannot_stop_the_search(plant):
    """This publishes inside a loop holding a live rig at its target peak.

    EventBus already isolates subscribers, so this is guarding that guarantee
    rather than anything in the tuner — but it is the guarantee the tuner
    relies on to publish from a hardware loop at all, and it is worth a test
    that fails if the bus ever stops providing it.
    """

    def explode(_event) -> None:
        raise RuntimeError("subscriber is broken")

    unsubscribe = bus.subscribe(MeasurementEvent, explode)
    try:
        tuner, _smu, _wavegen, _safety = _build(plant)
        result = tuner.find_minimum(
            6_000_000.0, 200.0, "Single Device", run_survey=False
        )
    finally:
        unsubscribe()
    assert result.frequency_hz > 0
    assert math.isfinite(result.input_power_w)


def test_readings_carry_where_they_came_from(plant, seen):
    """The two sources mean different things: one is establishing the
    operating point, the other is searching for it."""
    tuner, _smu, _wavegen, _safety = _build(plant)
    tuner.peak_controller.achieve_peak(200.0)
    tuner.find_minimum(6_000_000.0, 200.0, "Single Device", run_survey=False)
    assert {e.source for e in seen} == {"peak control", "frequency search"}


def test_peak_control_still_checks_current_when_nobody_is_listening(plant):
    """The current read exists to enforce the ceiling. Returning it for
    display must not make it conditional on anyone wanting it."""
    tuner, _smu, _wavegen, safety = _build(plant)
    tuner.peak_controller.achieve_peak(200.0)
    assert not safety.is_tripped


def test_a_measurement_event_defaults_to_nothing_measured():
    """Every field is optional: a reading that could not be taken is absent,
    not zero."""
    event = MeasurementEvent(source="frequency search")
    assert event.frequency_hz is None
    assert event.bus_voltage is None
    assert event.vds_peak is None
    assert event.dc_current is None


# -- The apply and recall ramps report as they move ---------------------------
#
# At the bench rate of 200 kHz/s, moving the gate from 6 to 13 MHz is a
# 35-second ramp. Without these events the frequency card sits on the previous
# run's value the whole time, which reads as "the apply did nothing".


def _controller() -> WavegenController:
    plant = SimulatedRigPlant()
    wavegen = Sdg6022x(MockScpiTcpClient("SDG6022X", shared_plant=plant))
    return WavegenController(wavegen, WavegenSettings())


def test_applying_new_settings_reports_the_ramp_as_it_moves(seen):
    controller = _controller()
    controller.apply("Single Device", 6_000_000, 50)
    seen.clear()

    controller.apply("Single Device", 13_000_000, 50, freq_rate_khz_s=1e9)

    ramp = [e for e in seen if e.source == "frequency ramp"]
    assert len(ramp) > 3, "a multi-step ramp should report more than once"
    frequencies = [e.frequency_hz for e in ramp]
    assert frequencies == sorted(frequencies), "the sweep climbs 6 → 13 MHz"
    assert frequencies[-1] == pytest.approx(13_000_000.0)


def test_the_first_apply_reports_the_configured_frequency(seen):
    """A configuration change sets the frequency directly rather than
    ramping; the display still needs to hear about it."""
    controller = _controller()
    controller.apply("Single Device", 6_000_000, 50)
    ramp = [e for e in seen if e.source == "frequency ramp"]
    assert [e.frequency_hz for e in ramp] == [pytest.approx(6_000_000.0)]


def test_a_no_op_apply_still_reports_where_the_gate_is(seen):
    """Re-applying identical settings moves nothing, but publishing the
    actual frequency lets a stale display resynchronise."""
    controller = _controller()
    controller.apply("Single Device", 13_000_000, 50)
    seen.clear()

    controller.apply("Single Device", 13_000_000, 50)

    ramp = [e for e in seen if e.source == "frequency ramp"]
    assert [e.frequency_hz for e in ramp] == [pytest.approx(13_000_000.0)]


def test_the_recall_ramp_reports_like_the_apply_ramp(seen):
    controller = _controller()
    controller.apply("Single Device", 6_000_000, 50)
    seen.clear()

    controller.ramp_to_frequency(6_500_000.0, "Single Device", rate_khz_s=1e9)

    ramp = [e for e in seen if e.source == "frequency ramp"]
    assert len(ramp) > 3
    assert ramp[-1].frequency_hz == pytest.approx(6_500_000.0)


def test_a_reading_that_trips_the_rig_is_not_published_as_an_operating_point(
    plant, seen
):
    """Publishing happens after the safety checks, never before.

    A reading about to trip the interlock belongs to the interlock first, and
    showing it on the telemetry panel as though it were a normal operating
    point is actively misleading — the last thing the operator would see
    before the trip is a number presented as fine.
    """
    from gan_fet.core.safety import SafetyTrip

    tuner, _smu, _wavegen, _safety = _build(plant)
    original = tuner.safety.check_sample

    def trip_on_the_measured_point(*args, **kwargs):
        # Only the tuner's own post-convergence check passes both, so this
        # trips there rather than inside peak control, which checks current
        # alone on every iteration.
        if kwargs.get("dc_current") is not None and kwargs.get("vds_peak"):
            raise SafetyTrip("overcurrent", "DC input current exceeded limit")
        return original(*args, **kwargs)

    tuner.safety.check_sample = trip_on_the_measured_point  # type: ignore[method-assign]
    with pytest.raises(SafetyTrip):
        tuner._evaluate(
            6_000_000.0, 200.0, False, cancel_check=None, status=None
        )
    search = [e for e in seen if e.source == "frequency search"]
    assert search == [], (
        "a reading that tripped the rig was shown as an operating point"
    )
