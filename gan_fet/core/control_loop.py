"""Primitives shared by every closed-loop tuner.

Peak control, the ZVS voltage search and the frequency search all settle
between commands, all poll a cancellation flag while doing so, and all coerce
instrument readings that may be absent or nonsensical. Those three behaviours
were reimplemented in each module; a divergence between them would be a
cancellation that is honoured in one loop and swallowed in another.

Cancellation is reported rather than raised. Each loop has its own terminal
exception, and preserving that is the point: the shared helper decides *when*
a wait was interrupted, the caller decides what that means.
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Optional

#: Longest single sleep while settling. Bounds how long a cancellation can go
#: unnoticed, independently of the settle time requested.
CANCEL_POLL_INTERVAL_S = 0.05


def is_cancelled(cancel_check: Optional[Callable[[], bool]]) -> bool:
    """Whether an optional cancellation predicate is asking us to stop."""
    return cancel_check is not None and cancel_check()


def finite_float(value: Any) -> Optional[float]:
    """Coerce an instrument reading to a usable float, or ``None``.

    ``None`` covers every unusable case — absent, non-numeric, NaN, infinite —
    because callers treat them identically: as telemetry that did not arrive.
    Distinguishing them would only invite a caller to act on a NaN.
    """
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def settle(
    seconds: float,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> bool:
    """Wait in short slices, returning ``False`` if cancelled part-way.

    Sliced so a long settle cannot hide a cancellation for its full duration,
    which matters because these waits sit between commands to an energised rig.
    """
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if is_cancelled(cancel_check):
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            return True
        time.sleep(min(CANCEL_POLL_INTERVAL_S, remaining))
