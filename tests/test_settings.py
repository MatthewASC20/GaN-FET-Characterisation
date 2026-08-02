from __future__ import annotations

import json

import pytest

from gan_fet.instruments.smu import Keithley2400
from gan_fet.settings import (
    SCOPE_INSTRUMENT_KEY,
    SCOPE_MODEL,
    InstrumentAddress,
    Settings,
    SettingsLoadError,
    SmuSettings,
)


def test_defaults_select_the_hdo4054_vxi11_rig():
    settings = Settings()

    assert settings.scope_model == SCOPE_MODEL
    assert settings.instruments == {
        "SDG6022X": InstrumentAddress("10.11.13.230", 5025),
        SCOPE_INSTRUMENT_KEY: InstrumentAddress(
            "TCPIP0::10.11.13.231::inst0::INSTR", 0
        ),
        "K2410": InstrumentAddress("GPIB0::24::INSTR", 0),
    }


def test_optional_dmm_is_not_silently_added_to_the_bench_defaults():
    assert "SDM3055" not in Settings().instruments


def test_legacy_lecroy_endpoint_migrates_without_changing_its_address(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "instruments": {
                    "LeCroy": {
                        "ip": "TCPIP0::192.0.2.50::inst0::INSTR",
                        "port": 0,
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    loaded = Settings.load(path)

    assert loaded.scope_model == SCOPE_MODEL
    assert loaded.instruments[SCOPE_INSTRUMENT_KEY] == InstrumentAddress(
        "TCPIP0::192.0.2.50::inst0::INSTR", 0
    )
    assert "LeCroy" not in loaded.instruments

    loaded.save(path)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["scope_model"] == SCOPE_MODEL
    assert persisted["instruments"][SCOPE_INSTRUMENT_KEY]["port"] == 0
    assert "LeCroy" not in persisted["instruments"]


def test_unsupported_scope_model_is_rejected_without_rewriting_file(tmp_path):
    path = tmp_path / "settings.json"
    original = json.dumps(
        {
            "scope_model": "UnsupportedScope",
            "instruments": {
                SCOPE_INSTRUMENT_KEY: {
                    "ip": "TCPIP0::192.0.2.50::inst0::INSTR",
                    "port": 0,
                }
            },
        },
        indent=2,
    )
    path.write_text(original, encoding="utf-8")

    with pytest.raises(SettingsLoadError, match="incompatible oscilloscope"):
        Settings.load(path)

    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("target", "port"),
    (
        ("10.11.13.231", 4000),
        ("TCPIP0::10.11.13.231::1861::SOCKET", 0),
        ("bogus::target", 0),
    ),
)
def test_unsupported_hdo_transport_is_rejected_without_default_substitution(
    tmp_path,
    target,
    port,
):
    path = tmp_path / "settings.json"
    original = json.dumps(
        {
            "scope_model": SCOPE_MODEL,
            "instruments": {
                SCOPE_INSTRUMENT_KEY: {
                    "ip": target,
                    "port": port,
                }
            },
        }
    )
    path.write_text(original, encoding="utf-8")

    with pytest.raises(SettingsLoadError, match="LXI/VXI-11 VISA"):
        Settings.load(path)

    assert path.read_text(encoding="utf-8") == original


def test_save_rejects_non_visa_scope_target(settings):
    settings.instruments[SCOPE_INSTRUMENT_KEY] = InstrumentAddress(
        "10.11.13.231", 4000
    )

    with pytest.raises(ValueError, match="VISA resource"):
        settings.save()

    assert not settings.settings_path.exists()


def test_save_rejects_boolean_scope_port(settings):
    settings.instruments[SCOPE_INSTRUMENT_KEY] = InstrumentAddress(
        "TCPIP0::10.11.13.231::inst0::INSTR", False
    )

    with pytest.raises(ValueError, match="LXI/VXI-11 VISA"):
        settings.save()

    assert not settings.settings_path.exists()


def test_incompatible_only_instrument_map_is_rejected_before_defaults_merge(
    tmp_path,
):
    path = tmp_path / "settings.json"
    original = json.dumps(
        {
            "instruments": {
                "ArchivedScope": {"ip": "192.0.2.99", "port": 4000},
                "K2410": {"ip": "192.0.2.20", "port": 1234},
            }
        }
    )
    path.write_text(original, encoding="utf-8")

    with pytest.raises(SettingsLoadError, match="no HDO4054-compatible"):
        Settings.load(path)

    assert path.read_text(encoding="utf-8") == original


def test_conflicting_hdo_and_legacy_lecroy_endpoints_are_rejected(tmp_path):
    path = tmp_path / "settings.json"
    original = json.dumps(
        {
            "scope_model": SCOPE_MODEL,
            "instruments": {
                SCOPE_INSTRUMENT_KEY: {
                    "ip": "TCPIP0::192.0.2.50::inst0::INSTR",
                    "port": 0,
                },
                "LeCroy": {
                    "ip": "TCPIP0::192.0.2.51::inst0::INSTR",
                    "port": 0,
                },
            },
        }
    )
    path.write_text(original, encoding="utf-8")

    with pytest.raises(SettingsLoadError, match="endpoints conflict"):
        Settings.load(path)

    assert path.read_text(encoding="utf-8") == original


def test_duplicate_equal_lecroy_endpoint_collapses_to_hdo4054(tmp_path):
    path = tmp_path / "settings.json"
    endpoint = {
        "ip": "TCPIP0::192.0.2.50::inst0::INSTR",
        "port": 0,
    }
    path.write_text(
        json.dumps(
            {
                "scope_model": "LeCroy",
                "instruments": {
                    SCOPE_INSTRUMENT_KEY: endpoint,
                    "LeCroy": dict(endpoint),
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = Settings.load(path)

    assert loaded.scope_model == SCOPE_MODEL
    assert loaded.instruments[SCOPE_INSTRUMENT_KEY] == InstrumentAddress(
        endpoint["ip"], endpoint["port"]
    )
    assert "LeCroy" not in loaded.instruments


def test_settings_round_trip_uses_atomic_json(settings):
    settings.last_params = {"device_name": "GS66504B"}
    settings.save()

    loaded = Settings.load(settings.settings_path)

    assert loaded.last_params == settings.last_params
    assert json.loads(settings.settings_path.read_text())["db_filename"] == "gan_fet.db"


def test_malformed_settings_are_not_overwritten(tmp_path):
    path = tmp_path / "settings.json"
    original = "{not-json\n"
    path.write_text(original)

    with pytest.raises(SettingsLoadError):
        Settings.load(path)

    assert path.read_text() == original


def test_direct_visa_disables_legacy_prologix_framing():
    loaded = Settings._from_dict(
        {
            "instruments": {"K2410": {"ip": "GPIB0::24::INSTR", "port": 9600}},
            "smu": {"prologix_gpib_addr": 24},
        }
    )

    assert loaded.smu.prologix_gpib_addr is None


def test_explicit_transport_uri_accepts_zero_legacy_port():
    loaded = Settings._from_dict(
        {
            "instruments": {
                "K2410": {
                    "ip": "prologix+serial://COM3?baud=9600&addr=24",
                    "port": 0,
                }
            }
        }
    )

    assert loaded.instruments["K2410"].port == 0
    assert loaded.instruments["K2410"].target.startswith("prologix+serial://")


def test_optional_dmm_configuration_is_preserved():
    loaded = Settings._from_dict(
        {
            "instruments": {
                "SDM3055": {"ip": "10.0.0.9", "port": 5025},
            }
        }
    )

    assert loaded.instruments["SDM3055"].ip == "10.0.0.9"


def test_legacy_smu_ramp_delay_migrates_to_canonical_rate():
    loaded = Settings._from_dict(
        {
            "smu": {
                "ramp_step_v": 4.0,
                "ramp_delay_s": 0.5,
            }
        }
    )

    assert loaded.smu.ramp_rate_v_s == pytest.approx(8.0)
    assert loaded.smu.ramp_delay_s == pytest.approx(0.5)


def test_custom_legacy_delay_beats_only_the_transitional_default_rate():
    migrated_default = Settings._from_dict(
        {
            "smu": {
                "ramp_step_v": 5.0,
                "ramp_delay_s": 0.5,
                "ramp_rate_v_s": 25.0,
            }
        }
    )
    explicit_rate = Settings._from_dict(
        {
            "smu": {
                "ramp_step_v": 5.0,
                "ramp_delay_s": 0.5,
                "ramp_rate_v_s": 40.0,
            }
        }
    )

    assert migrated_default.smu.ramp_rate_v_s == pytest.approx(10.0)
    assert explicit_rate.smu.ramp_rate_v_s == pytest.approx(40.0)
    assert explicit_rate.smu.ramp_delay_s == pytest.approx(0.125)


def test_smu_ramp_delay_is_a_computed_compatibility_view_not_persisted(settings):
    settings.smu.ramp_delay_s = 0.4
    settings.save()

    payload = json.loads(settings.settings_path.read_text())
    loaded = Settings.load(settings.settings_path)

    assert "ramp_delay_s" not in payload["smu"]
    assert payload["smu"]["ramp_rate_v_s"] == pytest.approx(12.5)
    assert loaded.smu.ramp_rate_v_s == pytest.approx(12.5)
    assert loaded.smu.ramp_delay_s == pytest.approx(0.4)


def test_smu_ramp_uses_rate_unless_call_explicitly_overrides_delay(monkeypatch):
    class FakeClient:
        handles_prologix_framing = False
        is_visa = False

        def __init__(self) -> None:
            self.output_on = False
            self.writes: list[str] = []

        def write(self, command: str) -> bool:
            self.writes.append(command)
            if command == ":OUTP ON":
                self.output_on = True
            elif command == ":OUTP OFF":
                self.output_on = False
            return True

        def query(self, command: str):
            if command == ":OUTP?":
                return "1" if self.output_on else "0"
            return None

    sleeps: list[float] = []
    monkeypatch.setattr("gan_fet.instruments.smu.time.sleep", sleeps.append)
    client = FakeClient()
    smu = Keithley2400(
        client,
        SmuSettings(ramp_step_v=5.0, ramp_rate_v_s=10.0),
    )
    assert smu.initialize()
    assert smu.output_on()

    smu.ramp_to(12.0)
    assert sleeps == pytest.approx([0.5, 0.5])

    sleeps.clear()
    smu.ramp_to(0.0, step_v=4.0, delay_s=0.1)
    assert sleeps == pytest.approx([0.1, 0.1])
