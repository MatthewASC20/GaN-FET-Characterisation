"""Phase-1 PyQt6 shell: identity banner, status feed, and E-stop.

Deliberately small. The operations spine — coordinator, refusals, control
states — arrives with the next phase; until then the composition root only
offers this window under ``--simulate``. What is here is exactly what must
never regress later: the unmistakable mode identity and an emergency stop
that no state may disable.
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QLabel,
    QMainWindow,
    QPushButton,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from gan_fet.ui.presentation import mode_banner_presentation
from gan_fet.ui_qt.event_bridge import EventBridge

_ESTOP_STYLE = (
    "background-color: #b71c1c; color: white; font-weight: bold;"
    " padding: 8px 16px;"
)


class QtMainWindow(QMainWindow):
    def __init__(
        self,
        *,
        is_simulated: bool,
        on_emergency_stop: Callable[[], object],
        bridge: Optional[EventBridge] = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("GaN FET Characterisation (Qt preview)")
        self.bridge = bridge if bridge is not None else EventBridge(self)
        self._on_emergency_stop = on_emergency_stop

        text, colour = mode_banner_presentation(is_simulated)
        self._banner = QLabel(text)
        self._banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._banner.setStyleSheet(
            f"background-color: {colour}; color: white;"
            " font-weight: bold; padding: 6px;"
        )

        # E-stop lives on a fixed toolbar and is never disabled by any
        # state — the same contract the Tk window keeps. No control-state
        # refresh may ever reach it.
        toolbar = QToolBar("Safety")
        toolbar.setMovable(False)
        toggle = toolbar.toggleViewAction()
        if toggle is not None:
            toggle.setVisible(False)
        self.estop_button = QPushButton("EMERGENCY STOP")
        self.estop_button.setStyleSheet(_ESTOP_STYLE)
        self.estop_button.clicked.connect(self._emergency_stop)
        toolbar.addWidget(self.estop_button)
        self.addToolBar(toolbar)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._banner)
        placeholder = QLabel(
            "PyQt6 interface — phase-1 shell.\n\n"
            "Operations arrive with the next phase;"
            " use the Tk interface to run the rig."
        )
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(placeholder, stretch=1)
        self.setCentralWidget(central)

        self._status = QStatusBar(self)
        self.setStatusBar(self._status)
        self._status.showMessage("Ready.")
        self.bridge.status_updated.connect(self._show_status)
        self.bridge.safety_trip.connect(self._show_trip)

    def banner_text(self) -> str:
        return self._banner.text()

    def _emergency_stop(self) -> None:
        self._status.showMessage("EMERGENCY STOP requested.")
        self._on_emergency_stop()

    def _show_status(self, event) -> None:
        self._status.showMessage(event.message)

    def _show_trip(self, event) -> None:
        self._status.showMessage(f"SAFETY TRIP: {event.reason}")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.bridge.close()
        super().closeEvent(event)
