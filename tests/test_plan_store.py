"""The one test plan the rig works from.

Three independent plans used to exist, and the case where they diverged was
invisible: a planned sequence ran the planner's points while the Experiment
tab's queue displayed a list rebuilt from its own selectors — and the window
switched to that queue the moment the sequence started. These tests hold the
single-source arrangement that replaced it.
"""

from __future__ import annotations

from gan_fet.core.models import MatrixPoint
from gan_fet.ui.plan_store import (
    ApplyAction,
    PlanStore,
    apply_plan_decision,
    pending_points,
    queue_heading,
)

def _point(voltage: int = 200) -> MatrixPoint:
    return MatrixPoint(
        device_name="EPC2001C",
        config="Single Device",
        frequency_hz=6_000_000,
        duty_pct=50,
        temperature_c=25,
        voltage_v=voltage,
    )


# -- the store ----------------------------------------------------------------


def test_nothing_is_applied_to_begin_with():
    """The Experiment tab's selectors keep driving the queue until the
    operator deliberately commits to a plan."""
    store = PlanStore()
    assert not store.is_applied
    assert store.applied is None


def test_applying_stores_the_points_and_where_they_came_from():
    store = PlanStore()
    applied = store.apply([_point(200), _point(300)], source="Planner")
    assert store.is_applied
    assert len(applied) == 2
    assert applied.source == "Planner"


def test_applying_again_replaces_rather_than_appends():
    """Two plans at once is the state this exists to make impossible."""
    store = PlanStore()
    store.apply([_point(200), _point(300)], source="Planner")
    store.apply([_point(400)], source="Planner")
    assert len(store.applied) == 1


def test_clearing_goes_back_to_the_live_matrix():
    store = PlanStore()
    store.apply([_point()], source="Planner")
    store.clear()
    assert not store.is_applied


def test_the_stored_points_cannot_be_changed_from_outside():
    """The queue and the sequence both read this. A caller mutating the list
    afterwards would make them disagree again."""
    points = [_point(200), _point(300)]
    store = PlanStore()
    applied = store.apply(points, source="Planner")
    points.append(_point(400))
    assert len(applied) == 2


# -- deciding what Apply does -------------------------------------------------


def _decide(**overrides):
    request = {
        "point_count": 5,
        "sequence_running": False,
        "existing": None,
    }
    request.update(overrides)
    return apply_plan_decision(**request)


def test_a_clear_state_applies_without_asking():
    assert _decide().action is ApplyAction.APPLY


def test_an_empty_plan_is_refused_with_the_reason():
    """Applying nothing would silently blank the queue."""
    decision = _decide(point_count=0)
    assert decision.action is ApplyAction.REFUSE
    assert "already been measured" in decision.message


def test_an_existing_plan_is_replaced_only_after_confirming():
    store = PlanStore()
    existing = store.apply([_point()] * 3, source="Planner")
    decision = _decide(existing=existing)
    assert decision.action is ApplyAction.CONFIRM_REPLACE
    assert "3 points" in decision.message
    assert "5 points" in decision.message


def test_a_running_sequence_is_offered_a_stop_rather_than_refused():
    """Refusing would leave the operator with no way to change course without
    hunting for the stop button."""
    decision = _decide(sequence_running=True)
    assert decision.action is ApplyAction.CONFIRM_STOP_AND_APPLY
    assert "Stop it and apply" in decision.message


def test_the_stop_prompt_says_completed_points_are_kept():
    """Otherwise stopping looks like it throws away the work already done."""
    decision = _decide(sequence_running=True)
    assert "stay recorded" in decision.message


def test_a_running_sequence_outranks_an_existing_plan():
    """Replacing the stored plan mid-run would leave the queue describing
    something the running sequence is not executing — the exact failure this
    change exists to remove."""
    store = PlanStore()
    existing = store.apply([_point()], source="Planner")
    decision = _decide(sequence_running=True, existing=existing)
    assert decision.action is ApplyAction.CONFIRM_STOP_AND_APPLY


def test_an_empty_plan_is_refused_even_mid_sequence():
    """There is nothing to stop the sequence *for*."""
    assert _decide(point_count=0, sequence_running=True).action is (
        ApplyAction.REFUSE
    )


# -- what the queue heading says ----------------------------------------------


def test_with_no_applied_plan_the_queue_says_to_build_one():
    """There is no computed fallback any more. An empty queue means nothing
    has been asked for, which is different from having finished."""
    heading = queue_heading(None, 0)
    assert "No test plan applied" in heading
    assert "Test Planner" in heading


