"""One definition of what a test-plan table looks like.

Two tables show a plan: the planner's "Sequence Execution Plan Listing" and
the Experiment tab's "Planned Tests". They are meant to be the same view of
the same thing, and while each built its own columns and its own row tuples
they were the same only for as long as someone remembered to change both.
They had already drifted — one showed a Status column and greyed the
completed rows, the other dropped completed points entirely and rendered an
empty table when a plan was finished.

So the columns, the formatting and the tags live here, and both trees are
built from them. Making the two disagree now requires editing this file, at
which point both change together.

Tk is touched only through the two ``configure_*`` helpers; ``plan_values``
is pure and is what the tests exercise.
"""

from __future__ import annotations

from typing import Iterable, Optional, Protocol

from gan_fet.core.models import MatrixPoint, freq_label


class PlanRowLike(Protocol):
    """What a row needs to be drawable.

    Structural rather than a shared base class, because the two producers are
    already the right shape and belong to different layers:
    :class:`gan_fet.core.sequence.PlannedTestPoint` is a core type that must
    not learn about the UI, and :class:`gan_fet.ui.plan_store.PlanRow` is the
    queue's own. Requiring a common ancestor would drag one into the other.
    """

    @property
    def point(self) -> MatrixPoint: ...

    @property
    def is_completed(self) -> bool: ...

#: ``(column id, heading, width, anchor)`` in display order. ``duration`` is
#: last because it is the only one the planner fills in and the queue does not
#: — a trailing empty column reads as "not estimated here" rather than as a
#: missing value in the middle of the row.
PLAN_COLUMNS: tuple[tuple[str, str, int, str], ...] = (
    ("step", "#", 50, "center"),
    ("temp", "Temp (°C)", 90, "center"),
    ("config", "Configuration", 150, "w"),
    ("freq", "Frequency", 110, "center"),
    ("duty", "Duty (%)", 80, "center"),
    ("voltage", "Voltage (V)", 90, "center"),
    ("status", "Status", 110, "center"),
    ("duration", "Est. Time (min)", 110, "center"),
)

PLAN_COLUMN_IDS = tuple(spec[0] for spec in PLAN_COLUMNS)

#: Completed rows stay visible but recede. Pending rows are the ones the
#: operator is being asked to read.
COMPLETED_TAG = "completed"
PENDING_TAG = "pending"

_TAG_STYLES = {
    COMPLETED_TAG: {"foreground": "#666666", "background": "#f0f0f0"},
    PENDING_TAG: {"foreground": "#0d47a1"},
}


def plan_values(
    index: int,
    point: MatrixPoint,
    is_completed: bool,
    duration_minutes: Optional[float] = None,
) -> tuple[tuple, str]:
    """The cell values and row tag for one planned point.

    ``index`` is 1-based and is the position in the plan, not in the visible
    rows: with completed points now listed, numbering the visible rows instead
    would renumber the plan every time a run finished.

    ``duration_minutes`` of None leaves the estimate blank rather than
    guessing, because the queue has no per-point duration to show.
    """
    return (
        (
            f"{index}",
            f"{point.temperature_c} °C",
            point.config,
            freq_label(point.frequency_hz),
            f"{point.duty_pct} %",
            f"{point.voltage_v} V",
            "Completed" if is_completed else "Pending",
            "" if duration_minutes is None else f"{duration_minutes:.1f}",
        ),
        COMPLETED_TAG if is_completed else PENDING_TAG,
    )


def configure_plan_tree(tree) -> None:
    """Apply the shared headings, widths and tag styles to a Treeview."""
    for column_id, heading, width, anchor in PLAN_COLUMNS:
        tree.heading(column_id, text=heading)
        tree.column(column_id, width=width, anchor=anchor)
    for tag, style in _TAG_STYLES.items():
        tree.tag_configure(tag, **style)


def fill_plan_tree(
    tree,
    rows: Iterable[PlanRowLike],
    duration_minutes: Optional[float] = None,
) -> int:
    """Replace the tree's contents with ``rows``. Returns how many were shown.

    Clearing and refilling rather than diffing: a plan is small, and a
    partially-updated table showing rows from two different plans is the
    failure this whole arrangement exists to prevent.
    """
    tree.delete(*tree.get_children())
    count = 0
    for index, row in enumerate(rows, start=1):
        values, tag = plan_values(
            index, row.point, row.is_completed, duration_minutes
        )
        tree.insert("", "end", values=values, tags=(tag,))
        count += 1
    return count
