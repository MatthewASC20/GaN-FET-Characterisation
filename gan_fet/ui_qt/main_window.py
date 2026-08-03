"""Phase-2 PyQt6 window: the operations spine.

This window implements the same ``RigUi`` protocol the Tk window does, so
``RigOperations`` — coordinator, worker pool, refusal ordering — runs
unchanged behind it. What works here: Bus Off, Reset Safety, and EMERGENCY
STOP, plus the shutdown-until-confirmed closing sequence. Experiment and
tuning screens arrive with the next phases; the composition root still only
offers this window under ``--simulate``.

Threading contract, same as Tk: workers never touch a widget. Completion
callbacks arrive through :class:`QtDispatcher`; broadcast telemetry arrives
through :class:`EventBridge`. Both ride Qt's queued signal delivery.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from gan_fet.ui.operations.rig_control import RigOperations
from gan_fet.ui.operations.worker_pool import WorkerPool
from gan_fet.ui.presentation import (
    HARDWARE_OPERATION_LABELS,
    mode_banner_presentation,
)
from gan_fet.ui.smu_status import smu_status_line
from gan_fet.ui.widgets import OperationCoordinator, resolve_rig_control_state
from gan_fet.ui_qt.dispatcher import QtDispatcher
from gan_fet.ui_qt.event_bridge import EventBridge

log = logging.getLogger(__name__)

_ESTOP_STYLE = (
    "background-color: #b71c1c; color: white; font-weight: bold;"
    " padding: 8px 16px;"
)


class QtMainWindow(QMainWindow):
    """Implements ``RigUi``; owns the coordinator, pool and dispatcher."""

    def __init__(
        self,
        *,
        settings,
        engine,
        safety,
        smu,
        wavegen_controller,
        is_simulated: bool,
        close_resources: Callable[[], None] = lambda: None,
        hardware_offline: bool = False,
        bridge: Optional[EventBridge] = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("GaN FET Characterisation (Qt preview)")
        self.engine = engine
        self.safety = safety
        self.smu = smu
        self.wavegen_controller = wavegen_controller
        self._close_resources = close_resources
        self._hardware_offline = hardware_offline
        self._closing = False
        self._resources_closed = False

        self.bridge = bridge if bridge is not None else EventBridge(self)
        self.dispatcher = QtDispatcher(self)
        self.operations = OperationCoordinator()
        self.pool = WorkerPool()
        self.rig = RigOperations(
            ui=self,
            operations=self.operations,
            pool=self.pool,
            dispatcher=self.dispatcher,
            safety=safety,
            smu=smu,
            wavegen_controller=wavegen_controller,
            settings=settings,
            engine=engine,
            hardware_labels=HARDWARE_OPERATION_LABELS,
        )
        # No auto-sequence yet; RigOperations guards every use with None
        # checks, and the engine remains the emergency-stop owner.
        self.rig.sequence = None

        self._build_ui(is_simulated)
        self.refresh_controls()
        self.smu_state_changed()

    # -- construction --------------------------------------------------------

    def _build_ui(self, is_simulated: bool) -> None:
        text, colour = mode_banner_presentation(is_simulated)
        self._banner = QLabel(text)
        self._banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._banner.setStyleSheet(
            f"background-color: {colour}; color: white;"
            " font-weight: bold; padding: 6px;"
        )

        # E-stop lives on a fixed toolbar and is never disabled by any
        # state — the same contract the Tk window keeps. refresh_controls
        # must never reach it.
        toolbar = QToolBar("Safety")
        toolbar.setMovable(False)
        toggle = toolbar.toggleViewAction()
        if toggle is not None:
            toggle.setVisible(False)
        self.estop_button = QPushButton("EMERGENCY STOP")
        self.estop_button.setStyleSheet(_ESTOP_STYLE)
        self.estop_button.clicked.connect(self.rig.emergency_stop)
        toolbar.addWidget(self.estop_button)
        self.addToolBar(toolbar)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._banner)

        smu_row = QWidget()
        smu_layout = QHBoxLayout(smu_row)
        self.smu_status_label = QLabel()
        smu_layout.addWidget(self.smu_status_label, stretch=1)
        self.bus_off_button = QPushButton("Bus Off")
        self.bus_off_button.clicked.connect(self.rig.bus_off)
        smu_layout.addWidget(self.bus_off_button)
        self.reset_safety_button = QPushButton("Reset Safety")
        self.reset_safety_button.clicked.connect(self.rig.reset_safety)
        smu_layout.addWidget(self.reset_safety_button)
        layout.addWidget(smu_row)

        placeholder = QLabel(
            "PyQt6 interface — operations spine.\n\n"
            "Experiment and tuning screens arrive with the next phases;"
            " use the Tk interface to run the rig."
        )
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(placeholder, stretch=1)
        self.setCentralWidget(central)

        self._status = QStatusBar(self)
        self.setStatusBar(self._status)
        self._status.showMessage("Ready.")
        self.bridge.status_updated.connect(
            lambda event: self._status.showMessage(event.message)
        )
        self.bridge.safety_trip.connect(self._show_trip)

    def banner_text(self) -> str:
        return self._banner.text()

    def _show_trip(self, event) -> None:
        self._status.showMessage(f"SAFETY TRIP: {event.reason}")
        self.refresh_controls()

    # -- RigUi ---------------------------------------------------------------

    def set_status(self, message: str) -> None:
        self._status.showMessage(message)

    def set_status_async(self, message: str) -> None:
        self.dispatcher.post(self._status.showMessage, message)

    def hardware_online(self, action: str) -> bool:
        if not self._hardware_offline:
            return True
        self.show_error(
            "Hardware Offline",
            f"Cannot {action}: one or more instruments are unreachable.",
        )
        return False

    def report_busy(self, active_kind: Optional[str]) -> None:
        label = HARDWARE_OPERATION_LABELS.get(
            active_kind or "", active_kind or "another operation"
        )
        self.show_info(
            "Rig Busy", f"Cannot start: waiting to {label}."
        )

    def show_info(self, title: str, message: str) -> None:
        QMessageBox.information(self, title, message)

    def show_error(self, title: str, message: str) -> None:
        QMessageBox.critical(self, title, message)

    def confirm(
        self, title: str, message: str, *, dangerous: bool = False
    ) -> bool:
        del dangerous  # both render as a yes/no question in Qt
        answer = QMessageBox.question(self, title, message)
        return answer == QMessageBox.StandardButton.Yes

    def is_closing(self) -> bool:
        return self._closing

    def refresh_controls(self) -> None:
        state = resolve_rig_control_state(
            active_kind=self.operations.active_kind,
            closing=self._closing,
            safety_tripped=bool(getattr(self.safety, "is_tripped", False)),
            hardware_offline=self._hardware_offline,
            engine_running=self.engine.is_busy(),
        )
        self.bus_off_button.setEnabled(state.shutdown_actions)
        self.reset_safety_button.setEnabled(state.reset_safety)
        # E-stop deliberately untouched: no state may disable it.

    def refresh_confirm(self) -> None:
        """No wavegen/recall buttons yet; arrives with the run screen."""

    def smu_state_changed(self) -> None:
        self.smu_status_label.setText(
            smu_status_line(
                setpoint_v=getattr(self.smu, "setpoint_v", None),
                current_a=None,
                output_on=bool(getattr(self.smu, "output_is_on", False)),
            )
        )
        self.refresh_controls()

    def set_tuning(self, active: bool, *, autotune: bool = False) -> None:
        del autotune
        if active:
            self._status.showMessage("Tuner running...")

    def voltage_tune_stopping(self) -> None:
        """No tune button yet; arrives with the run screen."""

    def flash(self, message: str) -> None:
        self._status.showMessage(message, 4000)

    # -- closing -------------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._resources_closed:
            self.bridge.close()
            event.accept()
            return
        event.ignore()
        if self._closing:
            return
        self._closing = True
        self.operations.begin_stopping()
        self.refresh_controls()
        self._status.showMessage(
            "Closing safely: cancelling work and shutting outputs down..."
        )
        self.engine.cancel()
        threading.Thread(
            target=self._shutdown_worker, name="qt-close", daemon=False
        ).start()

    def _shutdown_worker(self) -> None:
        """Loop until shutdown is confirmed — the same contract as Tk.

        The window never closes over live outputs: every failed attempt is
        logged at critical and retried, keeping the process alive for
        recovery rather than abandoning an energised rig.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                outputs_safe = self.safety.shutdown_outputs()
            except Exception:
                outputs_safe = False
                log.exception("Output shutdown attempt failed during close")
            try:
                self.engine.cancel_and_join(timeout=2.0)
                engine_stopped = not self.engine.is_busy()
            except Exception:
                engine_stopped = False
                log.exception("Engine join attempt failed during close")
            workers_stopped = self.pool.join_all(timeout=2.0)
            if outputs_safe and engine_stopped and workers_stopped:
                break
            detail = (
                "Waiting for safe shutdown "
                f"(outputs={outputs_safe}, engine={engine_stopped}, "
                f"workers={workers_stopped})"
            )
            log.critical("%s; attempt %d", detail, attempt)
            self.dispatcher.post(self._status.showMessage, detail)
            time.sleep(0.25)

        try:
            self._close_resources()
        except BaseException:
            log.exception("Application resource shutdown failed")
        self._resources_closed = True
        self.dispatcher.post(self.close)