def _plan(count: int, done: int = 0, source: str = "Planner", device: str = ""):
    """An applied plan of ``count`` points with the first ``done`` measured."""
    store = PlanStore()
    store.apply(
        [_point(100 + 10 * i) for i in range(count)],
        source=source,
        device_name=device or "EPC2001C",
    )
    for i in range(done):
        store.complete_point(_point(100 + 10 * i))
    return store.applied


def test_an_applied_plan_names_where_it_came_from():
    assert "Planner" in queue_heading(_plan(10, done=3))


def test_a_partly_finished_plan_reports_both_numbers():
    """"7 left" alone loses the size of the job; "10 planned" alone hides the
    progress. The operator is deciding whether to leave it running."""
    heading = queue_heading(_plan(10, done=3))
    assert "7 of 10" in heading
    assert "3 completed" in heading


def test_an_untouched_plan_does_not_claim_zero_completed():
    """Saying "0 completed" reads as though something was lost."""
    heading = queue_heading(_plan(4))
    assert "4 test(s) to run" in heading
    assert "completed" not in heading


def test_a_finished_applied_plan_says_it_is_complete():
    """Not "0 remaining", which reads like something went wrong."""
    heading = queue_heading(_plan(4, done=4))
    assert "complete" in heading
    assert "4" in heading, "a finished plan must still say how big it was"


def test_a_finished_plan_still_knows_what_was_in_it():
    """Marking rather than deleting is what makes this answerable — and what
    lets the same plan be applied to the next part."""
    applied = _plan(2, done=2)
    assert applied.total_count == 2
    assert applied.completed_count == 2
    assert len(applied.points) == 2
    assert "all 2 points measured" in queue_heading(applied)


# -- a plan built for another device -------------------------------------------


def test_a_plan_for_another_device_says_so_instead_of_listing_work():
    """Plans persist now, so one can outlive its device selection by days.
    Listing points that will be refused is worse than saying why."""
    heading = queue_heading(_plan(5, device="EPC2001C"), "GS66508B")
    assert "EPC2001C" in heading
    assert "GS66508B" in heading


def test_the_matching_device_reads_normally():
    assert "5 test(s) to run" in queue_heading(
        _plan(5, device="EPC2001C"), "EPC2001C"
    )


def test_an_unnamed_plan_matches_any_device():
    """Plans stored before the device was recorded must not be locked out."""
    store = PlanStore()
    applied = store.apply([], source="Planner", device_name="")
    assert applied.is_for("anything")


def test_no_selected_device_does_not_trigger_the_mismatch():
    """Startup order: the plan restores before the device selector is set."""
    assert "3 test(s) to run" in queue_heading(_plan(3, device="EPC2001C"), "")


def test_starting_a_plan_for_another_device_is_refused():
    """The one that matters. Running EPC2001C's plan with GS66508B mounted
    would drive it with the wrong frequencies and voltages and record the
    results against GS66508B — nothing about the data would look wrong."""
    from gan_fet.ui.plan_store import StartAction

    decision = _start(
        applied=_plan(5, device="EPC2001C"),
        pending=5,
        selected_device="GS66508B",
    )
    assert decision.action is StartAction.WRONG_DEVICE
    assert "EPC2001C" in decision.message
    assert "GS66508B" in decision.message


def test_the_wrong_device_outranks_the_plan_being_finished():
    """Otherwise the operator is told to apply a new plan when the real
    problem is which part is mounted."""
    from gan_fet.ui.plan_store import StartAction

    decision = _start(
        applied=_plan(2, done=2, device="EPC2001C"),
        pending=0,
        selected_device="GS66508B",
    )
    assert decision.action is StartAction.WRONG_DEVICE


def test_the_right_device_starts_normally():
    from gan_fet.ui.plan_store import StartAction

    decision = _start(
        applied=_plan(5, device="EPC2001C"),
        pending=5,
        selected_device="EPC2001C",
    )
    assert decision.action is StartAction.START


# -- what is left of an applied plan ------------------------------------------


def _key(point):
    return (
        point.frequency_hz,
        point.config,
        point.duty_pct,
        point.voltage_v,
        point.temperature_c,
    )


