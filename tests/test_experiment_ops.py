"""Starting a run, and stopping whichever thing is actually running.

Both were decision chains interleaved with ``messagebox`` calls inside the
window. Kept tkinter-free so they can be checked without a display.
"""

from __future__ import annotations

import pytest

from gan_fet.ui.operations.experiment_ops import (
    CancelTarget,
    ExperimentAction,
    cancel_target,
    experiment_precondition,
)


def _decide(**overrides):
    request = {
        "hardware_offline": False,
        "safety_tripped": False,
        "busy": False,
    }
    request.update(overrides)
    return experiment_precondition(**request)


# -- starting -----------------------------------------------------------------


def test_a_clear_rig_proceeds():
    assert _decide().action is ExperimentAction.PROCEED


def test_offline_hardware_is_handed_back_to_the_window():
    """The window owns that path because it also switches to Configuration."""
    assert _decide(hardware_offline=True).action is (
        ExperimentAction.HARDWARE_OFFLINE
    )


def test_a_latched_trip_is_refused():
    decision = _decide(safety_tripped=True)
    assert decision.action is ExperimentAction.REFUSE
    assert decision.refusal is not None
    assert "Reset" in decision.refusal.message


def test_a_busy_rig_is_reported_rather_than_refused_here():
    """The window reports it through the operation coordinator, which knows
    which operation holds the rig and can name it."""
    assert _decide(busy=True).action is ExperimentAction.REPORT_BUSY


def test_a_trip_outranks_a_busy_rig():
    """Busy is temporary and a trip is not. Telling the operator to wait for
    something to finish, when the real problem is a latched interlock they must
    clear, sends them to the wrong place."""
    assert _decide(safety_tripped=True, busy=True).action is (
        ExperimentAction.REFUSE
    )


def test_offline_hardware_outranks_both():
    decision = _decide(hardware_offline=True, safety_tripped=True, busy=True)
    assert decision.action is ExperimentAction.HARDWARE_OFFLINE


def test_the_decision_is_made_before_the_run_request_is_built():
    """Nothing here needs the parameters. Assembling them raises its own errors
    about operator input, and being told the duration is malformed when the
    real problem is a trip is the wrong diagnosis."""
    assert _decide(safety_tripped=True).action is ExperimentAction.REFUSE


# -- cancelling ---------------------------------------------------------------


def test_cancel_stops_a_running_zvs_search():
    assert cancel_target("zvs") is CancelTarget.ZVS_SEARCH


def test_cancel_stops_a_sequence_at_the_sequence_level():
    """Cancelling the engine instead would end the current point and let the
    sequence start the next one — Cancel appearing not to work."""
    assert cancel_target("sequence") is CancelTarget.SEQUENCE


def test_cancel_stops_the_engine_for_a_plain_run():
    assert cancel_target("experiment") is CancelTarget.EXPERIMENT


@pytest.mark.parametrize(
    "kind", [None, "apply_wavegen", "autotune", "bus_off", "report"]
)
def test_anything_else_falls_through_to_the_engine(kind):
    """Including nothing running at all: cancelling an idle engine is
    harmless, and guessing otherwise would leave a real run unstoppable if a
    new operation kind were added and this were not updated."""
    assert cancel_target(kind) is CancelTarget.EXPERIMENT
