from __future__ import annotations

from gan_fet.core.autotune import WavegenController
from gan_fet.core.zvs import ZvsTuner
from gan_fet.instruments.mock_scpi import SimulatedRigPlant
from gan_fet.settings import WavegenSettings, ZvsSettings


class _DutyWavegen:
    outputs_armed = False

    @staticmethod
    def is_dual(_config: str) -> bool:
        return False

    def ramp_to_duty(self, target: float, _dual: bool, **_kwargs) -> float:
        return float(target)


def test_controller_duty_ramp_returns_the_applied_value() -> None:
    wavegen = _DutyWavegen()
    controller = WavegenController(wavegen, WavegenSettings())
    controller.applied_duty = 10.0

    applied = controller.ramp_to_duty(17.5, "Single Device")

    assert applied == 17.5
    assert controller.applied_duty == 17.5


class _PlantSmu:
    def __init__(self, plant: SimulatedRigPlant, start_v: float) -> None:
        self.plant = plant
        self.plant.smu_output_on = True
        self.plant.smu_voltage_setpoint_v = start_v

    @property
    def setpoint_v(self) -> float:
        return self.plant.smu_voltage_setpoint_v

    @property
    def max_voltage_v(self) -> float:
        return 200.0

    def ramp_to(self, volts: float, **_kwargs) -> None:
        self.plant.smu_voltage_setpoint_v = float(volts)

    def measure_dc_current(self) -> float:
        return self.plant.dc_current_a()


class _NoTripSafety:
    def record_read_success(self, _source: str) -> None:
        return None

    def record_read_failure(self, _source: str) -> None:
        raise AssertionError("simulation current should remain readable")

    def check_sample(self, **_kwargs) -> None:
        return None

    def check_compliance(self) -> None:
        return None


def test_default_simulation_curve_is_tunable_with_default_zvs_threshold() -> None:
    plant = SimulatedRigPlant(zvs_voltage_v=95.0)
    settings = ZvsSettings(samples_per_point=1, settle_s=0.0)
    tuner = ZvsTuner(_PlantSmu(plant, 67.0), settings, _NoTripSafety())
    tuner._wait = lambda _seconds, _cancel: True

    result = tuner.find_minimum()

    assert result is not None
    assert result.v_zvs >= 93.0
    assert result.v_zvs > 67.0
