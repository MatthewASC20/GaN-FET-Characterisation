"""Preconditions and cancellation routing for experiment runs.

Starting a run and stopping one are both decision chains that were interleaved
with ``messagebox`` calls inside the window. The order matters in the same way
it does for the manual ZVS search — a latched trip is refused before anything
is built or applied — and cancellation has to reach whichever thing is actually
running, which is not always the experiment engine.

The window still raises the dialogs and drives the engine. These functions
decide what happens.

No tkinter: callers pass plain data.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

from gan_fet.ui.refusal import Refusal


class ExperimentAction(Enum):
    """What pressing Start should do."""

    #: Instruments are offline. The window reports this itself, because it also
    #: switches to the Configuration tab.
    HARDWARE_OFFLINE = auto()
    #: Refused with a reason to show.
    REFUSE = auto()
    #: Something else holds the rig. The window reports which, through the
    #: operation coordinator, so the message can name the active operation.
    REPORT_BUSY = auto()
    #: Nothing in the way; build the run request and go.
    PROCEED = auto()


@dataclass(frozen=True)
class ExperimentDecision:
    action: ExperimentAction
    refusal: Optional[Refusal] = None


#: Offered when the wavegen still holds different settings from the run about
#: to start. Declining cancels the start rather than running on stale settings:
#: a run recorded against parameters the wavegen was not using is worse than no
#: run at all, because nothing downstream can tell the difference.
APPLY_FIRST_PROMPT = (
    "Wavegen Settings",
    "The wavegen does not match the selected parameters.\n\nApply them now?",
)


def experiment_precondition(
    *,
    hardware_offline: bool,
    safety_tripped: bool,
    busy: bool,
) -> ExperimentDecision:
    """Decide whether a run may be started, in the order the checks happen.

    Deliberately evaluated before the run request is built. Assembling
    parameters can raise its own errors about the operator's input, and being
    told the duration is malformed when the real problem is a latched trip
    sends them to the wrong place.
    """
    if hardware_offline:
        return ExperimentDecision(ExperimentAction.HARDWARE_OFFLINE)

    if safety_tripped:
        return ExperimentDecision(
            ExperimentAction.REFUSE,
            Refusal(
                "Safety Interlock",
                "Reset the latched safety trip before starting an experiment.",
            ),
        )

    if busy:
        return ExperimentDecision(ExperimentAction.REPORT_BUSY)

    return ExperimentDecision(ExperimentAction.PROCEED)


class CancelTarget(Enum):
    """What Cancel should stop.

    The button is shared, so what it means depends on what is running. A
    sequence has to be cancelled at the sequence level rather than the engine
    level: cancelling the engine would end the current point and let the
    sequence start the next one, which looks from the outside like Cancel not
    working.
    """

    ZVS_SEARCH = auto()
    SEQUENCE = auto()
    EXPERIMENT = auto()


def cancel_target(active_kind: Optional[str]) -> CancelTarget:
    """Which layer a cancel request should reach."""
    if active_kind == "zvs":
        return CancelTarget.ZVS_SEARCH
    if active_kind == "sequence":
        return CancelTarget.SEQUENCE
    return CancelTarget.EXPERIMENT
