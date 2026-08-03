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

It is stored in SQLite rather than held only in memory. A plan is a statement
of intent about hours of bench time, and losing it to a restart meant
rebuilding it from memory and hoping the selections matched.

Measured points are *marked*, not removed. The queue drains on screen, but the
plan survives being finished, so it can be inspected afterwards or applied to
the next part.

No tkinter: the window renders what this decides.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Iterable, Optional, Protocol, Sequence

from gan_fet.core.models import MatrixPoint

log = logging.getLogger(__name__)


class PlanBackend(Protocol):
    """The operations :class:`PlanStore` needs from storage.

    Narrower than :class:`~gan_fet.storage.db.Database` so the store can be
    exercised without one.
    """

    def save_plan(
        self,
        points: Sequence[MatrixPoint],
        *,
        source: str,
        device_name: str,
        completed: Iterable[int] = (),
    ) -> None: ...

    def load_plan(
        self,
    ) -> Optional[tuple[list[MatrixPoint], set[int], str, str]]: ...

    def clear_plan(self) -> None: ...


@dataclass(frozen=True)
class AppliedPlan:
    """A plan the operator deliberately chose, and where it came from.

    ``points`` is the *whole* plan, in run order, and ``completed`` holds the
    positions already measured. Keeping the finished points means the plan can
    still answer "what was in that?" after the last one is done — which a
    queue that deleted as it drained could not.
    """

    points: tuple[MatrixPoint, ...]
    #: Shown in the queue heading, so it is obvious the list is not the live
    #: matrix. e.g. "Planner: 3 frequencies".
    source: str
    #: The device the plan was built for. Held explicitly rather than read
    #: from the Experiment tab's selector: a persisted plan outlives the
    #: selection that made it, and running it against a different part would
    #: record that part's runs with this one's parameters.
    device_name: str = ""
    #: Positions in ``points`` that have been measured.
    completed: frozenset[int] = field(default_factory=frozenset)

    def __len__(self) -> int:
        return len(self.points)

    @property
    def total_count(self) -> int:
        return len(self.points)

    @property
    def completed_count(self) -> int:
        return len(self.completed)

    @property
    def pending(self) -> tuple[MatrixPoint, ...]:
        """What is left to run, in order."""
        return tuple(
            point
            for position, point in enumerate(self.points)
            if position not in self.completed
        )

    def is_for(self, device_name: str) -> bool:
        """Whether this plan belongs to ``device_name``.

        An unnamed plan matches anything: plans stored before the device was
        recorded, and the headless tests, must not be locked out.
        """
        return not self.device_name or self.device_name == device_name.strip()


