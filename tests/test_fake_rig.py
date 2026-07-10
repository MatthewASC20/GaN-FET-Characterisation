"""Driver, controller and engine tests against the simulated rig."""

import time

import pytest

from gan_fet.core.experiment import EngineCallbacks, ExperimentEngine
from gan_fet.core.models import ExperimentParams, MatrixPoint
from gan_fet.core.safety import SafetyTrip
from gan_fet.core.voltage_control import PeakVoltageController
from gan_fet.core.zvs import ZvsTuner
from gan_fet.instruments.smu import SmuLimitError

SETTLE = 0.3  # writes are fire-and-forget; give the fake server time to log


def test_smu_init_prologix_preamble_and_setup(rig):
    assert rig.smu.initialize()
    time.sleep(SETTLE)
    cmds = rig.state.commands["smu"]
    assert cmds[:4] == ["++mode 1", "++addr 24", "++auto 1", "++eoi 1"]
    assert "*RST" in cmds
    assert ':SENS:FUNC "CURR"' in cmds
    assert any(c.startswith(":SENS:CURR:PROT 0.1") for c in cmds)


def test_smu_limit_enforcement(rig):
    rig.smu.initialize()
    with pytest.raises(SmuLimitError):
        rig.smu.set_voltage(2000)
    with pytest.raises(SmuLimitError):
        rig.smu.set_voltage(-5)


def test_smu_read_parse(rig):
    rig.smu.initialize()
    rig.smu.output_on()
    rig.smu.set_voltage(100.0)
    volts, amps = rig.smu.read()
    assert volts == pytest.approx(100.0)
    assert amps == pytest.approx(rig.state.dc_current())


def test_peak_control_converges(rig):
    rig.smu.initialize()
    rig.smu.output_on()
    controller = PeakVoltageController(
        rig.smu, rig.scope, rig.settings.peak_control, rig.safety
    )
    bus = controller.achieve_peak(300.0)
    # plant: peak = 3 * bus → expect bus ≈ 100 V
    assert rig.state.vds_peak() == pytest.approx(300.0, abs=rig.settings.peak_control.tolerance_v)
    assert bus == pytest.approx(100.0, abs=1.0)


def test_zvs_finds_minimum(rig):
    rig.smu.initialize()
    rig.smu.output_on()
    rig.smu.ramp_to(100.0)
    tuner = ZvsTuner(rig.smu, rig.settings.zvs, rig.safety)
    result = tuner.find_minimum()
    assert result is not None
    assert result.v_zvs == pytest.approx(rig.state.v_zvs, abs=rig.settings.zvs.step_v)


def test_compliance_trip_shuts_everything_down(rig):
    rig.smu.initialize()
    rig.smu.output_on()
    rig.state.compliance = True
    with pytest.raises(SafetyTrip):
        rig.safety.check_compliance()
    time.sleep(SETTLE)
    assert rig.state.smu_output is False
    assert "C1:OUTP OFF" in rig.state.commands["wavegen"]
    events = rig.db._execute("SELECT kind FROM safety_events").fetchall()
    assert ("compliance",) in events


def test_engine_full_run(rig):
    engine = ExperimentEngine(
        rig.db, rig.settings, rig.smu, rig.scope, rig.dmm, rig.wavegen,
        rig.safety, callbacks=EngineCallbacks(),
    )
    point = MatrixPoint("FAKE1", "Single Device", 6_000_000, 25, 25, 300)
    assert engine.start(ExperimentParams(point=point, duration_minutes=0.02, find_zvs=True))
    engine.wait_until_idle()
    time.sleep(SETTLE)

    run = rig.db.find_run(point)
    assert run is not None and run.status == "completed"
    assert run.v_zvs == pytest.approx(rig.state.v_zvs, abs=1.5)
    assert run.readings.iin is not None
    assert run.readings.fsw_hz == 6_000_000
    assert run.readings.irms == pytest.approx(2.5)
    assert run.readings.vds_pk is not None
    assert len(rig.db.samples_for_run(run.id)) >= 5
    # safe shutdown: bus ramped to 0 and output off
    assert rig.state.smu_output is False
    assert rig.state.smu_volts == pytest.approx(0.0)
