"""Autotune must not move the gate frequency unguarded.

Moving the gate frequency shifts the resonant operating point and therefore
Vds peak, and this ramp has no closed-loop peak control behind it — unlike the
in-run frequency search. This covers the ramp guard: if the operation runs
with the bus live anyway, it halts before the interlock would trip. The UI
policy that denies it in the first place is in ``test_ui_logic.py``, which
requires tkinter.
"""

from __future__ import annotations

import pytest

from gan_fet.core.autotune import FrequencyRampAborted, WavegenController
from gan_fet.instruments.mock_scpi import SimulatedRigPlant
from gan_fet.settings import WavegenSettings


# -- Ramp guard: halts before the interlock ----------------------------------


class _Scope:
    def __init__(self, plant: SimulatedRigPlant) -> None:
        self.plant = plant

    def peak_voltage(self):
        return self.plant.vds_peak_v()


class _FailingScope:
    def peak_voltage(self):
        raise ConnectionError("scope unreachable")


class _Wavegen:
    """Steps frequency in 10 kHz increments like the real driver."""

    def __init__(self, plant: SimulatedRigPlant) -> None:
        self.plant = plant
        self.outputs_armed = True

    @staticmethod
    def is_dual(config: str) -> bool:
        return "Dual" in config

    def read_frequency(self):
        return self.plant.wavegen_frequency_hz

    def ramp_to_frequency(
        self, target_freq_hz, dual, *, rate_khz_s=None, cancel_check=None
    ) -> float:
        current = float(self.plant.wavegen_frequency_hz)
        target = float(target_freq_hz)
        direction = 1.0 if target > current else -1.0
        while abs(target - current) > 1.0:
            if cancel_check is not None and cancel_check():
                return current
            current += direction * min(10_000.0, abs(target - current))
            self.plant.wavegen_frequency_hz = current
        return current


def _energised_plant() -> SimulatedRigPlant:
    plant = SimulatedRigPlant(resonant_model=True)
    plant.wavegen_frequency_hz = 5_000_000.0
    plant.wavegen_c1_on = True
    plant.latch_tank_nominal()
    plant.smu_output_on = True
    # A bus that is safe far from resonance but not as the tank is approached.
    plant.smu_voltage_setpoint_v = 180.0
    return plant


def test_ramp_aborts_before_the_hard_ceiling():
    plant = _energised_plant()
    controller = WavegenController(
        _Wavegen(plant), WavegenSettings(), scope=_Scope(plant),
        peak_ceiling_v=429.75,
    )
    with pytest.raises(FrequencyRampAborted) as excinfo:
        controller.ramp_to_frequency(6_400_000.0, "Single Device")

    # Stopped below the 450 V interlock, not after breaching it.
    assert excinfo.value.peak_v > 429.75
    assert plant.vds_peak_v() < 450.0
    # And short of the requested target.
    assert plant.wavegen_frequency_hz < 6_400_000.0


def test_aborted_ramp_records_the_frequency_actually_reached():
    plant = _energised_plant()
    controller = WavegenController(
        _Wavegen(plant), WavegenSettings(), scope=_Scope(plant),
        peak_ceiling_v=429.75,
    )
    with pytest.raises(FrequencyRampAborted):
        controller.ramp_to_frequency(6_400_000.0, "Single Device")
    # The gate is where the ramp stopped; reporting the target would misstate
    # the operating point to everything downstream.
    assert controller.tuned_freq_hz == pytest.approx(
        plant.wavegen_frequency_hz
    )


def test_safe_ramp_completes_untouched():
    plant = _energised_plant()
    plant.smu_voltage_setpoint_v = 5.0
    controller = WavegenController(
        _Wavegen(plant), WavegenSettings(), scope=_Scope(plant),
        peak_ceiling_v=429.75,
    )
    controller.ramp_to_frequency(6_400_000.0, "Single Device")
    assert plant.wavegen_frequency_hz == pytest.approx(6_400_000.0)
    assert controller.tuned_freq_hz == pytest.approx(6_400_000.0)


def test_guard_is_inert_without_a_scope_or_ceiling():
    """Unconfigured guard must not silently block ramps."""
    plant = _energised_plant()
    plant.smu_voltage_setpoint_v = 5.0
    controller = WavegenController(_Wavegen(plant), WavegenSettings())
    controller.ramp_to_frequency(6_400_000.0, "Single Device")
    assert plant.wavegen_frequency_hz == pytest.approx(6_400_000.0)


def test_read_failure_does_not_abort_the_ramp():
    """A failed read is the watchdog's business, not a reason to stop here.

    Aborting on unreadable telemetry would make a flaky scope look like a
    peak excursion; repeated failures are already handled by the safety
    monitor's read-failure watchdog.
    """
    plant = _energised_plant()
    plant.smu_voltage_setpoint_v = 5.0
    controller = WavegenController(
        _Wavegen(plant), WavegenSettings(), scope=_FailingScope(),
        peak_ceiling_v=429.75,
    )
    controller.ramp_to_frequency(6_400_000.0, "Single Device")
    assert plant.wavegen_frequency_hz == pytest.approx(6_400_000.0)


def test_cancellation_still_stops_the_ramp():
    plant = _energised_plant()
    plant.smu_voltage_setpoint_v = 5.0
    controller = WavegenController(
        _Wavegen(plant), WavegenSettings(), scope=_Scope(plant),
        peak_ceiling_v=429.75,
    )
    calls = {"n": 0}

    def cancel_after_a_few() -> bool:
        calls["n"] += 1
        return calls["n"] > 5

    controller.ramp_to_frequency(
        6_400_000.0, "Single Device", cancel_check=cancel_after_a_few
    )
    assert plant.wavegen_frequency_hz < 6_400_000.0
