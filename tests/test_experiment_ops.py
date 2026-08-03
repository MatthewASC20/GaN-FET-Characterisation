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
    run_finished_report,
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
    assert cancel_target("voltage_tune") is CancelTarget.VOLTAGE_TUNE


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


# -- how a finished run is reported -------------------------------------------

def test_an_engine_message_wins_over_the_generic_text():
    """The engine knows which point, which trip, which instrument stopped
    answering. The fallback knows none of that."""
    report = run_finished_report(
        success=False, message="Tripped: overcurrent at 6.83 MHz"
    )
    assert report.status == "Tripped: overcurrent at 6.83 MHz"


def test_a_silent_success_reads_as_complete():
    assert run_finished_report(success=True, message="").status == (
        "Experiment complete!"
    )


def test_a_silent_failure_reads_as_stopped_not_failed():
    """The common way for a run to end without success is the operator
    cancelling it. Reporting that as a failure would be wrong."""
    assert run_finished_report(success=False, message="").status == "Stopped."


def test_an_ordinary_run_raises_no_dialog():
    assert run_finished_report(success=True, message="").dialog is None


# -- the simulated validation run ---------------------------------------------


def _validation(**overrides):
    fields = {
        "success": True,
        "message": "",
        "validation": True,
        "sample_count": 42,
        "data_dir": "/data/simulation",
        "has_screenshot": True,
    }
    fields.update(overrides)
    return run_finished_report(**fields)


def test_a_successful_validation_reports_what_was_saved_and_where():
    report = _validation()
    assert "42 samples" in report.status
    assert "/data/simulation" in report.status
    assert report.dialog is not None
    title, body = report.dialog
    assert title == "Simulation Validation Complete"
    assert "42 samples" in body and "/data/simulation" in body


def test_a_failed_validation_is_not_announced_as_complete():
    """The one outcome here that could actually mislead someone about whether
    the rig works. It gets the ordinary report and no dialog."""
    report = _validation(success=False, message="")
    assert report.dialog is None
    assert report.status == "Stopped."
    assert "validation" not in report.status.lower()


def test_a_failed_validation_still_shows_why_it_failed():
    report = _validation(success=False, message="Tripped: overcurrent")
    assert report.status == "Tripped: overcurrent"


def test_the_screenshot_note_says_which_way_it_went():
    assert "was saved" in _validation(has_screenshot=True).dialog[1]
    assert "No simulated scope capture" in (
        _validation(has_screenshot=False).dialog[1]
    )


def test_a_validation_that_stored_nothing_says_zero_rather_than_nothing():
    """Silence about the sample count would read as success."""
    report = _validation(sample_count=0)
    assert "0 samples" in report.status


def test_a_non_validation_run_never_gets_the_validation_dialog():
    assert run_finished_report(
        success=True, message="", validation=False, sample_count=42
    ).dialog is None
