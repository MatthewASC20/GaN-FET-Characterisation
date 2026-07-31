"""Pure option-set logic and per-device profile isolation.

These cover code that used to live on the tk.Tk subclass and was therefore
impossible to test without a display.
"""

import pytest

from gan_fet.core import param_options as po
from gan_fet.core.device_profile import DeviceProfileStore
from gan_fet.storage.db import Database

DEFAULTS = po.defaults_from_values({
    "configurations": ["Dual Conduction", "Single Conduction", "Single Device"],
    "frequencies": [6_000_000, 13_000_000, 27_000_000],
    "duties": [25, 50],
    "temperatures": [25, 40, 80],
    "voltages": [200, 300, 400],
})


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "profiles.db")
    yield database
    database.close()


def test_default_labels_match_legacy_format():
    assert po.default_label("frequencies", 13_000_000) == "13MHz"
    assert po.default_label("duties", 25) == "25%"
    assert po.default_label("temperatures", 80) == "80°C"
    assert po.default_label("voltages", 400) == "400V"
    assert po.default_label("configurations", "Single Device") == "Single Device"


def test_parse_value_numeric_and_string():
    assert po.parse_value("voltages", " 400 ") == 400
    assert po.parse_value("frequencies", "6,000,000") == 6_000_000
    assert po.parse_value("frequencies", "6e6") == 6_000_000
    assert po.parse_value("configurations", "  Dual Conduction ") == "Dual Conduction"
    for key, bad in (("voltages", "abc"), ("voltages", ""), ("configurations", "  ")):
        with pytest.raises(ValueError):
            po.parse_value(key, bad)


def test_normalize_accepts_legacy_and_modern_shapes():
    # legacy device_config.json pairs, dicts, and bare values all round-trip
    assert po.normalize("voltages", [[300, "300V"], {"value": 200}, 400]) == [
        (200, "200V"), (300, "300V"), (400, "400V"),
    ]


def test_normalize_drops_duplicates_and_junk_and_sorts():
    assert po.normalize("voltages", [400, 200, 400, "nonsense", None, 300]) == [
        (200, "200V"), (300, "300V"), (400, "400V"),
    ]
    # configurations sort by label, not value
    assert po.normalize("configurations", ["Zeta", "alpha", "  ", "Zeta"]) == [
        ("alpha", "alpha"), ("Zeta", "Zeta"),
    ]
    assert po.normalize("voltages", []) == []
    assert po.normalize("voltages", None) == []


def test_serialize_deserialize_round_trip():
    payload = po.serialize(DEFAULTS)
    assert payload["voltages"] == [[200, "200V"], [300, "300V"], [400, "400V"]]
    restored = po.deserialize(payload, DEFAULTS)
    assert restored == {key: list(value) for key, value in DEFAULTS.items()}


def test_deserialize_falls_back_per_key():
    restored = po.deserialize({"voltages": [500]}, DEFAULTS)
    assert restored["voltages"] == [(500, "500V")]          # stored wins
    assert restored["duties"] == list(DEFAULTS["duties"])    # missing key defaults


def test_merge_replaces_without_duplicating():
    merged = po.merge([(200, "200V"), (300, "300V")], 300, "300 volts")
    assert sorted(merged) == [(200, "200V"), (300, "300 volts")]


def test_profile_keeps_devices_isolated(db):
    profile = DeviceProfileStore(db, DEFAULTS)

    assert profile.activate("DEV_A")
    profile.set_options("voltages", [(150, "150V"), (250, "250V")])
    assert profile.values("voltages") == [150, 250]

    # switching devices must persist A's edits and load B's own defaults
    assert profile.activate("DEV_B")
    assert profile.values("voltages") == [200, 300, 400]

    profile.set_options("voltages", [(500, "500V")])
    assert profile.activate("DEV_A")
    assert profile.values("voltages") == [150, 250], "DEV_B's edit leaked into DEV_A"

    assert profile.activate("DEV_B")
    assert profile.values("voltages") == [500]


def test_profile_rejects_edits_with_no_device_and_empty_lists(db):
    profile = DeviceProfileStore(db, DEFAULTS)
    assert profile.set_options("voltages", [(200, "200V")]) is None  # no device yet

    profile.activate("DEV")
    assert profile.set_options("voltages", []) is None
    assert profile.values("voltages") == [200, 300, 400]  # unchanged


def test_profile_reactivating_same_device_is_a_noop(db):
    profile = DeviceProfileStore(db, DEFAULTS)
    assert profile.activate("DEV") is True
    assert profile.activate("DEV") is False
    assert profile.activate("  ") is False
