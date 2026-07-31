"""Background work for the UI.

Every long-running UI action (autotune, ZVS search, bus ramp-down, report
generation, shutdown) follows the same shape: do the work off the Tk thread,
then hand the result — or the exception — back on it. Doing that by hand at
each call site is how divergent error handling crept in, so it lives here
once.
"""

from __future__ import annotations

import logging
import threading
import tkinter as tk
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


def run_in_background(
    root: tk.Misc,
    work: Callable[[], Any],
    on_done: Optional[Callable[[Any, Optional[BaseException]], None]] = None,
    *,
    name: str = "ui-task",
) -> threading.Thread:
    """Run `work()` on a worker thread; deliver `(result, error)` to `on_done`
    on the Tk main thread. `on_done` is always called exactly once, whether
    the work returned or raised."""

    def runner() -> None:
        result: Any = None
        error: Optional[BaseException] = None
        try:
            result = work()
        except BaseException as exc:  # noqa: BLE001 — reported, not swallowed
            error = exc
            log.debug("background task %r failed", name, exc_info=exc)
        if on_done is None:
            return
        try:
            root.after(0, on_done, result, error)
        except RuntimeError:
            # Tk loop already gone (window closed) — nothing left to notify.
            log.debug("background task %r finished after shutdown", name)

    thread = threading.Thread(target=runner, daemon=True, name=name)
    thread.start()
    return thread
