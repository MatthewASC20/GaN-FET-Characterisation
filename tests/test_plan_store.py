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

LIVE = "Next 5 test(s) to execute (12 total pending)"


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


def test_with_no_applied_plan_the_heading_is_the_live_one():
    assert queue_heading(None, 12, 5, LIVE) == LIVE


def test_an_applied_plan_says_so_and_says_selections_are_ignored():
    """The operator will change a parameter, see the queue not move, and
    otherwise conclude the application is broken."""
    store = PlanStore()
    applied = store.apply([_point()] * 10, source="Planner")
    heading = queue_heading(applied, 7, 5, LIVE)
    assert "Applied plan" in heading
    assert "Planner" in heading
    assert "ignored" in heading


def test_an_applied_plan_shows_how_much_is_left_of_how_much():
    store = PlanStore()
    applied = store.apply([_point()] * 10, source="Planner")
    heading = queue_heading(applied, 7, 5, LIVE)
    assert "next 5 of 7" in heading
    assert "10 total" in heading


def test_a_finished_applied_plan_says_it_is_complete():
    """Not "0 remaining", which reads like something went wrong."""
    store = PlanStore()
    applied = store.apply([_point()] * 4, source="Planner")
    assert "complete" in queue_heading(applied, 0, 0, LIVE)


# -- what is left of an applied plan ------------------------------------------


def _key(point):
    return (
        point.frequency_hz,
        point.config,
        point.duty_pct,
        point.voltage_v,
        point.temperature_c,
    )


def test_nothing_measured_leaves_the_whole_plan():
    store = PlanStore()
    applied = store.apply([_point(200), _point(300)], source="Planner")
    assert len(pending_points(applied, set())) == 2


def test_measured_points_are_dropped_from_the_queue():
    """A plan half-finished in an earlier session must not look like it is
    about to repeat itself."""
    done, todo = _point(200), _point(300)
    store = PlanStore()
    applied = store.apply([done, todo], source="Planner")
    remaining = pending_points(applied, {_key(done)})
    assert remaining == [todo]


def test_a_fully_measured_plan_leaves_nothing():
    store = PlanStore()
    points = [_point(200), _point(300)]
    applied = store.apply(points, source="Planner")
    assert pending_points(applied, {_key(p) for p in points}) == []


def test_order_is_preserved_so_the_queue_matches_the_run_order():
    """The queue is a promise about what happens next, in what order."""
    a, b, c = _point(200), _point(300), _point(400)
    store = PlanStore()
    applied = store.apply([a, b, c], source="Planner")
    assert pending_points(applied, {_key(b)}) == [a, c]


def test_a_point_differing_only_in_voltage_is_not_treated_as_done():
    """Every field of the key matters; matching on too few would silently skip
    measurements the operator asked for."""
    store = PlanStore()
    applied = store.apply([_point(300)], source="Planner")
    assert len(pending_points(applied, {_key(_point(200))})) == 1


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
    contents = applied_queue(applied, set())
    assert contents.rows == [a, b, c]
    assert "Applied plan" in contents.heading


def test_an_applied_queue_is_capped_but_the_heading_says_the_total():
    """Five rows fit; the operator still needs to know how many there are."""
    from gan_fet.ui.plan_store import applied_queue

    store = PlanStore()
    applied = store.apply([_point(v) for v in range(100, 900, 100)], source="Planner")
    contents = applied_queue(applied, set())
    assert len(contents.rows) == 5
    assert "next 5 of 8" in contents.heading


def test_a_fully_measured_applied_queue_says_complete_rather_than_going_blank():
    """A blank table with no explanation is indistinguishable from a bug —
    which is exactly how this was reported from the bench."""
    from gan_fet.ui.plan_store import applied_queue

    points = [_point(200), _point(300)]
    store = PlanStore()
    applied = store.apply(points, source="Planner")
    contents = applied_queue(applied, {_key(p) for p in points})
    assert contents.rows == []
    assert "complete" in contents.heading
    assert contents.heading, "an empty queue must still say something"


def test_clearing_explains_that_the_live_matrix_is_not_a_plan():
    """Reported from the bench: an eighteen-row queue looks like a plan, so
    "no test plan is applied" reads as the button being broken."""
    decision = _clear()
    assert "live matrix" in decision.message
    assert "not a plan" in decision.message