class PlanStore:
    """Holds the applied plan, if there is one, and persists it.

    Deliberately not a plan *builder*. Building is
    :func:`gan_fet.core.sequence.build_plan`; this only remembers which result
    the operator committed to.

    ``backend`` is optional so the decision logic below can be tested without
    a database. In the application it is always the live :class:`Database`.
    """

    def __init__(self, backend: Optional[PlanBackend] = None) -> None:
        self._applied: Optional[AppliedPlan] = None
        self._backend = backend

    @property
    def applied(self) -> Optional[AppliedPlan]:
        return self._applied

    @property
    def is_applied(self) -> bool:
        return self._applied is not None

    def apply(
        self,
        points: Sequence[MatrixPoint],
        *,
        source: str,
        device_name: str = "",
    ) -> AppliedPlan:
        """Commit to ``points``, replacing whatever was applied before.

        Stored exactly as the planner produced it. In particular, points that
        already have runs are *not* filtered out here — the planner has
        already decided that, and "Include Completed Runs (Re-test)" means the
        operator asked for them deliberately. Filtering again here would make
        a re-test plan drain to nothing the moment it was applied.
        """
        if not device_name and points:
            device_name = points[0].device_name
        self._applied = AppliedPlan(tuple(points), source, device_name)
        self._persist(self._applied)
        return self._applied

    def clear(self) -> None:
        """Forget the applied plan, here and on disk."""
        self._applied = None
        self._persist(None)

    def complete_point(self, point: Optional[MatrixPoint]) -> bool:
        """Mark ``point`` measured, taking it out of the queue.

        Called when a run finishes. Marking rather than deleting keeps the
        finished plan inspectable; the queue drains because the table shows
        ``pending``. Persisting it here rather than recomputing from the run
        table is what lets a sequence interrupted by a crash resume where it
        stopped — and recomputing would re-run any point the operator had
        deliberately queued for a re-test.

        The *first* outstanding match is marked. A plan may legitimately queue
        the same point twice; completing one run must not tick off both.

        Returns whether anything changed, so a caller can skip a redraw.
        """
        if point is None or self._applied is None:
            return False
        key = _key(point)
        position = next(
            (
                index
                for index, candidate in enumerate(self._applied.points)
                if _key(candidate) == key
                and index not in self._applied.completed
            ),
            None,
        )
        if position is None:
            return False
        self._applied = AppliedPlan(
            self._applied.points,
            self._applied.source,
            self._applied.device_name,
            self._applied.completed | {position},
        )
        # Through the same full save as everything else. A surgical UPDATE
        # would be less work, but it is a second write path that can disagree
        # with the first, and a point takes a minute of bench time to measure
        # — the few milliseconds this costs are not worth a way for the stored
        # plan and the plan in memory to diverge.
        self._persist(self._applied)
        return True

    def restore(self) -> Optional[AppliedPlan]:
        """Reload the plan saved by a previous session, if any.

        Called once at startup. A plan whose points are all measured is
        restored as such rather than discarded: "the job is finished" is a
        real state the operator reached, and dropping it back to "no plan
        applied" would misreport what happened.
        """
        if self._backend is None:
            return None
        try:
            stored = self._backend.load_plan()
        except Exception:
            log.exception("Could not load the saved test plan")
            return None
        if stored is None:
            return None
        points, completed, source, device_name = stored
        self._applied = AppliedPlan(
            tuple(points), source, device_name, frozenset(completed)
        )
        log.info(
            "Restored applied test plan: %d of %d point(s) left for %s (%s)",
            len(self._applied.pending),
            len(points),
            device_name or "unknown device",
            source,
        )
        return self._applied

    def _persist(self, plan: Optional[AppliedPlan]) -> None:
        """Mirror the in-memory state to storage, best effort.

        A failed write must not refuse the apply. The operator is at the bench
        with a plan in front of them; losing persistence is an inconvenience
        next session, whereas refusing to apply blocks them now. Logged at
        error level so the divergence is not silent.
        """
        if self._backend is None:
            return
        try:
            if plan is None:
                self._backend.clear_plan()
            else:
                self._backend.save_plan(
                    plan.points,
                    source=plan.source,
                    device_name=plan.device_name,
                    completed=plan.completed,
                )
        except Exception:
            log.exception(
                "Could not save the test plan; it is applied for this session "
                "only and will not survive a restart"
            )


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


def _key(point: MatrixPoint) -> tuple:
    """The identity of a matrix point, matching ``build_matrix_plan``.

    Every field matters: matching on fewer would silently mark points done
    that were never measured.
    """
    return (
        point.frequency_hz,
        point.config,
        point.duty_pct,
        point.voltage_v,
        point.temperature_c,
    )


def measured_point(outcome: Any, success: bool) -> Optional[MatrixPoint]:
    """The point a finished run measured, or None if the queue must not shrink.

    Only a successful run takes work off the queue. ``runs`` is append-only and
    a failed or tripped attempt records itself without producing a measurement,
    so removing the point would quietly drop it from a sequence that had not
    actually done it — and on this rig the operator would only find out when
    the characterisation set came up short.

    Reads defensively because the outcome comes from the engine and may be
    absent entirely (a run that never started has no record).
    """
    if not success:
        return None
    return getattr(getattr(outcome, "record", None), "point", None)


def pending_points(applied: AppliedPlan) -> list[MatrixPoint]:
    """What the sequence has left to run.

    Read straight off the stored plan, which records its own progress. It used
    to be recomputed by subtracting the run table on every read, which meant a
    re-test plan — points deliberately queued again despite already having
    runs — filtered itself down to nothing before it could start.
    """
    return list(applied.pending)


@dataclass(frozen=True)
class PlanRow:
    """One row of a plan table: the point, and whether it has been measured."""

    point: MatrixPoint
    is_completed: bool


@dataclass(frozen=True)
class QueueContents:
    """What the Planned Tests table should show right now."""

    rows: list[PlanRow]
    heading: str


def plan_rows(applied: AppliedPlan) -> list[PlanRow]:
    """The outstanding points, in run order.

    All pending: measured points are marked in the stored plan and filtered
    out here, so the table drains as the sequence works through it. The flag
    is carried anyway so these rows render through the same table code as the
    planner's listing, where completed points *are* shown.
    """
    return [PlanRow(point, False) for point in applied.pending]


def applied_queue(
    applied: AppliedPlan, selected_device: str = ""
) -> QueueContents:
    """Rows and heading for an applied plan.

    Extracted from the widget because a blank table is indistinguishable from
    a broken one on screen, and a bug in here could only be found by running
    the application. Now it can be exercised without a display.
    """
    rows = plan_rows(applied)
    return QueueContents(rows, queue_heading(applied, selected_device))


