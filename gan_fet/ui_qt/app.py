"""Qt application runner.

Owns the ``QApplication`` for the lifetime of the window. The caller keeps
owning process resources and closes them idempotently after this returns;
the window's own closing sequence also closes them before accepting the
close, whichever happens first.
"""

from __future__ import annotations

from typing import Callable


def run_qt_shell(
    *,
    settings,
    db,
    engine,
    safety,
    smu,
    wavegen_controller,
    is_simulated: bool,
    close_resources: Callable[[], None],
) -> int:
    from PyQt6.QtWidgets import QApplication

    from gan_fet.ui_qt.main_window import QtMainWindow

    app = QApplication.instance() or QApplication([])
    window = QtMainWindow(
        settings=settings,
        db=db,
        engine=engine,
        safety=safety,
        smu=smu,
        wavegen_controller=wavegen_controller,
        is_simulated=is_simulated,
        close_resources=close_resources,
    )
    window.resize(960, 640)
    window.show()
    return app.exec()
