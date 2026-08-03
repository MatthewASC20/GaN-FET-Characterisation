"""The Qt planner pane and Planned Tests queue, offscreen.

Skipped wholesale when PyQt6 is not installed. Real widgets over a real
temporary :class:`Database`: the listing, the persistence round-trip and
the queue headings are exercised through the same policy modules the Tk
front-end uses, so a pass means both front-ends are reading one plan.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from gan_fet.core.sequence import build_matrix_plan  # noqa: E402
from gan_fet.settings import Settings  # noqa: E402
from gan_fet.storage.db import Database  # noqa: E402
from gan_fet.ui.plan_store import NO_PLAN_HEADING, PlanStore  # noqa: E402
from gan_fet.ui_qt.planner import PlannerPane, QueueView  # noqa: E402

DEVICE = "QT-DUT"


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "planner.db")
    yield database
    database.close()


@pytest.fixture()
def settings():
    return Settings()


def make_pane(db, settings):
    """A pane plus the record of every plan handed to on_apply_plan."""
    applied: list = []

    def on_apply_plan(plan):
        applied.append(plan)
        return True

    pane = PlannerPane(db, settings, lambda: DEVICE, on_apply_plan)
    return pane, applied


def select_first(pane, key: str, count: int = 1) -> None:
    """Tick only the first ``count`` options of one axis, as a user would.

    Item clicks fire ``itemSelectionChanged``, so this drives the live
    rebuild path rather than the silent restore path.
    """
    box = pane.select_boxes[key]
    box.clear_selection()
    for index in range(count):
        item = box.list.item(index)
        assert item is not None
        item.setSelected(True)


def test_listing_row_count_matches_build_matrix_plan(
    qapp, db, settings
) -> None:
    pane, _applied = make_pane(db, settings)

    select_first(pane, "frequencies", 1)
    select_first(pane, "configurations", 2)
    select_first(pane, "duties", 1)
    select_first(pane, "voltages", 2)
    select_first(pane, "temperatures", 1)

    expected = build_matrix_plan(
        db=db,
        device_name=DEVICE,
        frequencies_hz=[settings.default_frequencies[0]],
        configs=list(settings.default_configurations[:2]),
        duties=[settings.default_duties[0]],
        voltages=list(settings.default_voltages[:2]),
        temperatures=[settings.default_temperatures[0]],
        include_completed=False,
        duration_minutes_per_point=1.0,
    )
    assert expected.total_count == 4
    assert pane.plan_table.rowCount() == len(expected.points)
    assert pane.current_plan is not None
    assert pane.current_plan.total_count == expected.total_count
    assert pane.current_plan.all_points == expected.all_points


def test_clearing_an_axis_empties_the_listing(qapp, db, settings) -> None:
    pane, _applied = make_pane(db, settings)
    pane.select_boxes["voltages"].clear_selection()
    assert pane.current_plan is None
    assert pane.plan_table.rowCount() == 0
    assert "Select at least one option" in pane.summary_label.text()


def test_apply_hands_the_current_plan_to_the_callback(
    qapp, db, settings
) -> None:
    pane, applied = make_pane(db, settings)
    select_first(pane, "frequencies", 1)
    select_first(pane, "temperatures", 1)

    pane.apply_button.click()

    assert len(applied) == 1
    plan = applied[0]
    assert plan is not None and plan is pane.current_plan
    assert len(plan.points) == pane.plan_table.rowCount() > 0


def test_selections_persist_and_a_new_pane_restores_them(
    qapp, db, settings
) -> None:
    pane, _applied = make_pane(db, settings)
    select_first(pane, "frequencies", 1)
    select_first(pane, "voltages", 2)
    pane.include_completed_check.setChecked(True)
    pane.duration_entry.setText("2.5")
    pane.tune_voltage_check.setChecked(False)
    pane.generate_plan(show_errors=False)

    stored = db.load_plan_selections(DEVICE)
    assert stored is not None
    selections, include_completed, duration, tune_voltage = stored
    assert selections["frequencies"] == {
        str(settings.default_frequencies[0])
    }
    assert selections["voltages"] == {
        str(v) for v in settings.default_voltages[:2]
    }
    assert include_completed is True
    assert duration == 2.5
    assert tune_voltage is False

    fresh, _applied2 = make_pane(db, settings)
    assert fresh.select_boxes["frequencies"].get_selected_values() == [
        settings.default_frequencies[0]
    ]
    assert fresh.select_boxes["voltages"].get_selected_values() == list(
        settings.default_voltages[:2]
    )
    # Axes that were untouched come back fully selected, as saved.
    assert fresh.select_boxes["duties"].get_selected_values() == list(
        settings.default_duties
    )
    assert fresh.include_completed_check.isChecked()
    assert fresh.duration_entry.text() == "2.5"
    assert not fresh.tune_voltage_check.isChecked()


def test_an_unchanged_rebuild_costs_no_write(qapp, db, settings) -> None:
    writes: list[int] = []
    real_save = db.save_plan_selections

    def counting_save(*args, **kwargs):
        writes.append(1)
        return real_save(*args, **kwargs)

    db.save_plan_selections = counting_save  # type: ignore[method-assign]
    pane, _applied = make_pane(db, settings)
    first = len(writes)
    assert first >= 1

    pane.generate_plan(show_errors=False)
    pane.generate_plan(show_errors=False)
    assert len(writes) == first


def test_queue_view_says_no_plan_then_shows_the_applied_rows(
    qapp, db, settings
) -> None:
    store = PlanStore()
    view = QueueView(store)
    assert view.heading_label.text() == NO_PLAN_HEADING
    assert view.table.rowCount() == 0

    plan = build_matrix_plan(
        db=db,
        device_name=DEVICE,
        frequencies_hz=[settings.default_frequencies[0]],
        configs=[settings.default_configurations[0]],
        duties=[settings.default_duties[0]],
        voltages=list(settings.default_voltages),
        temperatures=[settings.default_temperatures[0]],
    )
    store.apply(
        plan.pending_points, source="Planner: test", device_name=DEVICE
    )
    view.refresh()

    assert view.table.rowCount() == len(plan.pending_points) > 0
    heading = view.heading_label.text()
    assert heading != NO_PLAN_HEADING
    assert str(len(plan.pending_points)) in heading
    assert "Planner: test" in heading


def test_queue_view_drains_as_points_complete(qapp, db, settings) -> None:
    store = PlanStore()
    plan = build_matrix_plan(
        db=db,
        device_name=DEVICE,
        frequencies_hz=[settings.default_frequencies[0]],
        configs=[settings.default_configurations[0]],
        duties=[settings.default_duties[0]],
        voltages=list(settings.default_voltages[:2]),
        temperatures=[settings.default_temperatures[0]],
    )
    store.apply(plan.pending_points, source="Planner: drain")
    view = QueueView(store)
    assert view.table.rowCount() == 2

    assert store.complete_point(plan.pending_points[0])
    view.refresh()
    assert view.table.rowCount() == 1
    assert "1 of 2" in view.heading_label.text()
