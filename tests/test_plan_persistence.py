"""Test plans survive a restart.

Plans used to live only in memory. Completion never did — that has always come
from the ``runs`` table, recomputed on every read — so losing a plan never lost
a measurement. What it lost was the *intent*: the order, the re-tests, and how
far through a multi-hour sequence the rig had got. Closing the application, or
crashing mid-sequence, meant rebuilding the selection by hand and hoping it
matched.

Two slots are stored, because they answer different questions:

* ``draft``   — the whole matrix the planner is showing, completed points
  included, rewritten on every selection change.
* ``applied`` — the outstanding work, drained as runs complete.

These tests use a real SQLite file. The point is the round trip.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from gan_fet.core.models import MatrixPoint
from gan_fet.storage.schema import PLAN_SLOT_APPLIED, PLAN_SLOT_DRAFT
from gan_fet.ui.plan_store import PlanStore, measured_point


def _point(voltage: int = 200, frequency_hz: int = 6_000_000) -> MatrixPoint:
    return MatrixPoint(
        device_name="EPC2001C",
        config="Single Device",
        frequency_hz=frequency_hz,
        duty_pct=50,
        temperature_c=25,
        voltage_v=voltage,
    )


# -- the storage layer ---------------------------------------------------------


def test_a_saved_plan_comes_back_unchanged(database):
    points = [_point(200), _point(300), _point(400)]
    database.save_plan(
        PLAN_SLOT_APPLIED, points, source="Planner", device_name="EPC2001C"
    )

    restored, source, device, total = database.load_plan(PLAN_SLOT_APPLIED)

    assert restored == points
    assert source == "Planner"
    assert device == "EPC2001C"
    assert total == 3


def test_run_order_survives_the_round_trip(database):
    """The order is part of what was applied. Reading rows back in whatever
    order SQLite felt like returning them would silently reorder a sequence
    that was deliberately grouped by temperature."""
    points = [_point(400), _point(200), _point(300)]
    database.save_plan(
        PLAN_SLOT_APPLIED, points, source="Planner", device_name="EPC2001C"
    )

    restored, _, _, _ = database.load_plan(PLAN_SLOT_APPLIED)

    assert [p.voltage_v for p in restored] == [400, 200, 300]


def test_an_unsaved_slot_reads_as_none(database):
    """None means "no plan", and has to stay distinguishable from a plan with
    nothing left in it."""
    assert database.load_plan(PLAN_SLOT_APPLIED) is None


def test_a_drained_plan_is_empty_but_still_present(database):
    """The state the bench report was actually about: every point measured.
    Returning None here would render as "no test plan applied" and send the
    operator looking for a bug that is not there."""
    database.save_plan(
        PLAN_SLOT_APPLIED, [], source="Planner", device_name="EPC2001C",
        total_count=6,
    )

    stored = database.load_plan(PLAN_SLOT_APPLIED)

    assert stored is not None
    points, _, _, total = stored
    assert points == []
    assert total == 6, "a drained plan still knows how big it was"


def test_saving_replaces_rather_than_appends(database):
    """Two plans at once in one slot is the state this exists to prevent."""
    database.save_plan(
        PLAN_SLOT_APPLIED, [_point(200), _point(300)],
        source="Planner", device_name="EPC2001C",
    )
    database.save_plan(
        PLAN_SLOT_APPLIED, [_point(400)],
        source="Planner", device_name="EPC2001C",
    )

    points, _, _, total = database.load_plan(PLAN_SLOT_APPLIED)

    assert [p.voltage_v for p in points] == [400]
    assert total == 1


def test_the_two_slots_do_not_disturb_each_other(database):
    """The listing shows the whole matrix while the queue drains. Applying,
    or finishing a point, must not rewrite what the planner is displaying."""
    database.save_plan(
        PLAN_SLOT_DRAFT, [_point(200), _point(300), _point(400)],
        source="Planner draft", device_name="EPC2001C",
    )
    database.save_plan(
        PLAN_SLOT_APPLIED, [_point(300)],
        source="Planner", device_name="EPC2001C",
    )

    draft, draft_source, _, _ = database.load_plan(PLAN_SLOT_DRAFT)
    applied, applied_source, _, _ = database.load_plan(PLAN_SLOT_APPLIED)

    assert len(draft) == 3
    assert len(applied) == 1
    assert draft_source != applied_source


def test_clearing_one_slot_leaves_the_other(database):
    database.save_plan(
        PLAN_SLOT_DRAFT, [_point()], source="d", device_name="EPC2001C"
    )
    database.save_plan(
        PLAN_SLOT_APPLIED, [_point()], source="a", device_name="EPC2001C"
    )

    database.clear_plan(PLAN_SLOT_APPLIED)

    assert database.load_plan(PLAN_SLOT_APPLIED) is None
    assert database.load_plan(PLAN_SLOT_DRAFT) is not None


def test_every_field_of_a_point_round_trips(database):
    """A plan that comes back with the wrong duty or temperature would run the
    wrong measurement on live hardware without anything looking amiss."""
    point = MatrixPoint(
        device_name="GS66508B",
        config="Half Bridge",
        frequency_hz=13_560_000,
        duty_pct=35,
        temperature_c=125,
        voltage_v=350,
    )
    database.save_plan(
        PLAN_SLOT_APPLIED, [point], source="Planner", device_name="GS66508B"
    )

    restored, _, _, _ = database.load_plan(PLAN_SLOT_APPLIED)

    assert restored == [point]


# -- the store on top of it ----------------------------------------------------


def test_an_applied_plan_survives_a_restart(database):
    """The whole point. A new PlanStore over the same database stands in for
    reopening the application."""
    points = [_point(200), _point(300)]
    PlanStore(database).apply(points, source="Planner", device_name="EPC2001C")

    reopened = PlanStore(database)
    assert reopened.applied is None, "nothing is loaded until restore() is called"

    restored = reopened.restore()

    assert restored is not None
    assert list(restored.points) == points
    assert restored.source == "Planner"
    assert restored.device_name == "EPC2001C"


def test_a_sequence_resumes_where_it_stopped(database):
    """Points completed before the interruption are gone from the queue, and
    stay gone. Recomputing the remainder from the run table instead would be
    wrong for exactly the plans that matter — a re-test queues points that
    already have runs."""
    a, b, c = _point(200), _point(300), _point(400)
    store = PlanStore(database)
    store.apply([a, b, c], source="Planner", device_name="EPC2001C")
    store.complete_point(a)
    store.complete_point(b)

    resumed = PlanStore(database).restore()

    assert list(resumed.points) == [c]
    assert resumed.total_count == 3, "still reports the size of the whole job"


def test_a_finished_plan_restores_as_finished_not_as_absent(database):
    store = PlanStore(database)
    store.apply([_point(200)], source="Planner", device_name="EPC2001C")
    store.complete_point(_point(200))

    resumed = PlanStore(database).restore()

    assert resumed is not None, "a completed plan is not the same as no plan"
    assert list(resumed.points) == []
    assert resumed.total_count == 1


def test_clearing_a_plan_clears_it_for_good(database):
    store = PlanStore(database)
    store.apply([_point()], source="Planner", device_name="EPC2001C")
    store.clear()

    assert PlanStore(database).restore() is None


def test_restoring_nothing_leaves_the_store_empty(database):
    assert PlanStore(database).restore() is None


def test_applying_replaces_the_stored_plan_too(database):
    """Otherwise a restart would resurrect the plan that was replaced."""
    store = PlanStore(database)
    store.apply([_point(200), _point(300)], source="Planner",
                device_name="EPC2001C")
    store.apply([_point(400)], source="Planner", device_name="EPC2001C")

    resumed = PlanStore(database).restore()

    assert [p.voltage_v for p in resumed.points] == [400]
    assert resumed.total_count == 1, "the replacement's size, not the old one's"


def test_the_device_is_stored_with_the_plan(database):
    """A persisted plan outlives the selection that built it. Reading the
    device from whatever happens to be selected at restore time would attach
    the plan to the wrong part."""
    store = PlanStore(database)
    store.apply([_point()], source="Planner", device_name="EPC2001C")

    assert PlanStore(database).restore().device_name == "EPC2001C"


def test_the_device_falls_back_to_the_points_own(database):
    """Callers that do not pass one still must not store a blank."""
    store = PlanStore(database)
    store.apply([_point()], source="Planner")

    assert store.applied.device_name == "EPC2001C"


def test_a_storage_failure_does_not_refuse_the_apply(database, caplog):
    """The operator is at the bench with a plan in front of them. Losing
    persistence costs them next session; refusing to apply blocks them now.
    It must be logged, though — a silent divergence is worse than either."""

    class Failing:
        def save_plan(self, *_args, **_kwargs):
            raise RuntimeError("disk gone")

        def load_plan(self, _slot):
            return None

        def clear_plan(self, _slot):
            raise RuntimeError("disk gone")

    store = PlanStore(Failing())
    applied = store.apply([_point()], source="Planner")

    assert applied is not None
    assert store.is_applied
    assert "Could not save the test plan" in caplog.text


def test_a_load_failure_leaves_the_store_empty_rather_than_raising(caplog):
    """A corrupt plan must not stop the application starting."""

    class Failing:
        def save_plan(self, *_args, **_kwargs):
            return None

        def load_plan(self, _slot):
            raise RuntimeError("corrupt")

        def clear_plan(self, _slot):
            return None

    store = PlanStore(Failing())

    assert store.restore() is None
    assert not store.is_applied
    assert "Could not load the saved test plan" in caplog.text


def test_completing_a_point_is_persisted_immediately(database):
    """Not flushed at shutdown: the interruption this protects against is the
    one where shutdown never runs."""
    a, b = _point(200), _point(300)
    store = PlanStore(database)
    store.apply([a, b], source="Planner", device_name="EPC2001C")
    store.complete_point(a)

    points, _, _, _ = database.load_plan(PLAN_SLOT_APPLIED)

    assert points == [b]


def test_a_point_matching_on_every_field_but_frequency_is_not_completed(
    database,
):
    """Multi-frequency plans queue the same (config, duty, voltage, temp) at
    several frequencies. Dropping the wrong one would skip a measurement."""
    at_six = _point(200, frequency_hz=6_000_000)
    at_seven = replace(at_six, frequency_hz=7_000_000)
    store = PlanStore(database)
    store.apply([at_six, at_seven], source="Planner", device_name="EPC2001C")

    store.complete_point(at_six)

    assert list(store.applied.points) == [at_seven]


# -- which attempts take work off the queue ------------------------------------


class _Outcome:
    def __init__(self, point):
        self.record = SimpleNamespace(point=point) if point else None


def test_a_successful_run_takes_its_point_off_the_queue():
    assert measured_point(_Outcome(_point(200)), True) == _point(200)


def test_a_failed_run_leaves_the_point_outstanding():
    """``runs`` is append-only: a failed attempt records itself without
    producing a measurement. Removing the point would drop it from a sequence
    that had not done it, and the gap would only surface when the
    characterisation set came up short."""
    assert measured_point(_Outcome(_point(200)), False) is None


def test_a_tripped_run_leaves_the_point_outstanding():
    """A safety trip reports as an unsuccessful run. The point is exactly the
    one that still needs measuring."""
    assert measured_point(_Outcome(_point(200)), success=False) is None


def test_a_run_that_never_produced_a_record_removes_nothing():
    """A run that failed before creating its record has no point to drop, and
    reaching through the missing record would raise inside a UI callback."""
    assert measured_point(_Outcome(None), True) is None


def test_no_outcome_at_all_removes_nothing():
    assert measured_point(None, True) is None


def test_a_failed_run_does_not_shrink_a_stored_plan(database):
    """The end-to-end version: the queue and the file both keep the point."""
    a, b = _point(200), _point(300)
    store = PlanStore(database)
    store.apply([a, b], source="Planner", device_name="EPC2001C")

    store.complete_point(measured_point(_Outcome(a), success=False))

    assert list(store.applied.points) == [a, b]
    stored, _, _, _ = database.load_plan(PLAN_SLOT_APPLIED)
    assert stored == [a, b]
