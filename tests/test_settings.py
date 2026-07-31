"""Settings persistence.

The loader is deliberately forgiving: a settings file written by a different
version of the app must still load, with anything it cannot understand
falling back to the default rather than poisoning a section with raw JSON.
"""

import json

from gan_fet.settings import (
    GoogleSettings,
    SafetySettings,
    Settings,
    SmuSettings,
    ZvsSettings,
)


def test_full_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    settings = Settings()
    settings.smu.max_voltage_v = 1234.0
    settings.smu.prologix_gpib_addr = 24
    settings.safety.max_dc_current_a = 0.5
    settings.instruments["K2400"].ip = "10.1.2.3"
    settings.instruments["K2400"].port = 1234
    settings.last_params = {"device_name": "X", "voltage": 300}
    settings.save(path)

    loaded = Settings.load(path)
    assert isinstance(loaded.smu, SmuSettings)
    assert isinstance(loaded.zvs, ZvsSettings)
    assert isinstance(loaded.safety, SafetySettings)
    assert isinstance(loaded.google, GoogleSettings)
    assert loaded.smu.max_voltage_v == 1234.0
    assert loaded.smu.prologix_gpib_addr == 24
    assert loaded.safety.max_dc_current_a == 0.5
    assert loaded.instruments["K2400"].ip == "10.1.2.3"
    assert loaded.instruments["K2400"].port == 1234
    assert loaded.last_params == {"device_name": "X", "voltage": 300}


def test_unknown_keys_are_ignored(tmp_path):
    """A file from a newer version must not break the whole section."""
    path = tmp_path / "settings.json"
    Settings().save(path)
    raw = json.loads(path.read_text())
    raw["smu"]["max_voltage_v"] = 500.0
    raw["smu"]["option_from_the_future"] = True
    path.write_text(json.dumps(raw))

    loaded = Settings.load(path)
    assert loaded.smu.max_voltage_v == 500.0
    assert not hasattr(loaded.smu, "option_from_the_future")


def test_malformed_sections_fall_back_to_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "zvs": "not-a-dict",
        "safety": ["also", "wrong"],
        "smu": {"nplc": 2.0},
        "instruments": {"K2400": {"ip": "10.0.0.1", "port": "not-a-port"}},
    }))

    loaded = Settings.load(path)
    assert isinstance(loaded.zvs, ZvsSettings) and loaded.zvs.step_v == 1.0
    assert isinstance(loaded.safety, SafetySettings)
    assert loaded.smu.nplc == 2.0, "a valid section alongside bad ones still loads"
    # the whole instruments block is rejected together, keeping defaults
    assert loaded.instruments["K2400"].port == 1234


def test_missing_or_corrupt_file_yields_defaults(tmp_path):
    assert Settings.load(tmp_path / "absent.json").smu.nplc == 1.0
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json at all")
    assert Settings.load(corrupt).smu.nplc == 1.0


def test_derived_paths_and_durations(tmp_path):
    settings = Settings()
    settings.data_dir = str(tmp_path)
    assert settings.db_path == tmp_path / "gan_fet.db"
    assert settings.screenshots_dir == tmp_path / "screenshots"
    assert settings.default_duration_minutes(300) == 1
    assert settings.default_duration_minutes(999) == 1  # unmapped voltage
