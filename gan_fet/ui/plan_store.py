"""The one test plan the rig is working from.

Before this there were three: the Experiment tab's queue, the Planner tab's
plan, and the list "Start Auto Sequence" built for itself — each an independent
``build_plan`` call over different inputs. They usually agreed, which is worse
than never agreeing, because the case where they diverged was invisible: a
planned sequence would run the planner's points while the queue displayed a
list rebuilt from the Experiment tab's selectors, and the window switched to
that queue the moment the sequence started.

So there is now one applied plan. The queue displays it and the sequence runs
it, and when nothing is applied both fall back to the live matrix as before.

No tkinter: the window renders what this decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Optional, Sequence

from gan_fet.core.models import MatrixPoint


@dataclass(frozen=True)
class AppliedPlan:
    """A plan the operator deliberately chose, and where it came from."""

    points: tuple[MatrixPoint, ...]
    #: Shown in the queue heading, so it is obvious the list is not the live
    #: matrix. e.g. "Planner: 3 frequencies".
    source: str

    def __len__(self) -> int:
        return len(self.points)


class PlanStore:
    """Holds the applied plan, if there is one.

    Deliberately not a plan *builder*. Building is
    :func:`gan_fet.core.sequence.build_plan`; this only remembers which result
    the operator committed to.
    """

    def __init__(self) -> None:
        self._applied: Optional[AppliedPlan] = None

    @property
    def applied(self) -> Optional[AppliedPlan]:
        return self._applied

    @property
    def is_applied(self) -> bool:
        return self._applied is not None

    def apply(self, points: Sequence[MatrixPoint], *, source: str) -> AppliedPlan:
        """Commit to ``points``, replacing whatever was applied before."""
        self._applied = AppliedPlan(tuple(points), source)
        return self._applied

    def clear(self) -> None:
        """Go back to following the Experiment tab's selectors."""
        self._applied = None


class ApplyAction(Enum):
    """What pressing "Apply Test Plan" should do."""

    #: Nothing to apply. The planner produced no runnable points.
    REFUSE = auto()
    #: A sequence is running. Offer to stop it and apply, or leave it alone.
    CONFIRM_STOP_AND_APPLY = auto()
    #: A plan is already applied. Offer to replace it.
    CONFIRM_REPLACE = auto()
    #: Nothing in the way.
    APPLY = auto()


@dataclass(frozen=True)
class ApplyDecision:
    action: Any
    title: str = ""
    message: str = ""


def apply_plan_decision(
    *,
    point_count: int,
    sequence_running: bool,
    existing: Optional[AppliedPlan],
) -> ApplyDecision:
    """Decide what applying a freshly built plan should do.

    A running sequence is checked before an existing plan, and is offered as a
    deliberate stop rather than refused. Refusing would leave the operator with
    no way to change course without hunting for the stop button; silently
    applying would leave the queue describing a plan the running sequence is
    not executing, which is the failure this whole change exists to remove.
    """
    if point_count <= 0:
        return ApplyDecision(
            ApplyAction.REFUSE,
            "Apply Test Plan",
            "There are no runnable points in this plan. Every combination "
            "selected has already been measured, or nothing is selected.",
        )

    if sequence_running:
        return ApplyDecision(
            ApplyAction.CONFIRM_STOP_AND_APPLY,
            "Sequence Running",
            "An auto sequence is currently running.\n\n"
            f"Stop it and apply this plan ({point_count} points) instead?\n\n"
            "The running sequence will be cancelled. Points already completed "
            "stay recorded.",
        )

    if existing is not None:
        return ApplyDecision(
            ApplyAction.CONFIRM_REPLACE,
            "Replace Test Plan",
            f"A test plan is already applied ({len(existing)} points, from "
            f"{existing.source}).\n\n"
            f"Replace it with this one ({point_count} points)?",
        )

    return ApplyDecision(ApplyAction.APPLY)


def pending_points(
    applied: AppliedPlan, completed: set
) -> list[MatrixPoint]:
    """The applied plan minus what has already been measured.

    Completed points are dropped rather than shown struck through: this is a
    queue of what is left to run, and a plan half-finished in an earlier
    session should not look like it is about to repeat itself.

    ``completed`` holds ``(frequency_hz, config, duty_pct, voltage_v,
    temperature_c)`` tuples, matching ``build_matrix_plan``.
    """
    return [
        point
        for point in applied.points
        if (
            point.frequency_hz,
            point.config,
            point.duty_pct,
            point.voltage_v,
            point.temperature_c,
        )
        not in completed
    ]


@dataclass(frozen=True)
class QueueContents:
    """What the Auto Testing Queue should show right now."""

    rows: list[MatrixPoint]
    heading: str


def applied_queue(
    applied: AppliedPlan, completed: set, *, limit: int = 5
) -> QueueContents:
    """Rows and heading for an applied plan.

    Extracted from the widget because a blank queue is indistinguishable from
    a broken one on screen, and a bug in here could only be found by running
    the application. Now it can be exercised without a display.
    """
    pending = pending_points(applied, completed)
    shown = pending[:limit]
    return QueueContents(shown, queue_heading(applied, len(pending), len(shown), ""))


def queue_heading(
    applied: Optional[AppliedPlan], pending: int, shown: int, live_label: str
) -> str:
    """The queue's heading, which has to say which plan is on screen.

    An applied plan ignores the Experiment tab's selectors, so without this the
    operator would change a parameter, see the queue not move, and reasonably
    conclude the application was broken.
    """
    if applied is None:
        return live_label
    if pending <= 0:
        return f"Applied plan complete — all {len(applied)} points measured."
    return (
        f"Applied plan ({applied.source}): next {shown} of {pending} "
        f"remaining, {len(applied)} total. Parameter selections are ignored "
        "until the plan is cleared."
    )


class ClearAction(Enum):
    """What pressing "Clear Plan" should do."""

    #: Nothing applied; the queue already follows the selectors.
    NOTHING_TO_CLEAR = auto()
    #: A sequence is running on this plan. Confirm before pulling it away.
    CONFIRM_STOP_AND_CLEAR = auto()
    #: Nothing in the way.
    CLEAR = auto()


def clear_plan_decision(
    *, sequence_running: bool, existing: Optional[AppliedPlan]
) -> ApplyDecision:
    """Decide what clearing the applied plan should do.

    Clearing during a run is offered as a stop, for the same reason applying
    is: the running sequence holds its own copy of the point list, so clearing
    the store alone would leave the queue describing the live matrix while a
    plan was still executing — which is the divergence this module exists to
    prevent.
    """
    if existing is None:
        return ApplyDecision(
            ClearAction.NOTHING_TO_CLEAR,  # type: ignore[arg-type]
            "Clear Test Plan",
            "No test plan is applied.\n\n"
            "The Auto Testing Queue is showing the live matrix — every "
            "combination of the parameters selected on this tab that has not "
            "been measured yet. That is not a plan and there is nothing to "
            "clear; it updates by itself as you change the selections.",
        )
    if sequence_running:
        return ApplyDecision(
            ClearAction.CONFIRM_STOP_AND_CLEAR,  # type: ignore[arg-type]
            "Sequence Running",
            "An auto sequence is running this plan.\n\n"
            "Stop it and clear the plan?\n\n"
            "Points already completed stay recorded, and the queue goes back "
            "to following the parameter selections.",
        )
    return ApplyDecision(ClearAction.CLEAR, "", "")  # type: ignore[arg-type]
