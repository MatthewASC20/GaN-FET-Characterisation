"""A refused action and the dialog to show for it.

Shared by every operation that has to say no before touching the rig, so the
severity of a refusal is decided once rather than at each ``messagebox`` call
site.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Refusal:
    """Why an action cannot proceed, and how loudly to say so.

    ``severity`` picks the message box: ``"info"`` for "not now" — the rig is
    busy, the operation is already running — and ``"warning"`` for "that
    request does not make sense as asked". ``"error"`` is for input that is
    wrong rather than merely untimely.
    """

    title: str
    message: str
    severity: str = "warning"