def test_the_queue_is_the_stored_plan():
    """No longer recomputed by subtracting the run table: the plan is drained
    as runs finish, so what is stored is what is left."""
    store = PlanStore()
    applied = store.apply([_point(200), _point(300)], source="Planner")
    assert len(pending_points(applied)) == 2


def test_completing_a_point_removes_it():
    """A plan half-finished must not look like it is about to repeat itself."""
    done, todo = _point(200), _point(300)
    store = PlanStore()
    store.apply([done, todo], source="Planner")
    assert store.complete_point(done) is True
    assert pending_points(store.applied) == [todo]


def test_completing_every_point_leaves_an_empty_but_present_plan():
    """Empty is not the same as absent: the operator finished the job, and
    "no plan applied" would misreport that."""
    store = PlanStore()
    points = [_point(200), _point(300)]
    store.apply(points, source="Planner")
    for point in points:
        store.complete_point(point)
    assert store.is_applied
    assert pending_points(store.applied) == []


def test_order_is_preserved_so_the_queue_matches_the_run_order():
    """The queue is a promise about what happens next, in what order."""
    a, b, c = _point(200), _point(300), _point(400)
    store = PlanStore()
    store.apply([a, b, c], source="Planner")
    store.complete_point(b)
    assert pending_points(store.applied) == [a, c]


def test_a_point_differing_only_in_voltage_is_not_treated_as_done():
    """Every field of the key matters; matching on too few would silently skip
    measurements the operator asked for."""
    store = PlanStore()
    store.apply([_point(300)], source="Planner")
    assert store.complete_point(_point(200)) is False
    assert len(pending_points(store.applied)) == 1


def test_completing_a_point_that_is_not_in_the_plan_changes_nothing():
    """Manual runs happen alongside a plan. One must not be able to shrink a
    queue it was never part of."""
    store = PlanStore()
    store.apply([_point(200)], source="Planner")
    assert store.complete_point(_point(999)) is False
    assert len(store.applied) == 1


def test_completing_with_no_plan_applied_is_harmless():
    """Ordinary one-off runs go through the same completion path."""
    store = PlanStore()
    assert store.complete_point(_point()) is False
    assert not store.is_applied


def test_applying_does_not_filter_out_points_that_already_have_runs():
    """"Include Completed Runs (Re-test)" means the operator asked for them
    deliberately. Filtering here would drain a re-test plan to nothing before
    it could start."""
    store = PlanStore()
    points = [_point(200), _point(300)]
    applied = store.apply(points, source="Planner")
    assert len(applied) == 2
    assert applied.total_count == 2


# -- clearing the applied plan -------------------------------------------------


def _clear(**overrides):
    from gan_fet.ui.plan_store import clear_plan_decision

    request = {"sequence_running": False, "existing": None}
    request.update(overrides)
    return clear_plan_decision(**request)


def test_clearing_with_nothing_applied_says_so():
    """Silently doing nothing would look like the button is broken."""
    from gan_fet.ui.plan_store import ClearAction

    decision = _clear()
    assert decision.action is ClearAction.NOTHING_TO_CLEAR
    assert "No test plan is applied" in decision.message


def test_clearing_an_idle_plan_needs_no_confirmation():
    from gan_fet.ui.plan_store import ClearAction

    store = PlanStore()
    assert _clear(existing=store.apply([_point()], source="Planner")).action is (
        ClearAction.CLEAR
    )


def test_clearing_mid_sequence_is_offered_as_a_stop():
    """The running sequence holds its own copy of the point list, so clearing
    the store alone would leave the queue showing the live matrix while a plan
    was still executing."""
    from gan_fet.ui.plan_store import ClearAction

    store = PlanStore()
    decision = _clear(
        sequence_running=True, existing=store.apply([_point()], source="Planner")
    )
    assert decision.action is ClearAction.CONFIRM_STOP_AND_CLEAR
    assert "Stop it and clear" in decision.message
    assert "stay recorded" in decision.message


def test_a_running_sequence_with_no_applied_plan_still_has_nothing_to_clear():
    """It is running the live matrix, which clearing does not affect."""
    from gan_fet.ui.plan_store import ClearAction

    assert _clear(sequence_running=True).action is ClearAction.NOTHING_TO_CLEAR


# -- what the queue renders ----------------------------------------------------


def test_an_applied_queue_shows_the_pending_points_in_order():
    from gan_fet.ui.plan_store import applied_queue

    a, b, c = _point(200), _point(300), _point(400)
    store = PlanStore()
    applied = store.apply([a, b, c], source="Planner")
    contents = applied_queue(applied)
    assert [row.point for row in contents.rows] == [a, b, c]
    assert "3 test(s) to run, in order" in contents.heading


