from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gan_fet import app
from gan_fet.settings import (
    SIMULATION_DB_FILENAME,
    SIMULATION_DIRNAME,
    SIMULATION_SETTINGS_FILENAME,
    Settings,
)


def test_simulation_settings_isolate_every_mutable_artifact(settings):
    settings.google.enabled = True
    settings.last_params = {"device_name": "LIVE-DUT"}
    settings.save()
    live_contents = settings.settings_path.read_bytes()

    simulated = settings.for_simulation()
    root = (settings.resolved_data_dir / SIMULATION_DIRNAME).resolve()

    assert simulated is not settings
    assert simulated.is_simulation_runtime
    assert simulated.resolved_data_dir == root
    assert simulated.settings_path == root / SIMULATION_SETTINGS_FILENAME
    assert simulated.db_path == root / SIMULATION_DB_FILENAME
    assert simulated.simulation_db_path == simulated.db_path
    assert simulated.screenshots_dir == root / "screenshots"
    assert simulated.logs_dir == root / "logs"
    assert not simulated.google.enabled
    assert simulated.last_params["device_name"] == "SIMULATED-DUT"

    simulated.last_params["device_name"] = "SIMULATED-DUT"
    simulated.save()

    assert settings.last_params == {"device_name": "LIVE-DUT"}
    assert settings.settings_path.read_bytes() == live_contents
    persisted = json.loads(simulated.settings_path.read_text(encoding="utf-8"))
    assert persisted["data_dir"] == str(root)
    assert persisted["db_filename"] == SIMULATION_DB_FILENAME
    assert persisted["google"]["enabled"] is False


def test_existing_simulation_settings_are_loaded_but_paths_remain_pinned(settings):
    first = settings.for_simulation()
    first.last_params = {"device_name": "PERSISTED-SIM-DUT"}
    first.save()

    payload = json.loads(first.settings_path.read_text(encoding="utf-8"))
    payload["data_dir"] = "/tmp/attempted-simulation-escape"
    payload["db_filename"] = "attempted-escape.db"
    payload["google"]["enabled"] = True
    first.settings_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = settings.for_simulation()
    root = (settings.resolved_data_dir / SIMULATION_DIRNAME).resolve()

    assert loaded.last_params == {"device_name": "PERSISTED-SIM-DUT"}
    assert loaded.resolved_data_dir == root
    assert loaded.db_path == root / SIMULATION_DB_FILENAME
    assert loaded.settings_path == root / SIMULATION_SETTINGS_FILENAME
    assert not loaded.google.enabled


def test_legacy_settings_can_be_read_without_materializing_live_config(
    tmp_path,
    monkeypatch,
):
    requested = tmp_path / "new" / "settings.json"
    legacy = tmp_path / "legacy" / "settings.json"
    source = Settings()
    source.last_params = {"device_name": "LEGACY-DUT"}
    source.save(legacy)

    import gan_fet.settings as settings_module

    monkeypatch.setattr(settings_module, "DEFAULT_SETTINGS_PATH", requested)
    monkeypatch.setattr(settings_module, "LEGACY_SETTINGS_PATHS", (legacy,))

    loaded = Settings.load(persist_legacy_migration=False)

    assert loaded.last_params == {"device_name": "LEGACY-DUT"}
    assert loaded.settings_path == requested.resolve()
    assert not requested.exists()


@pytest.mark.parametrize(
    "arguments",
    (
        ["--simulate", "--diagnose"],
        ["--simulate", "--migrate"],
        ["--simulate", "--migrate", "/tmp/legacy-data"],
    ),
)
def test_simulation_cli_rejects_hardware_or_migration_modes(arguments):
    with pytest.raises(SystemExit) as caught:
        app.main(arguments)

    assert caught.value.code == 2


