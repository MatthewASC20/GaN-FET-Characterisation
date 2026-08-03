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
    sheets=None,
    hardware_offline: bool = False,
    recovered_runs: int = 0,
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
        hardware_offline=hardware_offline,
        close_resources=close_resources,
    )
    if sheets is not None:
        sheets.status = window.set_status_async
    # Same precedence as the Tk startup: offline first, then the Sheets
    # degradation, then mode/recovery notices.
    if hardware_offline:
        window.set_status(
            "DISCONNECTED: One or more configured instruments are "
            "unreachable. Update their connections in Configuration."
        )
    elif (
        sheets is not None
        and not sheets.available
        and settings.google.enabled
        and not is_simulated
    ):
        window.set_status(
            "Sheets sync disabled: credentials or Google libraries missing."
        )
    if is_simulated:
        window.set_status(
            "SIMULATION MODE ACTIVE: using isolated virtual instruments "
            "and data."
        )
    elif recovered_runs and not hardware_offline:
        window.set_status(
            f"Marked {recovered_runs} prior interrupted run(s) for review."
        )
    window.resize(960, 640)
    window.show()
    return app.exec()
