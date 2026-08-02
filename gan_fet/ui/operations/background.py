"""Running one rig operation off the Tk thread and reporting how it ended.

Every rig operation has the same skeleton: do the work on a worker, catch
whatever it raises, and post a completion callback back to the UI thread. The
completion callback is what releases the operation token, so **it has to be
posted on every path**. A worker that failed to post would leave the
coordinator believing an operation was still running, and the rig would refuse
every subsequent command with "Rig Busy" until the application was restarted.

That skeleton was written out six times. Here once.

No tkinter: the dispatcher is the only route back to the Tk thread and this
module only ever hands it a callable.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional, Protocol

from gan_fet.ui.operations.worker_pool import WorkerPool

log = logging.getLogger(__name__)


class Dispatcher(Protocol):
    """The UI-thread marshalling half of ``UiDispatcher``.

    Only ``post`` is needed, and only for its side effect — the returned future
    is deliberately ignored, because nothing here waits on the UI thread.
    """

    def post(
        self, func: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any: ...


def run_in_background(
    *,
    pool: WorkerPool,
    dispatcher: Dispatcher,
    work: Callable[[], None],
    on_done: Callable[[Optional[BaseException]], None],
    name: str,
    on_error: Optional[
        Callable[[BaseException], Optional[BaseException]]
    ] = None,
) -> threading.Thread:
    """Run ``work`` on a worker and post ``on_done`` with how it ended.

    ``on_done`` receives ``None`` on success or the exception otherwise, and is
    posted whatever happens — that is the whole point of this function.

    ``on_error`` is optional recovery that runs **on the worker thread**, while
    the rig is still in whatever state the failure left it. Bus Off uses it to
    attempt the emergency shutdown path before reporting. It may return a
    replacement exception to describe the outcome better; anything it raises
    itself is logged and discarded, because a failed recovery must not stop the
    original failure being reported.

    ``BaseException`` is caught deliberately. ``KeyboardInterrupt`` or
    ``SystemExit`` inside a worker is still an operation that ended, and it has
    to release its token like any other.
    """

    def worker() -> None:
        error: Optional[BaseException] = None
        try:
            work()
        except BaseException as exc:  # noqa: BLE001 - see docstring
            error = exc
            if on_error is not None:
                try:
                    error = on_error(exc) or exc
                except Exception:  # pragma: no cover - defensive
                    log.exception("Recovery failed during %s", name)
        dispatcher.post(on_done, error)

    return pool.start(worker, name=name)