def test_simulation_cli_does_not_save_the_live_settings(monkeypatch, settings):
    calls = {}

    def fake_load(cls, *, persist_legacy_migration=True):
        calls["persist_legacy_migration"] = persist_legacy_migration
        return settings

    def fail_live_save(*_args, **_kwargs):
        raise AssertionError("simulation CLI attempted to save live settings")

    def fake_run_gui(received, simulate=False, use_qt=False):
        calls["run_gui"] = (received, simulate)
        return 17

    monkeypatch.setattr(Settings, "load", classmethod(fake_load))
    monkeypatch.setattr(settings, "save", fail_live_save)
    monkeypatch.setattr(app, "run_gui", fake_run_gui)

    assert app.main(["--simulate"]) == 17
    assert calls["persist_legacy_migration"] is False
    assert calls["run_gui"] == (settings, True)


def test_direct_run_gui_call_uses_only_simulation_settings_and_paths(
    monkeypatch,
    settings,
):
    settings.google.enabled = True
    settings.save()
    live_contents = settings.settings_path.read_bytes()
    seen = {}

    class FakeLogger:
        def __init__(self, path):
            seen["log_path"] = path

        def close(self, **_kwargs):
            return True

    class FakeDatabase:
        def close(self):
            seen["db_closed"] = True

    class FakeSafety:
        def __init__(self, safety_settings, smu, wavegen, db):
            seen["safety_settings"] = safety_settings

    class FakeEngine:
        def __init__(self, db, runtime, smu, scope, dmm, wavegen, safety):
            seen["engine_settings"] = runtime

    class FakeSheets:
        available = False

        def __init__(self, google_settings, frequencies):
            seen["google_settings"] = google_settings
            self.status = lambda _message: None

        def close(self, **_kwargs):
            return True

    class FakeStatusBar:
        def set_message(self, message):
            seen["status"] = message

    class FakeWindow:
        def __init__(self, runtime, *_args, **kwargs):
            seen["window_settings"] = runtime
            seen["window_simulated"] = kwargs["is_simulated"]
            self.status_bar = FakeStatusBar()

        def _status_async(self, _message):
            pass

        def mainloop(self):
            seen["mainloop"] = True

    def fake_open_database(path):
        seen["db_path"] = path
        return FakeDatabase(), 0

    def fake_build_rig(runtime, *, simulate=False):
        seen["rig_settings"] = runtime
        seen["rig_simulated"] = simulate
        instrument = SimpleNamespace(client=SimpleNamespace())
        return SimpleNamespace(
            wavegen=instrument,
            scope=instrument,
            dmm=None,
            smu=instrument,
            wavegen_controller=object(),
            clients=(),
        )

    import gan_fet.core.command_logger as logger_module
    import gan_fet.core.experiment as experiment_module
    import gan_fet.core.safety as safety_module
    import gan_fet.instruments.rig as rig_module
    import gan_fet.sheets.sync as sheets_module
    import gan_fet.ui.main_window as window_module

    monkeypatch.setattr(logger_module, "ScpiFileLogger", FakeLogger)
    monkeypatch.setattr(experiment_module, "ExperimentEngine", FakeEngine)
    monkeypatch.setattr(safety_module, "SafetyMonitor", FakeSafety)
    monkeypatch.setattr(rig_module, "build_instrument_rig", fake_build_rig)
    monkeypatch.setattr(sheets_module, "SheetsSync", FakeSheets)
    monkeypatch.setattr(window_module, "MainWindow", FakeWindow)
    monkeypatch.setattr(app, "_open_runtime_database", fake_open_database)

    assert app.run_gui(settings, simulate=True) == 0

    root = (settings.resolved_data_dir / SIMULATION_DIRNAME).resolve()
    runtime = seen["window_settings"]
    assert runtime is not settings
    assert runtime.is_simulation_runtime
    assert seen["engine_settings"] is runtime
    assert seen["rig_settings"] is runtime
    assert seen["rig_simulated"] is True
    assert seen["window_simulated"] is True
    assert seen["db_path"] == root / SIMULATION_DB_FILENAME
    assert seen["log_path"] == root / "logs" / "gan_experiment_scpi.log"
    assert seen["google_settings"].enabled is False
    assert runtime.settings_path.is_file()
    assert settings.settings_path.read_bytes() == live_contents
    assert seen["db_closed"] is True
    assert seen["mainloop"] is True
