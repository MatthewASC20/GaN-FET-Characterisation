"""Test plans survive a restart.

Plans used to live only in memory. Completion never did — that has always come
from the ``runs`` table, recomputed on every read — so losing a plan never lost
a measurement. What it lost was the *intent*: the order, the deliberate
re-tests, and how far through a multi-hour sequence the rig had got. Closing
the application, or crashing mid-sequence, meant rebuilding the selection by
hand and hoping it matched.

Two things are stored, and they are different kinds of thing:

* the **applied plan** — points, in order, each marked when measured. The
  Planned Tests table drains because it shows the unmarked ones; the plan
  itself survives being finished.
* the **planner's selections**, per device — which boxes are ticked, not the
  matrix they expand to.

These tests use a real SQLite file. The point is the round trip.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from gan_fet.core.models import MatrixPoint
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
    database.save_plan(points, source="Planner", device_name="EPC2001C")

    restored, completed, source, device = database.load_plan()

    assert restored == points
    assert completed == set()
    assert source == "Planner"
    assert device == "EPC2001C"


def test_run_order_survives_the_round_trip(database):
    """The order is part of what was applied. Reading rows back in whatever
    order SQLite felt like returning them would silently reorder a sequence
    that was deliberately grouped by temperature."""
    points = [_point(400), _point(200), _point(300)]
    database.save_plan(points, source="Planner", device_name="EPC2001C")

    restored, _, _, _ = database.load_plan()

    assert [p.voltage_v for p in restored] == [400, 200, 300]


def test_no_plan_reads_as_none(database):
    """None means "no plan", and has to stay distinguishable from a plan whose
    points are all measured."""
    assert database.load_plan() is None


def test_a_finished_plan_keeps_its_points(database):
    """The state the bench report was actually about. Deleting as the queue
    drained left nothing to show and nothing to re-apply; marking keeps both."""
    points = [_point(200), _point(300)]
    database.save_plan(
        points, source="Planner", device_name="EPC2001C", completed=[0, 1]
    )

    restored, completed, _, _ = database.load_plan()

    assert restored == points, "the plan is still there after it is finished"
    assert completed == {0, 1}


def test_saving_replaces_rather_than_appends(database):
    """Two plans at once is the state this exists to prevent."""
    database.save_plan(
        [_point(200), _point(300)], source="Planner", device_name="EPC2001C"
    )
    database.save_plan([_point(400)], source="Planner", device_name="EPC2001C")

    points, _, _, _ = database.load_plan()

    assert [p.voltage_v for p in points] == [400]


def test_re_saving_keeps_the_points_already_measured(database):
    """Otherwise persisting any later change would resurrect finished work."""
    points = [_point(200), _point(300)]
    database.save_plan(
        points, source="Planner", device_name="EPC2001C", completed=[0]
    )

    _, completed, _, _ = database.load_plan()

    assert completed == {0}


def test_clearing_removes_the_plan(database):
    database.save_plan([_point()], source="Planner", device_name="EPC2001C")

    database.clear_plan()

    assert database.load_plan() is None


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
    database.save_plan([point], source="Planner", device_name="GS66508B")

    restored, _, _, _ = database.load_plan()

    assert restored == [point]


# -- the store on top of it ----------------------------------------------------


def test_an_applied_plan_survives_a_restart(database):
    """The whole point. A new PlanStore over the same database stands in for
    reopening the application."""
    points = [_point(200), _point(300)]
    PlanStore(database).apply(points, source="Planner", device_name="EPC2001C")

    reopened = PlanStore(database)
    assert reopened.applied is None, "nothing loads until restore() is called"

    restored = reopened.restore()

    assert restored is not None
    assert list(restored.points) == points
    assert restored.source == "Planner"
    assert restored.device_name == "EPC2001C"


def test_a_sequence_resumes_where_it_stopped(database):
    """Points completed before the interruption stay completed. Recomputing
    the remainder from the run table instead would be wrong for exactly the
    plans that matter — a re-test queues points that already have runs."""
    a, b, c = _point(200), _point(300), _point(400)
    store = PlanStore(database)
    store.apply([a, b, c], source="Planner", device_name="EPC2001C")
    store.complete_point(a)
    store.complete_point(b)

    resumed = PlanStore(database).restore()

    assert list(resumed.pending) == [c]
    assert resumed.total_count == 3, "still reports the size of the whole job"
    assert list(resumed.points) == [a, b, c], "and what was in it"


def test_a_finished_plan_restores_as_finished_not_as_absent(database):
    store = PlanStore(database)
    store.apply([_point(200)], source="Planner", device_name="EPC2001C")
    store.complete_point(_point(200))

    resumed = PlanStore(database).restore()

    assert resumed is not None, "a completed plan is not the same as no plan"
    assert list(resumed.pending) == []
    assert resumed.completed_count == 1


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
    store.apply(
        [_point(200), _point(300)], source="Planner", device_name="EPC2001C"
    )
    store.apply([_point(400)], source="Planner", device_name="EPC2001C")

    resumed = PlanStore(database).restore()

    assert [p.voltage_v for p in resumed.points] == [400]


def test_a_replacement_does_not_inherit_the_old_progress(database):
    """Position 0 of the old plan being done says nothing about position 0 of
    the new one."""
    store = PlanStore(database)
    store.apply(
        [_point(200), _point(300)], source="Planner", device_name="EPC2001C"
    )
    store.complete_point(_point(200))
    store.apply([_point(400)], source="Planner", device_name="EPC2001C")

    assert PlanStore(database).restore().completed_count == 0


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

        def load_plan(self):
            return None

        def mark_plan_point_completed(self, _position):
            raise RuntimeError("disk gone")

        def clear_plan(self):
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

        def load_plan(self):
            raise RuntimeError("corrupt")

        def mark_plan_point_completed(self, _position):
            return None

        def clear_plan(self):
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

    _, completed, _, _ = database.load_plan()

    assert completed == {0}


def test_a_point_matching_on_every_field_but_frequency_is_not_completed(
    database,
):
    """Multi-frequency plans queue the same (config, duty, voltage, temp) at
    several frequencies. Marking the wrong one would skip a measurement."""
    at_six = _point(200, frequency_hz=6_000_000)
    at_seven = replace(at_six, frequency_hz=7_000_000)
    store = PlanStore(database)
    store.apply([at_six, at_seven], source="Planner", device_name="EPC2001C")

    store.complete_point(at_six)

    assert list(store.applied.pending) == [at_seven]


def test_a_duplicated_point_is_completed_one_run_at_a_time(database):
    """A plan may deliberately queue the same point twice — a repeatability
    check. One run must tick off one of them."""
    store = PlanStore(database)
    store.apply(
        [_point(200), _point(200)], source="Planner", device_name="EPC2001C"
    )

    store.complete_point(_point(200))

    assert len(store.applied.pending) == 1
    assert PlanStore(database).restore().completed_count == 1


def test_the_second_run_of_a_duplicated_point_completes_the_other_one(
    database,
):
    """Marking the *first* match every time would leave a repeatability check
    permanently one run short, with the queue insisting there is work left
    that has in fact been done twice."""
    store = PlanStore(database)
    store.apply(
        [_point(200), _point(200)], source="Planner", device_name="EPC2001C"
    )

    assert store.complete_point(_point(200)) is True
    assert store.complete_point(_point(200)) is True

    assert store.applied.completed == frozenset({0, 1})
    assert list(store.applied.pending) == []


def test_a_third_run_of_a_duplicated_point_has_nothing_left_to_complete(
    database,
):
    """And must say so, rather than silently re-marking a finished row."""
    store = PlanStore(database)
    store.apply(
        [_point(200), _point(200)], source="Planner", device_name="EPC2001C"
    )
    store.complete_point(_point(200))
    store.complete_point(_point(200))

    assert store.complete_point(_point(200)) is False


def test_progress_survives_a_restart_point_by_point(database):
    """Each completion is written as it happens, not flushed at shutdown: the
    interruption this protects against is the one where shutdown never runs."""
    points = [_point(200), _point(300), _point(400)]
    store = PlanStore(database)
    store.apply(points, source="Planner", device_name="EPC2001C")

    for expected, point in enumerate(points, start=1):
        store.complete_point(point)
        assert PlanStore(database).restore().completed_count == expected


# -- which attempts take work off the queue ------------------------------------


class _Outcome:
    def __init__(self, point):
        self.record = SimpleNamespace(point=point) if point else None


def test_a_successful_run_takes_its_point_off_the_queue():
    assert measured_point(_Outcome(_point(200)), True) == _point(200)


def test_a_failed_run_leaves_the_point_outstanding():
    """``runs`` is append-only: a failed attempt records itself without
    producing a measurement. Marking the point would drop it from a sequence
    that had not done it, and the gap would only surface when the
    characterisation set came up short."""
    assert measured_point(_Outcome(_point(200)), False) is None


def test_a_tripped_run_leaves_the_point_outstanding():
    """A safety trip reports as an unsuccessful run. The point is exactly the
    one that still needs measuring."""
    assert measured_point(_Outcome(_point(200)), success=False) is None


def test_a_run_that_never_produced_a_record_marks_nothing():
    """A run that failed before creating its record has no point to mark, and
    reaching through the missing record would raise inside a UI callback."""
    assert measured_point(_Outcome(None), True) is None


def test_no_outcome_at_all_marks_nothing():
    assert measured_point(None, True) is None


def test_a_failed_run_does_not_shrink_a_stored_plan(database):
    """The end-to-end version: the queue and the file both keep the point."""
    a, b = _point(200), _point(300)
    store = PlanStore(database)
    store.apply([a, b], source="Planner", device_name="EPC2001C")

    store.complete_point(measured_point(_Outcome(a), success=False))

    assert list(store.applied.pending) == [a, b]
    _, completed, _, _ = database.load_plan()
    assert completed == set()


# -- the planner's selections --------------------------------------------------


def test_selections_round_trip(database):
    database.save_plan_selections(
        "EPC2001C",
        {"frequencies": [6_000_000, 6_500_000], "duties": [50]},
        include_completed=True,
        duration_minutes=2.5,
        tune_voltage=False,
    )

    selections, include_completed, duration, tune_voltage = (
        database.load_plan_selections("EPC2001C")
    )

    assert selections["frequencies"] == {"6000000", "6500000"}
    assert selections["duties"] == {"50"}
    assert include_completed is True
    assert duration == 2.5
    assert tune_voltage is False


def test_selections_are_per_device(database):
    """Coming back to a part should bring back the matrix being worked on for
    it, not whatever was selected for a different one."""
    database.save_plan_selections("EPC2001C", {"duties": [50]})
    database.save_plan_selections("GS66508B", {"duties": [35, 40]})

    epc, _, _, _ = database.load_plan_selections("EPC2001C")
    gs, _, _, _ = database.load_plan_selections("GS66508B")

    assert epc["duties"] == {"50"}
    assert gs["duties"] == {"35", "40"}


def test_a_device_never_planned_for_reads_as_none(database):
    """Which the planner treats as "select everything" — its existing default.
    Returning empty selections instead would open the planner with nothing
    ticked and no plan."""
    assert database.load_plan_selections("EPC2001C") is None


def test_saving_selections_replaces_the_previous_set(database):
    database.save_plan_selections("EPC2001C", {"duties": [40, 45, 50]})
    database.save_plan_selections("EPC2001C", {"duties": [50]})

    selections, _, _, _ = database.load_plan_selections("EPC2001C")

    assert selections["duties"] == {"50"}


def test_a_deliberately_empty_selection_is_kept(database):
    """Distinct from never having planned: the operator cleared a box."""
    database.save_plan_selections("EPC2001C", {"duties": [50]})
    database.save_plan_selections("EPC2001C", {"duties": []})

    stored = database.load_plan_selections("EPC2001C")

    assert stored is not None, "still a saved state, just an empty one"
    assert stored[0].get("duties", set()) == set()


def test_options_default_sensibly_when_not_passed(database):
    database.save_plan_selections("EPC2001C", {"duties": [50]})

    _, include_completed, duration, tune_voltage = database.load_plan_selections(
        "EPC2001C"
    )

    assert include_completed is False
    assert duration == 1.0
    assert tune_voltage is True


# -- upgrading a schema 7 database ---------------------------------------------


_SCHEMA_7_PLAN_TABLES = """
CREATE TABLE plan_points (
    slot TEXT NOT NULL,
    position INTEGER NOT NULL,
    device_name TEXT NOT NULL,
    config TEXT NOT NULL,
    frequency_hz INTEGER NOT NULL,
    duty_pct INTEGER NOT NULL,
    temperature_c INTEGER NOT NULL,
    voltage_v INTEGER NOT NULL,
    PRIMARY KEY (slot, position)
);
CREATE TABLE plan_meta (
    slot TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    device_name TEXT NOT NULL,
    total_count INTEGER NOT NULL DEFAULT 0,
    saved_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _schema_7_database(tmp_path, *, applied=(), draft=()):
    """A database at schema 7: two plans in one table, keyed by ``slot``."""
    import sqlite3

    from gan_fet.storage.schema import SCHEMA

    path = tmp_path / "legacy" / "gan_fet.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    # Everything except the plan tables, which are recreated in the old shape.
    conn.executescript(SCHEMA.split("-- The applied test plan")[0])
    conn.executescript(_SCHEMA_7_PLAN_TABLES)
    conn.execute("INSERT INTO schema_version(version) VALUES (7)")
    for slot, points in (("applied", applied), ("draft", draft)):
        for position, point in enumerate(points):
            conn.execute(
                "INSERT INTO plan_points VALUES (?,?,?,?,?,?,?,?)",
                (
                    slot, position, point.device_name, point.config,
                    point.frequency_hz, point.duty_pct, point.temperature_c,
                    point.voltage_v,
                ),
            )
        if points:
            conn.execute(
                "INSERT INTO plan_meta VALUES (?,?,?,?,datetime('now'))",
                (slot, f"Planner {slot}", "EPC2001C", len(points)),
            )
    conn.commit()
    conn.close()
    return path


def test_a_schema_7_applied_plan_is_carried_across(tmp_path):
    """It is the operator's actual queue. Losing it to an upgrade would mean
    rebuilding a multi-hour plan by hand."""
    from gan_fet.storage.db import Database

    points = [_point(200), _point(300), _point(400)]
    path = _schema_7_database(tmp_path, applied=points, draft=[_point(999)])

    db = Database(path)
    try:
        restored = PlanStore(db).restore()
        assert list(restored.points) == points
        assert restored.device_name == "EPC2001C"
    finally:
        db.close()


def test_the_schema_7_draft_is_discarded(tmp_path):
    """Nothing ever read it, and plan_selections replaces it. Carrying it
    across would leave a table full of rows with no reader."""
    from gan_fet.storage.db import Database

    path = _schema_7_database(
        tmp_path, applied=[_point(200)], draft=[_point(v) for v in range(1, 20)]
    )

    db = Database(path)
    try:
        total = db._conn.execute(
            "SELECT COUNT(*) FROM plan_points"
        ).fetchone()[0]
        assert total == 1
    finally:
        db.close()


def test_the_slot_column_is_gone_after_upgrading(tmp_path):
    from gan_fet.storage.db import Database

    path = _schema_7_database(tmp_path, applied=[_point()])

    db = Database(path)
    try:
        columns = {
            row[1] for row in db._conn.execute("PRAGMA table_info(plan_points)")
        }
        assert "slot" not in columns
        assert "completed_at" in columns
    finally:
        db.close()


def test_upgrading_a_database_with_no_plan_is_harmless(tmp_path):
    from gan_fet.storage.db import Database

    path = _schema_7_database(tmp_path)

    db = Database(path)
    try:
        assert db.load_plan() is None
    finally:
        db.close()


def test_reopening_an_upgraded_database_does_not_rebuild_again(tmp_path):
    """The migration is keyed on the slot column being present, so a second
    open must be a no-op rather than dropping the plan it just carried."""
    from gan_fet.storage.db import Database

    path = _schema_7_database(tmp_path, applied=[_point(200), _point(300)])

    db = Database(path)
    db.close()
    db = Database(path)
    try:
        points, _, _, _ = db.load_plan()
        assert [p.voltage_v for p in points] == [200, 300]
    finally:
        db.close()
