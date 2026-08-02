"""Decisions behind the device selector.

Adding a device is cheap. Removing one deletes every run and sample recorded
against it, which is the most destructive thing this UI can do to bench
records — so the guards in front of it are worth stating somewhere they can be
tested. Inside the window they were interleaved with ``messagebox`` calls and
could only be exercised with a display attached.

The window still owns the dialogs. These functions decide *whether* to refuse
and *what to say*; the caller shows it.

No tkinter: callers pass plain data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from gan_fet.core.models import sanitize_device_name
from gan_fet.ui.run_request import InputRejected

#: What survives a device deletion. Stated in the confirmation because the
#: operator's real question is "have I lost the measurements", and the answer
#: is "only the local database rows".
_REMOVAL_SURVIVES = (
    "Saved screenshots, reports/exports, and Google Sheets/Drive data "
    "will not be deleted."
)


@dataclass(frozen=True)
class Refusal:
    """A refused action and the dialog to show for it.

    ``severity`` picks the message box: ``"info"`` for "not now, the rig is
    busy", ``"warning"`` for "that request does not make sense".
    """

    title: str
    message: str
    severity: str = "warning"


def removal_refusal(
    *, busy: bool, device_name: str, known_devices: Sequence[str]
) -> Optional[Refusal]:
    """Why this device cannot be removed, or ``None`` if it can be.

    Busy is checked first and deliberately: a delete that lands mid-run would
    pull the rows out from under an executing experiment, and no amount of
    confirming makes that safe.
    """
    if busy:
        return Refusal(
            "Rig Busy",
            "Devices cannot be removed while a rig operation is active.",
            severity="info",
        )
    name = device_name.strip()
    if not name:
        return Refusal(
            "No Device Selected",
            "Please select or enter a device name to remove.",
        )
    if name not in known_devices:
        return Refusal(
            "Device Not Found",
            f"Device '{name}' does not exist in the database.",
        )
    return None


def removal_confirmation(device_name: str) -> tuple[str, str]:
    """The title and body of the "are you sure" prompt."""
    return (
        "Confirm Delete Device",
        f"Permanently delete device '{device_name}' and all of its runs and "
        f"samples from the local database?\n\n{_REMOVAL_SURVIVES}\n\n"
        "This action cannot be undone.",
    )


def removal_report(device_name: str) -> tuple[str, str]:
    """The title and body confirming what was actually removed."""
    return (
        "Device Removed",
        f"Device '{device_name}' and its local database records were "
        "permanently removed. External files and cloud data were left "
        "unchanged.",
    )


def device_after_removal(remaining: Sequence[str]) -> str:
    """Which device to select once the current one is gone.

    Empty when nothing is left, which the window reads as "clear every
    device-dependent view" rather than "select nothing".
    """
    return remaining[0] if remaining else ""


def validated_new_device_name(raw: Optional[str]) -> Optional[str]:
    """A name for a newly added device, or ``None`` if the operator cancelled.

    Dismissing the dialog is a cancel and says nothing. Typing something that
    cannot be used raises :class:`InputRejected`. Silently sanitising is the
    wrong call here: the name becomes a directory and a Sheets tab, and an
    operator who typed ``EPC2001C/rev2`` should be told rather than left
    looking for ``EPC2001Crev2``.
    """
    if not raw:
        return None
    name = raw.strip()
    clean = sanitize_device_name(name)
    if not clean or clean != name:
        raise InputRejected(
            "Invalid Device Name",
            "Device name contains unsupported characters.",
        )
    return clean