def test_a_fully_measured_applied_queue_says_complete_rather_than_going_blank():
    """A blank table with no explanation is indistinguishable from a bug —
    which is exactly how this was reported from the bench."""
    from gan_fet.ui.plan_store import applied_queue

    points = [_point(200), _point(300)]
    store = PlanStore()
    store.apply(points, source="Planner")
    for point in points:
        store.complete_point(point)
    contents = applied_queue(store.applied)
    assert contents.rows == []
    assert "complete" in contents.heading
    assert contents.heading, "an empty queue must still say something"


def test_clearing_says_where_a_plan_would_come_from():
    """Reported from the bench: the queue looked like it already had a plan,
    so "no test plan is applied" read as the button being broken. There is
    nothing to mistake for a plan now, but the message still has to point at
    the one place a plan comes from."""
    decision = _clear()
    assert "nothing to clear" in decision.message
    assert "Test Planner" in decision.message


# -- starting a sequence -------------------------------------------------------


def _start(**overrides):
    from gan_fet.ui.plan_store import start_sequence_decision

    request = {"applied": None, "pending": 0}
    request.update(overrides)
    return start_sequence_decision(**request)


def test_starting_with_no_plan_offers_the_planner_rather_than_just_refusing():
    """The queue is fed from another tab. Saying only "no" leaves the operator
    to work that out for themselves."""
    from gan_fet.ui.plan_store import StartAction

    decision = _start()
    assert decision.action is StartAction.OFFER_PLANNER
    assert "Open the Test Planner" in decision.message


def test_starting_an_applied_plan_with_work_left_just_starts():
    from gan_fet.ui.plan_store import StartAction

    store = PlanStore()
    applied = store.apply([_point()] * 3, source="Planner")
    assert _start(applied=applied, pending=3).action is StartAction.START


def test_starting_a_finished_plan_says_so_rather_than_offering_the_planner():
    """A finished plan is not a missing one, and the operator has a choice to
    make about it — apply a new one, or clear this."""
    from gan_fet.ui.plan_store import StartAction

    store = PlanStore()
    applied = store.apply([_point()] * 3, source="Planner")
    decision = _start(applied=applied, pending=0)
    assert decision.action is StartAction.ALREADY_COMPLETE
    assert "already been measured" in decision.message
    assert "clear this one" in decision.message


# -- how much of the plan is shown ---------------------------------------------


def test_the_table_shows_every_remaining_point():
    """A plan you can only see the first five of does not answer "what did I
    just apply"."""
    from gan_fet.ui.plan_store import applied_queue

    store = PlanStore()
    applied = store.apply([_point(v) for v in range(100, 2100, 100)], source="Planner")
    contents = applied_queue(applied)
    assert len(contents.rows) == 20


def test_every_queued_row_is_pending():
    """Measured points are deleted from the queue, not marked in it, so a
    "Completed" row here would mean something has gone wrong upstream."""
    from gan_fet.ui.plan_store import applied_queue

    store = PlanStore()
    applied = store.apply([_point(200), _point(300)], source="Planner")
    assert all(not row.is_completed for row in applied_queue(applied).rows)


def test_the_running_point_is_marked_and_only_that_point():
    from gan_fet.ui.plan_store import applied_queue

    a, b, c = _point(200), _point(300), _point(400)
    store = PlanStore()
    applied = store.apply([a, b, c], source="Planner")

    rows = applied_queue(applied, running_point=b).rows
    assert [row.is_running for row in rows] == [False, True, False]


def test_without_a_running_point_no_row_claims_to_be_running():
    from gan_fet.ui.plan_store import applied_queue

    store = PlanStore()
    applied = store.apply([_point(200), _point(300)], source="Planner")
    assert all(not row.is_running for row in applied_queue(applied).rows)


def test_a_point_queued_twice_shows_one_running_row():
    """A plan may deliberately queue the same point twice, and the rig is on
    one of them — the same convention complete_point uses to tick off one
    occurrence per run."""
    from gan_fet.ui.plan_store import applied_queue

    point = _point(200)
    store = PlanStore()
    applied = store.apply([point, point], source="Planner")

    rows = applied_queue(applied, running_point=point).rows
    assert [row.is_running for row in rows] == [True, False]
