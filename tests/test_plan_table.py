"""One renderer behind both plan tables.

The Sequence Execution Plan Listing and the Experiment tab's Planned Tests are
two views of the same thing, and each used to build its own columns and its
own row tuples. They had already drifted: one had a Status column and greyed
the completed rows, the other had six columns and dropped completed points
entirely. Nothing failed when they diverged, because nothing compared them.

These tests compare them.
"""

from __future__ import annotations

from gan_fet.core.models import MatrixPoint
from gan_fet.ui.plan_store import PlanRow
from gan_fet.ui.plan_table import (
    COMPLETED_TAG,
    PENDING_TAG,
    PLAN_COLUMN_IDS,
    PLAN_COLUMNS,
    plan_values,
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


def test_a_row_has_exactly_one_cell_per_column():
    """A mismatch here silently shifts every value one column left in Tk, so
    the voltage appears under Duty and nothing raises."""
    values, _ = plan_values(1, _point(), False, 1.0)

    assert len(values) == len(PLAN_COLUMNS)
    assert len(PLAN_COLUMN_IDS) == len(PLAN_COLUMNS)


def test_the_column_ids_are_unique():
    """Tk silently keeps the last of a duplicated column id."""
    assert len(set(PLAN_COLUMN_IDS)) == len(PLAN_COLUMN_IDS)


def test_the_two_tables_render_the_same_point_identically():
    """The whole reason this module exists. The planner passes a duration and
    the queue does not; everything else has to match, cell for cell."""
    point = _point(350)
    listing, listing_tag = plan_values(4, point, False, 2.5)
    queue, queue_tag = plan_values(4, point, False, None)

    assert listing[:-1] == queue[:-1]
    assert listing_tag == queue_tag


def test_a_missing_duration_is_blank_rather_than_a_guess():
    """The queue has no per-point estimate. Showing "0.0 min" would be a
    number the operator could plan around, and it would be wrong."""
    values, _ = plan_values(1, _point(), False, None)

    assert values[-1] == ""


def test_completed_and_pending_rows_are_told_apart():
    _, done = plan_values(1, _point(), True, 1.0)
    _, todo = plan_values(2, _point(), False, 1.0)

    assert done == COMPLETED_TAG
    assert todo == PENDING_TAG
    assert done != todo


def test_the_status_cell_says_which():
    """The tag is a colour, and colour alone is not a readable answer."""
    done, _ = plan_values(1, _point(), True, 1.0)
    todo, _ = plan_values(1, _point(), False, 1.0)

    assert "Completed" in done
    assert "Pending" in todo


def test_the_index_is_the_position_in_the_plan():
    """Completed points are listed in the planner and removed from the queue,
    so numbering the visible rows would renumber the plan every time a run
    finished — and step 7 in the morning would not be step 7 by lunchtime."""
    values, _ = plan_values(7, _point(), False, 1.0)

    assert values[0] == "7"


def test_every_parameter_of_the_point_appears():
    """A row that omits one is a row the operator cannot check the plan
    against, on a rig where the wrong voltage is a 400 V mistake."""
    values, _ = plan_values(
        1,
        MatrixPoint(
            device_name="GS66508B",
            config="Half Bridge",
            frequency_hz=13_560_000,
            duty_pct=35,
            temperature_c=125,
            voltage_v=350,
        ),
        False,
        1.0,
    )
    rendered = " ".join(values)

    assert "125" in rendered
    assert "Half Bridge" in rendered
    assert "35" in rendered
    assert "350" in rendered
    assert "13.56" in rendered or "13560" in rendered


def test_rows_carry_the_point_they_came_from():
    """The queue builds PlanRow, the planner builds PlannedTestPoint, and the
    renderer takes either. Both need the same two attribute names."""
    row = PlanRow(_point(), False)

    assert row.point.voltage_v == 200
    assert row.is_completed is False
