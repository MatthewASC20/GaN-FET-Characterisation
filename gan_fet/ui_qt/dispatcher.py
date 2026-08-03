"""Worker-to-UI marshalling for the Qt front-end.

Satisfies the ``Dispatcher`` protocol the operations layer already speaks
(``post`` only). The mechanism is a signal to self: Qt delivers a signal
emitted from a worker thread as a queued call on the object's own thread,
which is the UI thread this object is created on. That single property is
everything ``UiDispatcher`` implements by hand for Tk.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from PyQt6.QtCore import QObject, pyqtSignal

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