#: Shown when no plan has been applied. The queue is empty because nothing has
#: been asked for, which is a different thing from having finished.
NO_PLAN_HEADING = (
    "No test plan applied. Build one in the Test Planner and press "
    "\u201cApply Test Plan\u201d — nothing runs until you do."
)


def wrong_device_heading(applied: AppliedPlan, selected_device: str) -> str:
    """Said when the applied plan belongs to a different part.

    Names both devices. "Wrong device" alone leaves the operator to work out
    which of the two is wrong, and the answer is not always the plan.
    """
    return (
        f"This plan was built for {applied.device_name}, but "
        f"{selected_device} is selected. It will not run against a different "
        f"part — select {applied.device_name} again, or clear the plan."
    )


def queue_heading(
    applied: Optional[AppliedPlan], selected_device: str = ""
) -> str:
    """The Planned Tests heading.

    There is no computed fallback. The table shows the plan the operator
    applied and nothing else: a cross-product assembled from whatever options
    happened to be configured is a guess at what someone wants, and it looked
    enough like a real plan that "Clear Plan" appeared broken when it correctly
    reported there was none.

    The states are worded so they cannot be confused for one another: an empty
    table means "no plan", and only that.
    """
    if applied is None:
        return NO_PLAN_HEADING
    if selected_device and not applied.is_for(selected_device):
        return wrong_device_heading(applied, selected_device)
    total = applied.total_count
    if total == 0:
        return f"Applied plan is empty ({applied.source}) — nothing to run."
    pending = len(applied.pending)
    if pending <= 0:
        return f"Applied plan complete — all {total} points measured."
    done = total - pending
    if done > 0:
        return (
            f"{pending} of {total} points left to run ({applied.source}); "
            f"{done} completed."
        )
    return f"{pending} test(s) to run, in order ({applied.source})."


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
            "No test plan is applied, so there is nothing to clear. The "
            "Auto Testing Queue is empty until you apply one from the Test "
            "Planner.",
        )
    if sequence_running:
        return ApplyDecision(
            ClearAction.CONFIRM_STOP_AND_CLEAR,  # type: ignore[arg-type]
            "Sequence Running",
            "An auto sequence is running this plan.\n\n"
            "Stop it and clear the plan?\n\n"
            "Points already completed stay recorded, and the queue is left "
            "empty until you apply another plan.",
        )
    return ApplyDecision(ClearAction.CLEAR, "", "")  # type: ignore[arg-type]


class StartAction(Enum):
    """What "Start Auto Sequence" should do."""

    #: Nothing to run. Offer to go and build a plan.
    OFFER_PLANNER = auto()
    #: The applied plan has no points left.
    ALREADY_COMPLETE = auto()
    #: The plan belongs to a different device. Refuse.
    WRONG_DEVICE = auto()
    #: Run it.
    START = auto()


def start_sequence_decision(
    *,
    applied: Optional[AppliedPlan],
    pending: int,
    selected_device: str = "",
) -> ApplyDecision:
    """Decide what starting a sequence should do.

    With nothing applied the operator is offered the Test Planner rather than
    told no. Refusing alone leaves them to work out for themselves that the
    queue is fed from another tab, which is exactly the confusion the implicit
    cross-product used to hide.

    A plan built for a different device is refused outright, not offered as a
    confirmation. Now that plans persist, one can outlive the selection that
    made it by days; running it would drive the mounted part with another
    part's frequencies and voltages and record the results against the
    mounted one. Nothing about the resulting data would look wrong.
    """
    if applied is None:
        return ApplyDecision(
            StartAction.OFFER_PLANNER,
            "No Test Plan",
            "There is no test plan to run.\n\n"
            "Open the Test Planner to build one?",
        )
    if selected_device and not applied.is_for(selected_device):
        return ApplyDecision(
            StartAction.WRONG_DEVICE,
            "Wrong Device",
            f"The applied test plan was built for {applied.device_name}, "
            f"but {selected_device} is selected.\n\n"
            "Running it would drive this part with another part's "
            "parameters and record the results against this one.\n\n"
            f"Select {applied.device_name} again, or clear the plan and "
            "build a new one.",
        )
    if pending <= 0:
        return ApplyDecision(
            StartAction.ALREADY_COMPLETE,
            "Test Plan Complete",
            f"Every point in the applied plan ({len(applied)}) has already "
            "been measured.\n\nApply a new plan, or clear this one.",
        )
    return ApplyDecision(StartAction.START, "", "")
