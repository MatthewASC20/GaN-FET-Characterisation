"""ZVS dwell instrumentation.

The dwell fraction is recorded on every run and sweep point so that ordinary
matrix work accumulates the evidence for whether ZVS onset can be bisected,
rather than requiring a dedicated bench session. Nothing acts on it yet.

The distinction these tests protect is between a measured zero and no
measurement: the first says the switch is hard switching, the second says the
scope is not configured. Collapsing them would report hard switching whenever
P4 was missing.
"""

from __future__ import annotations

import pytest

from gan_fet.instruments.base import OscilloscopeInterface
from gan_fet.instruments.mock_scpi import MockScpiTcpClient, SimulatedRigPlant
from gan_fet.instruments.oscilloscope import LeCroyHdo4054


def _plant(bus_v: float, frequency_hz: float) -> SimulatedRigPlant:
    plant = SimulatedRigPlant(resonant_model=True)
    plant.wavegen_frequency_hz = frequency_hz
    plant.wavegen_c1_on = True
    plant.latch_tank_nominal()
    plant.smu_output_on = True
    plant.smu_voltage_setpoint_v = bus_v
    return plant


# -- plant model -------------------------------------------------------------


def test_dwell_is_exactly_zero_below_onset():
    """Zero, not merely small: onset is a crossing, and a search that bisects
    it depends on the flat region actually being flat."""
    plant = _plant(bus_v=40.0, frequency_hz=5_000_000.0)  # far below resonance
    assert plant.zvs_dwell_fraction() == 0.0


def test_dwell_becomes_positive_past_onset():
    plant = _plant(bus_v=40.0, frequency_hz=6_390_000.0)  # at resonance
    assert plant.zvs_dwell_fraction() > 0.0


def test_dwell_rises_monotonically_towards_resonance():
    """The property a bisection would rely on, asserted against the model."""
    plant = _plant(bus_v=40.0, frequency_hz=5_000_000.0)
    previous = -1.0
    for frequency in range(5_000_000, 6_400_000, 100_000):
        plant.wavegen_frequency_hz = float(frequency)
        dwell = plant.zvs_dwell_fraction()
        assert dwell >= previous, f"dwell fell at {frequency / 1e6:.2f} MHz"
        previous = dwell


def test_dwell_is_zero_when_the_rig_is_not_energised():
    plant = _plant(bus_v=40.0, frequency_hz=6_390_000.0)
    plant.smu_output_on = False
    assert plant.zvs_dwell_fraction() == 0.0
    plant.smu_output_on = True
    plant.wavegen_c1_on = False
    assert plant.zvs_dwell_fraction() == 0.0


def test_dwell_saturates_rather_than_exceeding_the_period():
    plant = _plant(bus_v=400.0, frequency_hz=6_390_000.0)
    assert 0.0 <= plant.zvs_dwell_fraction() <= 0.5


# -- driver ------------------------------------------------------------------


def _driver(plant: SimulatedRigPlant) -> LeCroyHdo4054:
    return LeCroyHdo4054(MockScpiTcpClient("HDO4054", shared_plant=plant))


def test_driver_normalises_a_percentage_to_a_fraction():
    plant = _plant(bus_v=40.0, frequency_hz=6_390_000.0)
    expected = plant.zvs_dwell_fraction()
    assert expected > 0.01, "fixture should produce a measurable dwell"
    assert _driver(plant).zvs_dwell_fraction() == pytest.approx(
        expected, abs=1e-4
    )


def test_driver_writes_a_threshold_scaled_to_the_target():
    plant = _plant(bus_v=40.0, frequency_hz=6_390_000.0)
    assert _driver(plant).set_zvs_threshold(10.0) is True
    assert plant.zvs_threshold_v == pytest.approx(10.0)


def test_driver_refuses_a_non_positive_threshold():
    """A zero or negative level would make every sample count as 'at zero'."""
    plant = _plant(bus_v=40.0, frequency_hz=6_390_000.0)
    driver = _driver(plant)
    assert driver.set_zvs_threshold(0.0) is False
    assert driver.set_zvs_threshold(-5.0) is False
    assert plant.zvs_threshold_v is None


def test_interface_default_reports_no_measurement_not_zero():
    """A scope without P4 configured must not look like hard switching."""

    class _Bare(OscilloscopeInterface):
        def verify_identity(self) -> str:
            return "LECROY,HDO4054"

        def peak_voltage(self):
            return 200.0

        def rms_current(self):
            return 1.0

        def isw_rms(self):
            return 0.5

        def screenshot(self, dest_path):
            return None

    scope = _Bare()
    assert scope.zvs_dwell_fraction() is None
    assert scope.set_zvs_threshold(10.0) is False
