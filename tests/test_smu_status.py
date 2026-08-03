"""The one line that says whether the rig is energised.

Kept tkinter-free so the wording can be checked without a display.
"""

from __future__ import annotations

from gan_fet.ui.smu_status import smu_status_line


def _line(**overrides) -> str:
    fields = {"setpoint_v": 200.0, "current_a": 0.0327, "output_on": False}
    fields.update(overrides)
    return smu_status_line(**fields)


def test_all_three_fields_are_present():
    assert _line(output_on=True) == "Bus setpoint: 200.0 V | I: 32.70 mA | Output: ON"


def test_the_output_state_is_spelled_out_both_ways():
    assert _line(output_on=True).endswith("Output: ON")
    assert _line(output_on=False).endswith("Output: OFF")


def test_the_voltage_is_labelled_a_setpoint_not_a_measurement():
    """It is what the SMU was commanded to, not what the scope saw across the
    device. The two diverge under load, and reading one as the other is how a
    compliance limit gets mistaken for a working bus."""
    assert "Bus setpoint:" in _line()


def test_current_reads_in_milliamps():
    assert "32.70 mA" in _line(current_a=0.0327)


def test_an_unread_current_is_a_dash_not_a_zero():
    line = _line(current_a=None)
    assert "I: — |" in line
    assert "0.00 mA" not in line


def test_zero_current_is_reported_as_a_measurement():
    """Zero is a reading, and on this rig a meaningful one."""
    assert "I: 0.00 mA" in _line(current_a=0.0)


def test_an_unknown_setpoint_is_a_dash():
    assert _line(setpoint_v=None).startswith("Bus setpoint: — V")


def test_a_live_output_says_so_even_with_nothing_else_known():
    line = smu_status_line(setpoint_v=None, current_a=None, output_on=True)
    assert line.endswith("Output: ON")
