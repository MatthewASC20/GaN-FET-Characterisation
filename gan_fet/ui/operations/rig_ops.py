"""Preconditions for the operator's rig controls.

Deciding whether the ZVS search may start is a safety question, not a widget
question, and the order of the checks is the substance of it: a latched trip is
refused before anything can energise, and a search already running is stopped
rather than started again. That chain lived inside the window interleaved with
``messagebox`` calls, where it could only be exercised with a display attached.

The window still raises the dialogs and reads the Tk variables. These functions
decide what happens and in what order.

No tkinter: callers pass plain data.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Callable, Optional, Protocol

from gan_fet.ui.refusal import Refusal


class _TripSource(Protocol):
    @property
    def trip_reason(self) -> Optional[tuple[Any, ...]]: ...


def raise_if_aborted(
    *,
    cancelled: Callable[[], bool],
    safety: _TripSource,
    trip_error: Callable[..., BaseException],
    what: str,
) -> None:
    """Stop before the next energising step if cancellation or a trip landed.

    Called between every step that arms or energises something, because both
    conditions can arrive *during* the previous step: the operator presses Stop
    while the bus is ramping, or the safety monitor trips on a sample taken
    mid-search. Checking once at the top would let the rest of the sequence run
    on a rig that had already been told to stop.

    Cancellation is checked first, so an operator who pressed Stop is told the
    search stopped rather than being shown a trip they did not cause. The trip
    is not lost by this: the safety monitor has already latched and recorded it
    independently, and the window reflects the latched state either way.
    """
    if cancelled():
        raise InterruptedError(f"{what} cancelled")
    reason = safety.trip_reason
    if reason is not None:
        raise trip_error(*reason)


class ZvsAction(Enum):
    """Whether a ZVS search may start, and what to do if not.

    Stopping a *running* search is deliberately not one of these. That check
    happens in the window before any of this is consulted, because it must not
    depend on state that decides whether a search could be started: an operator
    reaching for Stop after a trip, or with the hardware offline, still needs
    the search to stop.
    """

    #: Instruments are offline. The window reports this itself, because it also
    #: switches to the Configuration tab.
    HARDWARE_OFFLINE = auto()
    #: Refused with a reason to show.
    REFUSE = auto()
    #: The wavegen does not match the selection; offer to apply it first.
    APPLY_WAVEGEN_FIRST = auto()
    #: Nothing in the way.
    LAUNCH = auto()


@dataclass(frozen=True)
class ZvsDecision:
    action: ZvsAction
    refusal: Optional[Refusal] = None
    prompt: Optional[tuple[str, str]] = None


#: Offered when the wavegen still holds different settings from the selection.
_APPLY_FIRST_PROMPT = (
    "Find ZVS",
    "The wavegen does not match the selected parameters. Apply them before "
    "the ZVS search?",
)


def zvs_precondition(
    *,
    hardware_offline: bool,
    safety_tripped: bool,
    target_peak_v: Optional[float],
    max_vds_peak_v: float,
    wavegen_pending: bool,
) -> ZvsDecision:
    """Decide what "Find ZVS Now" does, in the order the checks must happen.

    ``target_peak_v`` is ``None`` when the field could not be read at all,
    which is treated exactly like an out-of-range value: an unreadable target
    is not a target.

    Assumes no ZVS search is already running — see :class:`ZvsAction`.
    """
    if hardware_offline:
        return ZvsDecision(ZvsAction.HARDWARE_OFFLINE)

    # Before the target is even looked at: a latched trip means the rig has
    # already been judged unsafe, and no value in that field makes it safe.
    if safety_tripped:
        return ZvsDecision(
            ZvsAction.REFUSE,
            Refusal(
                "Find ZVS",
                "Reset the latched safety interlock before energizing the rig.",
            ),
        )

    if (
        target_peak_v is None
        or target_peak_v <= 0
        or target_peak_v > max_vds_peak_v
    ):
        return ZvsDecision(
            ZvsAction.REFUSE,
            Refusal(
                "Find ZVS",
                "The selected Vds target must be positive and no greater than "
                f"the {max_vds_peak_v:g} V safety limit.",
                severity="error",
            ),
        )

    if wavegen_pending:
        return ZvsDecision(
            ZvsAction.APPLY_WAVEGEN_FIRST, prompt=_APPLY_FIRST_PROMPT
        )

    return ZvsDecision(ZvsAction.LAUNCH)
