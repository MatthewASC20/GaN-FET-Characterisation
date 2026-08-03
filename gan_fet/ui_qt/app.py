"""Qt application runner.

Owns the ``QApplication`` for the lifetime of the window; the caller keeps
owning process resources (database, clients, loggers) and closes them after
this returns, exactly as with the Tk main loop.
"""

from __future__ import annotations

from typing import Callable


def run_qt_shell(
    *,
    is_simulated: bool,
    on_emergency_stop: Callable[[], object],
) -> int:
    from PyQt6.QtWidgets import QApplication

    from gan_fet.ui_qt.main_window import QtMainWindow

    app = QApplication.instance() or QApplication([])
    window = QtMainWindow(
        is_simulated=is_simulated,
        on_emergency_stop=on_emergency_stop,
    )
    window.resize(960, 640)
    window.show()
    return app.exec()
