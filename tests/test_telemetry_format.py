"""Units and missing-value rendering for the live telemetry cards.

These are what an operator reads off the screen mid-run, so the unit choices
and the difference between "zero" and "no reading" are worth pinning. Kept
tkinter-free so they can be checked without a display.
"""

from __future__ import annotations

import pytest

from gan_fet.ui.telemetry_format import (
    NO_READING,
    format_amps,
    format_frequency,
    format_milliamps,
    format_volts,
)


def test_frequency_reads_in_megahertz_at_matrix_frequencies():
    assert format_frequency(6_000_000) == "6.00 MHz"
    assert format_frequency(13_000_000) == "13.00 MHz"
    assert format_frequency(5_318_500) == "5.32 MHz"


def test_frequency_falls_back_to_kilohertz_below_a_megahertz():
    assert format_frequency(500_000) == "500.0 kHz"


def test_a_stopped_gate_is_not_reported_as_a_frequency():
    """Zero hertz is not a switching frequency, so it reads as no value."""
    assert format_frequency(0) == f"{NO_READING} MHz"
    assert format_frequency(-1) == f"{NO_READING} MHz"


def test_dc_current_reads_in_milliamps():
    """The working range is tens of mA; amps would show three leading zeros."""
    assert format_milliamps(0.0327) == "32.70 mA"
    assert format_milliamps(0.0) == "0.00 mA"


def test_rms_currents_read_in_amps():
    """These run to several amps, unlike the DC input current."""
    assert format_amps(2.4197) == "2.420 A"
    assert format_amps(0.0) == "0.000 A"


def test_volts_read_to_a_tenth():
    assert format_volts(199.9) == "199.9 V"
    assert format_volts(0.0) == "0.0 V"


@pytest.mark.parametrize(
    "formatter, unit",
    [
        (format_volts, "V"),
        (format_milliamps, "mA"),
        (format_amps, "A"),
        (format_frequency, "MHz"),
    ],
)
def test_absent_readings_are_a_dash_never_a_zero(formatter, unit):
    """On a rig where zero current means something specific, a missing
    measurement must not look like a measured zero."""
    assert formatter(None) == f"{NO_READING} {unit}"


@pytest.mark.parametrize(
    "formatter", [format_volts, format_milliamps, format_amps, format_frequency]
)
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "not a number", object()])
def test_unusable_values_are_reported_as_missing(formatter, bad):
    assert NO_READING in formatter(bad)


def test_zero_current_is_distinguishable_from_no_current_reading():
    assert format_milliamps(0.0) != format_milliamps(None)
    assert format_amps(0.0) != format_amps(None)
