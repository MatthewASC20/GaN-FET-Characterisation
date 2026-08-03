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


# -- the interlock guard between energising steps -----------------------------


class _Safety:
    def __init__(self, trip_reason=None) -> None:
        self.trip_reason = trip_reason


class _Trip(Exception):
    def __init__(self, *reason) -> None:
        super().__init__(*reason)
        self.reason = reason


def _abort(*, cancelled=False, trip_reason=None):
    from gan_fet.ui.operations.rig_ops import raise_if_aborted

    return raise_if_aborted(
        cancelled=lambda: cancelled,
        safety=_Safety(trip_reason),
        trip_error=_Trip,
        what="ZVS search",
    )


def test_a_clear_rig_passes_the_guard():
    assert _abort() is None


def test_cancellation_stops_the_next_step():
    with pytest.raises(InterruptedError, match="ZVS search cancelled"):
        _abort(cancelled=True)


def test_a_trip_stops_the_next_step_and_carries_its_reason():
    with pytest.raises(_Trip) as raised:
        _abort(trip_reason=("overcurrent", "DC input current exceeded limit"))
    assert raised.value.reason == (
        "overcurrent",
        "DC input current exceeded limit",
    )


def test_cancellation_is_reported_ahead_of_a_trip():
    """An operator who pressed Stop is told the search stopped, not shown a
    trip they did not cause. The trip is not lost: the safety monitor has
    already latched and recorded it independently."""
    with pytest.raises(InterruptedError):
        _abort(cancelled=True, trip_reason=("overcurrent", "…"))


def test_the_operation_name_appears_in_the_cancellation():
    from gan_fet.ui.operations.rig_ops import raise_if_aborted

    with pytest.raises(InterruptedError, match="autotune cancelled"):
        raise_if_aborted(
            cancelled=lambda: True,
            safety=_Safety(),
            trip_error=_Trip,
            what="autotune",
        )


def test_the_guard_is_re_evaluated_on_every_call():
    """It exists to be called between steps, because both conditions can
    arrive during the previous one. A guard that cached its answer would let
    the rest of the sequence run on a rig already told to stop."""
    from gan_fet.ui.operations.rig_ops import raise_if_aborted

    state = {"cancelled": False}
    guard = lambda: raise_if_aborted(  # noqa: E731
        cancelled=lambda: state["cancelled"],
        safety=_Safety(),
        trip_error=_Trip,
        what="ZVS search",
    )
    assert guard() is None
    state["cancelled"] = True
    with pytest.raises(InterruptedError):
        guard()


# -- autotune ------------------------------------------------------------------
#
# Autotune ramps the gate frequency with no closed-loop peak control behind it,
# so the bus check is not a convenience: with the bus live the resonant
# operating point, and therefore Vds peak, moves uncontrolled.


def _autotune(**overrides):
    from gan_fet.ui.operations.rig_ops import autotune_precondition

    request = {
        "hardware_offline": False,
        "bus_energised": False,
        "has_candidate": True,
        "wavegen_pending": False,
    }
    request.update(overrides)
    return autotune_precondition(**request)


def test_autotune_launches_with_the_bus_off_and_a_candidate():
    from gan_fet.ui.operations.rig_ops import AutotuneAction

    assert _autotune().action is AutotuneAction.LAUNCH


def test_a_live_bus_refuses_autotune():
    from gan_fet.ui.operations.rig_ops import AutotuneAction

    decision = _autotune(bus_energised=True)
    assert decision.action is AutotuneAction.REFUSE
    assert "no closed-loop peak control" in decision.refusal.message


def test_the_refusal_names_the_alternative_that_is_safe():
    """The operator is being told to do something else. 'Find frequency before
    run' holds Vds peak on target throughout, which autotune does not."""
    decision = _autotune(bus_energised=True)
    assert "Find frequency before run" in decision.refusal.message


def test_a_live_bus_outranks_a_missing_candidate():
    """Both refuse, but only one of them is about the rig being unsafe."""
    decision = _autotune(bus_energised=True, has_candidate=False)
    assert "bus is energised" in decision.refusal.message


def test_no_stored_frequency_is_reported_as_information_not_a_warning():
    """Nothing is wrong; there is simply nothing to tune to yet."""
    from gan_fet.ui.operations.rig_ops import AutotuneAction

    decision = _autotune(has_candidate=False)
    assert decision.action is AutotuneAction.REFUSE
    assert decision.refusal.severity == "info"


def test_a_stale_wavegen_offers_to_apply_before_autotuning():
    from gan_fet.ui.operations.rig_ops import AutotuneAction

    decision = _autotune(wavegen_pending=True)
    assert decision.action is AutotuneAction.APPLY_WAVEGEN_FIRST
    assert decision.prompt[0] == "Autotune"


def test_a_live_bus_is_refused_before_the_wavegen_is_offered():
    """Applying settings and then refusing to autotune would have moved the
    gate for nothing, with the bus still live."""
    from gan_fet.ui.operations.rig_ops import AutotuneAction

    decision = _autotune(bus_energised=True, wavegen_pending=True)
    assert decision.action is AutotuneAction.REFUSE
