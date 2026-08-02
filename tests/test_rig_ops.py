"""Preconditions in front of the manual ZVS search.

This is the control that energises the rig from the front panel, so the order
of the checks is the substance: a search already running is stopped rather than
started again, and a latched trip is refused before the target voltage is even
looked at. Kept tkinter-free so it can be checked without a display.
"""

from __future__ import annotations

import pytest

from gan_fet.ui.operations.rig_ops import ZvsAction, zvs_precondition

MAX_PEAK_V = 450.0


def _decide(**overrides):
    request = {
        "hardware_offline": False,
        "safety_tripped": False,
        "target_peak_v": 200.0,
        "max_vds_peak_v": MAX_PEAK_V,
        "wavegen_pending": False,
    }
    request.update(overrides)
    return zvs_precondition(**request)


def test_a_clear_rig_launches():
    assert _decide().action is ZvsAction.LAUNCH


# -- order of the checks -----------------------------------------------------
#
# Stopping a running search is not decided here. It happens in the window
# before this is consulted, and reads none of this state, so that an operator
# reaching for Stop after a trip or with the hardware offline still stops the
# search. See ZvsAction.


def test_a_latched_trip_is_refused_before_the_target_is_considered():
    """The rig has already been judged unsafe. No value in that field makes it
    safe, so the trip must not be reported as a bad target."""
    decision = _decide(safety_tripped=True, target_peak_v=99_999.0)
    assert decision.action is ZvsAction.REFUSE
    assert decision.refusal is not None
    assert "interlock" in decision.refusal.message


def test_offline_hardware_is_handed_back_to_the_window():
    """The window owns that path because it also switches to Configuration."""
    assert _decide(hardware_offline=True).action is ZvsAction.HARDWARE_OFFLINE


def test_a_trip_is_reported_even_when_the_wavegen_is_also_out_of_date():
    decision = _decide(safety_tripped=True, wavegen_pending=True)
    assert decision.action is ZvsAction.REFUSE


# -- the target voltage ------------------------------------------------------


@pytest.mark.parametrize("target", [0.0, -1.0, -200.0])
def test_a_non_positive_target_is_refused(target):
    decision = _decide(target_peak_v=target)
    assert decision.action is ZvsAction.REFUSE
    assert decision.refusal is not None and decision.refusal.severity == "error"


def test_a_target_above_the_safety_limit_is_refused():
    decision = _decide(target_peak_v=MAX_PEAK_V + 1)
    assert decision.action is ZvsAction.REFUSE
    assert f"{MAX_PEAK_V:g}" in decision.refusal.message


def test_a_target_exactly_at_the_safety_limit_is_allowed():
    assert _decide(target_peak_v=MAX_PEAK_V).action is ZvsAction.LAUNCH


def test_an_unreadable_target_is_refused_not_treated_as_zero():
    """An unreadable field is not a target. Defaulting it to a number would
    let a malformed entry decide what the rig is driven to."""
    decision = _decide(target_peak_v=None)
    assert decision.action is ZvsAction.REFUSE


def test_the_limit_is_whatever_the_caller_passes():
    """The interlock is configuration, not a constant compiled in here."""
    assert _decide(target_peak_v=300.0, max_vds_peak_v=250.0).action is (
        ZvsAction.REFUSE
    )
    assert _decide(target_peak_v=300.0, max_vds_peak_v=400.0).action is (
        ZvsAction.LAUNCH
    )


# -- applying the wavegen first ----------------------------------------------


def test_a_stale_wavegen_offers_to_apply_first():
    decision = _decide(wavegen_pending=True)
    assert decision.action is ZvsAction.APPLY_WAVEGEN_FIRST
    assert decision.prompt is not None
    title, message = decision.prompt
    assert title == "Find ZVS"
    assert "does not match" in message


def test_a_bad_target_is_refused_before_the_wavegen_is_offered():
    """Applying settings that cannot then be searched wastes a rig operation
    and leaves the wavegen changed for nothing."""
    decision = _decide(target_peak_v=0.0, wavegen_pending=True)
    assert decision.action is ZvsAction.REFUSE
