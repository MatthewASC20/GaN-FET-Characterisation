"""Worker-to-UI marshalling for the Qt front-end.

Satisfies the ``Dispatcher`` protocol the operations layer already speaks
(``post`` only). The mechanism is a signal to self: Qt delivers a signal
emitted from a worker thread as a queued call on the object's own thread,
which is the UI thread this object is created on. That single property is
everything ``UiDispatcher`` implements by hand for Tk.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal

log = logging.getLogger(__name__)


class QtDispatcher(QObject):
    _invoke = pyqtSignal(object, tuple)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._invoke.connect(self._run)

    def post(self, func: Callable[..., Any], *args: Any) -> None:
        """Run ``func(*args)`` on the UI thread, from any thread."""
        self._invoke.emit(func, args)

    def _run(self, func: Callable[..., Any], args: tuple) -> None:
        try:
            func(*args)
        except Exception:
            log.exception("Dispatched UI callback failed")

    def call(
        self,
        func: Callable[..., Any],
        *args: Any,
        timeout: Optional[float] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Any:
        """Run ``func(*args)`` on the UI thread and wait for its answer.

        For worker code that needs an operator decision (an overwrite
        confirmation, a chamber prompt). Waits in short slices so a
        cancellation unblocks the worker instead of stranding it behind a
        dialog nobody will answer; a cancelled or timed-out call returns
        ``None``.
        """
        if QThread.currentThread() is self.thread():
            return func(*args)
        done = threading.Event()
        result: dict[str, Any] = {}

        def runner() -> None:
            try:
                result["value"] = func(*args)
            finally:
                done.set()

        self.post(runner)
        waited = 0.0
        while not done.wait(0.1):
            waited += 0.1
            if cancel_event is not None and cancel_event.is_set():
                return None
            if timeout is not None and waited >= timeout:
                return None
        return result.get("value")
