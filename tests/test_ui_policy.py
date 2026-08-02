"""UI decisions that were trapped inside the window class.

Parameter-option cleaning, run-request validation and confirm-button state are
all pure logic that previously required a display to exercise, because they sat
in ``MainWindow`` interleaved with widget calls. These tests run anywhere.

The confirm/autotune presentation is tested in ``test_ui_logic.py`` instead:
it lives in ``ui.widgets``, which imports tkinter.
"""

from __future__ import annotations

import pytest

from gan_fet.ui.param_options import OPTION_KEYS, default_label, normalize_options
from gan_fet.ui.run_request import (
    InputRejected,
    build_experiment_params,
    build_matrix_point,
    parse_positive_duration,
    require_populated_options,
    tuning_candidate,
    validated_device_name,
)

CEILING_V = 450.0


def _options(key, raw):
    return normalize_options(key, raw, max_vds_peak_v=CEILING_V)


# -- parameter options -------------------------------------------------------


def test_stored_entries_are_accepted_in_every_shape_releases_have_written():
    """Bare values, pairs and dicts have all been persisted at some point."""
    assert _options("duties", [25]) == [(25, "25%")]
    assert _options("duties", [(25, "quarter")]) == [(25, "quarter")]
    assert _options("duties", [{"value": 25, "label": "quarter"}]) == [(25, "quarter")]


def test_unlabelled_options_get_a_readable_default():
    assert _options("voltages", [400]) == [(400, "400V")]
    assert _options("temperatures", [40]) == [(40, "40°C")]
    assert default_label("frequencies", 6_000_000) == "6MHz"


def test_a_single_bad_entry_does_not_discard_the_whole_set():
    """A malformed option should not make a device unusable."""
    assert _options("duties", [25, "nonsense", None, 50]) == [(25, "25%"), (50, "50%")]


def test_duplicates_are_collapsed():
    assert _options("voltages", [200, 200, 300]) == [(200, "200V"), (300, "300V")]


def test_targets_above_the_interlock_are_rejected():
    """Offering an unreachable target only buys a run that fails once live."""
    assert _options("voltages", [200, 500]) == [(200, "200V")]


def test_out_of_range_values_are_rejected():
    assert _options("duties", [0, 50, 100]) == [(50, "50%")]
    assert _options("frequencies", [0, 6_000_000]) == [(6_000_000, "6MHz")]


def test_unknown_configurations_are_rejected():
    assert _options("configurations", ["Single Device", "Made Up"]) == [
        ("Single Device", "Single Device")
    ]


def test_configurations_sort_by_name_and_magnitudes_by_value():
    names = [label for _v, label in _options("configurations", list(reversed(
        ["Dual Conduction", "Single Conduction", "Single Device"]
    )))]
    assert names == sorted(names, key=str.lower)
    assert [v for v, _ in _options("voltages", [400, 200, 300])] == [200, 300, 400]


def test_empty_input_yields_no_options():
    assert _options("voltages", None) == []
    assert _options("voltages", []) == []


# -- run request -------------------------------------------------------------


def test_a_device_name_the_sanitiser_would_change_is_rejected():
    """Silently sanitising would file results under a name nobody typed."""
    assert validated_device_name(" GS66504B ") == "GS66504B"
    with pytest.raises(InputRejected):
        validated_device_name("bad/name")
    with pytest.raises(InputRejected):
        validated_device_name("   ")


def test_missing_option_sets_are_named_in_the_rejection():
    with pytest.raises(InputRejected) as excinfo:
        require_populated_options(
            {"configurations": ["Single Device"], "voltages": []}, OPTION_KEYS
        )
    assert "Voltage" in excinfo.value.message
    assert "Frequency" in excinfo.value.message
    assert "Configuration" not in excinfo.value.message


def test_a_complete_selection_builds_a_point():
    point = build_matrix_point(
        device_name="GS66504B",
        config="Single Device",
        frequency_hz="6000000",
        duty_pct="50",
        temperature_c="25",
        voltage_v="200",
    )
    assert point.device_name == "GS66504B"
    assert point.frequency_hz == 6_000_000


def test_an_unparseable_field_is_rejected_not_guessed():
    with pytest.raises(InputRejected, match="Invalid parameter selection"):
        build_matrix_point(
            device_name="GS66504B",
            config="Single Device",
            frequency_hz="six million",
            duty_pct="50",
            temperature_c="25",
            voltage_v="200",
        )


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "", "inf", "nan", "100000"])
def test_durations_that_would_strand_the_rig_are_rejected(bad):
    with pytest.raises(ValueError):
        parse_positive_duration(bad)


def test_params_carry_the_search_flags_through():
    point = build_matrix_point(
        device_name="D",
        config="Single Device",
        frequency_hz=6_000_000,
        duty_pct=50,
        temperature_c=25,
        voltage_v=200,
    )
    params = build_experiment_params(
        point, "1.5", find_zvs=True, tune_frequency=False
    )
    assert params.duration_minutes == pytest.approx(1.5)
    assert params.find_zvs is True
    assert params.tune_frequency is False


def test_a_bad_duration_rejects_the_whole_request():
    point = build_matrix_point(
        device_name="D",
        config="Single Device",
        frequency_hz=6_000_000,
        duty_pct=50,
        temperature_c=25,
        voltage_v=200,
    )
    with pytest.raises(InputRejected, match="duration"):
        build_experiment_params(point, "-5", find_zvs=False, tune_frequency=True)


def test_no_tuning_candidate_when_already_at_the_prior_frequency():
    """Offering a tune that changes nothing is a button that does nothing."""
    prior = (6_400_000.0, "Single Device", 25)
    assert tuning_candidate(prior, applied_freq_hz=6_400_000.0) is None
    assert tuning_candidate(prior, applied_freq_hz=6_400_000.5) is None
    assert tuning_candidate(prior, applied_freq_hz=6_300_000.0) == prior
    assert tuning_candidate(prior, applied_freq_hz=None) == prior
    assert tuning_candidate(None, applied_freq_hz=6_000_000.0) is None
