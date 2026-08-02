"""Production-composition regression for the safe virtual bench workflow."""

from __future__ import annotations

from pathlib import Path

import pytest

from gan_fet.core.events import InstrumentCommandEvent, bus
from gan_fet.core.experiment import ExperimentEngine
from gan_fet.core.models import ExperimentParams, MatrixPoint
from gan_fet.core.safety import SafetyMonitor
from gan_fet.instruments.rig import build_instrument_rig
from gan_fet.settings import Settings
from gan_fet.storage.db import Database


def test_real_procedure_collects_viewable_isolated_simulation_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = Settings()
    live.data_dir = str(tmp_path / "live-data")
    live._settings_path = tmp_path / "live-settings.json"
    live._data_base_dir = tmp_path
    live.last_params = {"device_name": "LIVE-DUT"}
    live.save()
    live_settings_before = live.settings_path.read_bytes()
    live_db = live.db_path
    live_db.parent.mkdir(parents=True, exist_ok=True)
    live_db.write_bytes(b"live database sentinel")
    live_db_before = live_db.read_bytes()

    def reject_live_transport(*_args, **_kwargs) -> None:
        raise AssertionError("simulation attempted to construct a live SCPI client")

    monkeypatch.setattr(
        "gan_fet.instruments.scpi.ScpiTcpClient.__init__",
        reject_live_transport,
    )

    settings = live.for_simulation()
    settings.smu.sample_interval_s = 0.005
    settings.smu.ramp_step_v = 100.0
    settings.smu.ramp_rate_v_s = 1_000_000_000.0
    settings.peak_control.settle_s = 0.001
    settings.peak_control.max_iterations = 20
    settings.safety.watchdog_consecutive_failures = 2
    settings.save()

    commands: list[InstrumentCommandEvent] = []
    unsubscribe = bus.subscribe(InstrumentCommandEvent, commands.append)
    database = Database(settings.db_path)
    rig = build_instrument_rig(settings, simulate=True)
    try:
        clients = tuple(rig.clients)
        assert clients
        assert all(client.is_simulated and client.connected for client in clients)
        plants = {id(client.shared_plant) for client in clients}
        assert len(plants) == 1

        identities = {client.name: client.query("*IDN?") for client in clients}
        assert "SDG6022X-SIM" in identities["SDG6022X"]
        assert "LECROY,HDO4054,HDO4054SIM" in identities["HDO4054"]
        assert "K2410-SIM" in identities["K2410"]

        point = MatrixPoint(
            device_name="SIMULATION-E2E",
            config="Dual Conduction",
            frequency_hz=13_000_000,
            duty_pct=50,
            temperature_c=25,
            voltage_v=200,
        )
        rig.wavegen_controller.apply(
            point.config,
            point.frequency_hz,
            point.duty_pct,
        )
        safety = SafetyMonitor(
            settings.safety,
            rig.smu,
            rig.wavegen,
            database,
        )
        engine = ExperimentEngine(
            database,
            settings,
            rig.smu,
            rig.scope,
            rig.dmm,
            rig.wavegen,
            safety,
        )

        assert engine.start(
            ExperimentParams(point=point, duration_minutes=0.0005)
        )
        outcome = engine.wait_until_idle(timeout=5.0)

        assert outcome is not None and outcome.success
        assert outcome.status == "completed"
        assert outcome.record is not None
        record = outcome.record
        samples = database.samples_for_run(record.id)
        assert len(samples) >= 2
        assert all(row[1] is not None and row[2] is not None for row in samples)
        assert any(row[4] is not None and row[4] > 0 for row in samples)
        assert record.readings.vin == pytest.approx(200.0 / 3.0, abs=1.0)
        assert record.readings.fsw_hz == pytest.approx(13_000_000.0)
        assert record.readings.irms is not None and record.readings.irms > 0
        assert record.readings.isw_rms is not None and record.readings.isw_rms > 0
        assert record.readings.vds_pk == pytest.approx(200.0, abs=2.0)

        screenshot = Path(record.screenshot_path or "")
        assert screenshot.is_file()
        assert screenshot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert screenshot.is_relative_to(settings.simulation_root)

        plant = clients[0].shared_plant
        assert not rig.smu.output_is_on
        assert not rig.wavegen.outputs_armed
        assert not plant.smu_output_on
        assert not plant.wavegen_c1_on
        assert not plant.wavegen_c2_on
        assert plant.smu_voltage_setpoint_v == pytest.approx(0.0)
        assert commands and all(event.is_simulated for event in commands)
    finally:
        unsubscribe()
        rig.close_clients()
        database.close()

    assert live.settings_path.read_bytes() == live_settings_before
    assert live_db.read_bytes() == live_db_before
    assert settings.db_path.is_relative_to(settings.simulation_root)
    assert settings.settings_path.is_relative_to(settings.simulation_root)
    assert settings.screenshots_dir.is_relative_to(settings.simulation_root)
    assert settings.logs_dir.is_relative_to(settings.simulation_root)
