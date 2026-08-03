"""PyQt6 window: operations spine plus the experiment workspace.

Implements the same ``RigUi`` protocol as the Tk window, so
``RigOperations`` runs unchanged behind it, and drives the experiment
engine through the same toolkit-free policy modules: ``run_request`` builds
the parameters, ``experiment_ops`` decides preconditions and cancel
routing, ``resolve_rig_control_state`` decides what is enabled.

Working against ``--simulate``: full experiment runs (start, pause,
cancel, overwrite confirmation), Apply Wavegen Settings, Tune DC Voltage
Now, Bus Off, Reset Safety, EMERGENCY STOP, live plot and telemetry.
Planner, tracker, analytics and configuration screens are still Tk; the
composition root offers this window only under ``--simulate``.

Threading contract, same as Tk: workers never touch a widget. Completion
callbacks arrive through :class:`QtDispatcher`; broadcast telemetry arrives
through :class:`EventBridge`. Both ride Qt's queued signal delivery.
"""

from __future__ import annotations

import logging
import threading
import time
from functools import partial
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from gan_fet.core.experiment import EngineCallbacks
from gan_fet.core.models import ExperimentParams, ExperimentState
from gan_fet.ui.operations.experiment_ops import (
    APPLY_FIRST_PROMPT,
    CancelTarget,
    ExperimentAction,
    cancel_target,
    experiment_precondition,
    run_finished_report,
)
from gan_fet.ui.operations.rig_control import RigOperations
from gan_fet.ui.operations.rig_ops import (
    VoltageTuneAction,
    voltage_tune_precondition,
)
from gan_fet.ui.operations.worker_pool import WorkerPool
from gan_fet.ui.param_options import OPTION_KEYS, default_label
from gan_fet.ui.presentation import (
    HARDWARE_OPERATION_LABELS,
    mode_banner_presentation,
)
from gan_fet.ui.refusal import Refusal
from gan_fet.ui.run_request import (
    InputRejected,
    build_experiment_params,
    build_matrix_point,
    require_populated_options,
)
from gan_fet.ui.smu_status import smu_status_line
from gan_fet.ui.telemetry_format import (
    format_amps,
    format_frequency,
    format_milliamps,
    format_volts,
    last_current_text,
)
from gan_fet.ui.widgets import OperationCoordinator, resolve_rig_control_state
from gan_fet.ui_qt.dispatcher import QtDispatcher
from gan_fet.ui_qt.event_bridge import EventBridge
from gan_fet.ui_qt.params import ParamSelector
from gan_fet.ui_qt.plot import QtLivePlot

log = logging.getLogger(__name__)

_ESTOP_STYLE = (
    "background-color: #b71c1c; color: white; font-weight: bold;"
    " padding: 8px 16px;"
)

_PARAM_TITLES = {
    "configurations": "Configuration:",
    "frequencies": "Frequency:",
    "duties": "Duty Cycle:",
    "temperatures": "Temperature:",
    "voltages": "Voltage:",
}

#: Card order matches the Tk telemetry panel: the two controlled
#: quantities first.
_TELEMETRY_CARDS = (
    ("freq_hz", "f_sw", format_frequency),
    ("vds_peak", "Vds peak", format_volts),
    ("dc_volts", "DC bus", format_volts),
    ("dc_current_a", "DC input", format_milliamps),
    ("rms_current_a", "I RMS", format_amps),
    ("isw_rms_a", "Isw RMS", format_amps),
)


class QtMainWindow(QMainWindow):
    """Implements ``RigUi``; owns the coordinator, pool and dispatcher."""

    def __init__(
        self,
        *,
        settings,
        db,
        engine,
        safety,
        smu,
        wavegen_controller,
        is_simulated: bool,
        close_resources=lambda: None,
        hardware_offline: bool = False,
        bridge: Optional[EventBridge] = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("GaN FET Characterisation (Qt preview)")
        self.settings = settings
        self.db = db
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

        self._param_options = {
            "configurations": list(settings.default_configurations),
            "frequencies": list(settings.default_frequencies),
            "duties": list(settings.default_duties),
            "temperatures": list(settings.default_temperatures),
            "voltages": list(settings.default_voltages),
        }

        self._build_ui(is_simulated)
        self._wire_engine()
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
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._banner)

        body = QWidget()
        body_layout = QHBoxLayout(body)
        left = QVBoxLayout()
        body_layout.addLayout(left, stretch=3)

        # Device row.
        device_row = QHBoxLayout()
        device_row.addWidget(QLabel("Device Name:"))
        self.device_combo = QComboBox()
        self.device_combo.setEditable(True)
        self._reload_devices()
        device_row.addWidget(self.device_combo, stretch=1)
        left.addLayout(device_row)

        # Matrix parameter selectors.
        self.param_selectors: dict[str, ParamSelector] = {}
        for key in OPTION_KEYS:
            selector = ParamSelector(
                _PARAM_TITLES[key],
                [
                    (value, default_label(key, value))
                    for value in self._param_options[key]
                ],
            )
            self.param_selectors[key] = selector
            left.addWidget(selector)

        # Run options.
        options_row = QHBoxLayout()
        options_row.addWidget(QLabel("Duration (min):"))
        self.duration_entry = QLineEdit("1.0")
        self.duration_entry.setMaximumWidth(90)
        options_row.addWidget(self.duration_entry)
        self.tune_voltage_check = QCheckBox("Tune DC Voltage before run")
        options_row.addWidget(self.tune_voltage_check)
        self.tune_frequency_check = QCheckBox(
            "Tune frequency at operating point"
        )
        self.tune_frequency_check.setChecked(
            bool(
                getattr(
                    self.settings, "tune_frequency_at_operating_point", True
                )
            )
        )
        options_row.addWidget(self.tune_frequency_check)
        options_row.addStretch(1)
        left.addLayout(options_row)

        # Action buttons.
        actions_row = QHBoxLayout()
        self.apply_button = QPushButton("Apply Wavegen Settings")
        self.apply_button.clicked.connect(lambda: self._apply_wavegen())
        actions_row.addWidget(self.apply_button)
        self.start_button = QPushButton("Start Experiment")
        self.start_button.clicked.connect(self._start_experiment)
        actions_row.addWidget(self.start_button)
        self.pause_button = QPushButton("Pause")
        self.pause_button.clicked.connect(self.engine.toggle_pause)
        actions_row.addWidget(self.pause_button)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._cancel_experiment)
        actions_row.addWidget(self.cancel_button)
        actions_row.addStretch(1)
        left.addLayout(actions_row)

        # SMU row.
        smu_row = QHBoxLayout()
        self.smu_status_label = QLabel()
        smu_row.addWidget(self.smu_status_label, stretch=1)
        self.voltage_tune_button = QPushButton("Tune DC Voltage Now")
        self.voltage_tune_button.clicked.connect(self._tune_voltage_now)
        smu_row.addWidget(self.voltage_tune_button)
        self.bus_off_button = QPushButton("Bus Off")
        self.bus_off_button.clicked.connect(self.rig.bus_off)
        smu_row.addWidget(self.bus_off_button)
        self.reset_safety_button = QPushButton("Reset Safety")
        self.reset_safety_button.clicked.connect(self.rig.reset_safety)
        smu_row.addWidget(self.reset_safety_button)
        left.addLayout(smu_row)

        self.last_current_label = QLabel(last_current_text(None))
        left.addWidget(self.last_current_label)

        # Telemetry cards.
        telemetry = QGridLayout()
        self._telemetry_values: dict[str, QLabel] = {}
        for column, (key, title, formatter) in enumerate(_TELEMETRY_CARDS):
            title_label = QLabel(title)
            title_label.setStyleSheet("font-weight: bold;")
            value_label = QLabel(formatter(None))
            telemetry.addWidget(
                title_label, 0, column, Qt.AlignmentFlag.AlignCenter
            )
            telemetry.addWidget(
                value_label, 1, column, Qt.AlignmentFlag.AlignCenter
            )
            self._telemetry_values[key] = value_label
        left.addLayout(telemetry)
        left.addStretch(1)

        self.plot = QtLivePlot()
        body_layout.addWidget(self.plot, stretch=4)
        outer.addWidget(body, stretch=1)
        self.setCentralWidget(central)

        self._status = QStatusBar(self)
        self.setStatusBar(self._status)
        self._status.showMessage("Ready.")
        self.bridge.status_updated.connect(
            lambda event: self._status.showMessage(event.message)
        )
        self.bridge.safety_trip.connect(self._show_trip)
        self.bridge.sample_acquired.connect(self._on_sample_event)
        self.bridge.measurement.connect(self._on_measurement_event)

    def _wire_engine(self) -> None:
        self.engine.callbacks = EngineCallbacks(
            on_state=lambda state: self.dispatcher.post(
                self._on_engine_state, state
            ),
            on_finished=lambda success, message: self.dispatcher.post(
                self._on_run_finished, success, message
            ),
            confirm_overwrite=self._confirm_overwrite,
            report_error=lambda title, message: self.dispatcher.post(
                self.show_error, title, message
            ),
        )

    def _reload_devices(self) -> None:
        current = self.device_combo.currentText()
        self.device_combo.clear()
        try:
            names = self.db.list_devices()
        except Exception:
            names = []
        self.device_combo.addItems(list(names))
        if current:
            self.device_combo.setCurrentText(current)

    def banner_text(self) -> str:
        return self._banner.text()

    # -- event handlers -------------------------------------------------------

    def _show_trip(self, event) -> None:
        self._status.showMessage(f"SAFETY TRIP: {event.reason}")
        self.refresh_controls()

    def _on_sample_event(self, event) -> None:
        self.last_current_label.setText(last_current_text(event.amps))
        self.plot.append(event.elapsed_s, event.amps, event.smu_voltage)
        self.update_telemetry(
            freq_hz=event.frequency_hz,
            vds_peak=event.vds_peak,
            dc_volts=(
                event.dc_voltage
                if event.dc_voltage is not None
                else event.smu_voltage
            ),
            dc_current_a=event.amps,
            rms_current_a=event.rms_current,
            isw_rms_a=event.isw_rms,
        )
        self.smu_state_changed()

    def _on_measurement_event(self, event) -> None:
        # Telemetry only: search readings must not reach the plot or the
        # database, which is why this is a separate event type.
        self.update_telemetry(
            freq_hz=event.frequency_hz,
            vds_peak=event.vds_peak,
            dc_volts=event.bus_voltage,
            dc_current_a=event.dc_current,
        )

    def update_telemetry(self, **readings: Optional[float]) -> None:
        """Update only the provided readings; ``None`` keeps the last value."""
        for key, title, formatter in _TELEMETRY_CARDS:
            value = readings.get(key)
            if value is not None:
                self._telemetry_values[key].setText(formatter(value))

    # -- experiment flow ------------------------------------------------------

    def _read_selection(self) -> dict:
        return {
            "device_name": self.device_combo.currentText(),
            "config": self.param_selectors["configurations"].value(),
            "frequency_hz": self.param_selectors["frequencies"].value(),
            "duty_pct": self.param_selectors["duties"].value(),
            "temperature_c": self.param_selectors["temperatures"].value(),
            "voltage_v": self.param_selectors["voltages"].value(),
        }

    def _build_params(self) -> Optional[ExperimentParams]:
        try:
            require_populated_options(self._param_options, OPTION_KEYS)
            point = build_matrix_point(**self._read_selection())
            return build_experiment_params(
                point,
                self.duration_entry.text(),
                tune_voltage=self.tune_voltage_check.isChecked(),
                tune_frequency=self.tune_frequency_check.isChecked(),
            )
        except InputRejected as rejected:
            self._show_refusal(
                Refusal(rejected.title, rejected.message, severity="error")
            )
            return None

    def _show_refusal(self, refusal: Refusal) -> None:
        if refusal.severity == "info":
            self.show_info(refusal.title, refusal.message)
        elif refusal.severity == "error":
            self.show_error(refusal.title, refusal.message)
        else:
            QMessageBox.warning(self, refusal.title, refusal.message)

    def _start_experiment(self) -> None:
        decision = experiment_precondition(
            hardware_offline=self._hardware_offline,
            safety_tripped=bool(getattr(self.safety, "is_tripped", False)),
            busy=self.operations.busy,
        )
        if decision.action is ExperimentAction.HARDWARE_OFFLINE:
            self.hardware_online(HARDWARE_OPERATION_LABELS["experiment"])
            return
        if decision.action is ExperimentAction.REFUSE:
            assert decision.refusal is not None
            self._show_refusal(decision.refusal)
            return
        if decision.action is ExperimentAction.REPORT_BUSY:
            self.report_busy(self.operations.active_kind)
            return
        params = self._build_params()
        if params is None:
            return
        if self.wavegen_controller.has_pending_changes(
            params.point.config,
            params.point.frequency_hz,
            params.point.duty_pct,
        ):
            # Declining cancels the start: a run recorded against settings
            # the wavegen was not using is worse than no run at all.
            if self.confirm(*APPLY_FIRST_PROMPT):
                self._apply_wavegen(
                    after_success=partial(self._launch_experiment, params)
                )
            return
        self._launch_experiment(params)

    def _launch_experiment(self, params: ExperimentParams) -> bool:
        token = self.operations.try_begin("experiment")
        if token is None:
            self.report_busy(self.operations.active_kind)
            return False
        self.refresh_controls()
        self.plot.reset(params.point.describe())
        if not self.engine.start(params):
            self.operations.finish(token)
            self.refresh_controls()
            self.show_info(
                "Rig Busy", "The experiment engine could not start."
            )
            return False
        return True

    def _cancel_experiment(self) -> None:
        if cancel_target(self.operations.active_kind) is (
            CancelTarget.VOLTAGE_TUNE
        ):
            if self.rig.request_voltage_tune_stop():
                return
        self.operations.cancel_active()
        self.engine.cancel()

    def _confirm_overwrite(self, description: str) -> bool:
        active = self.operations.active
        return bool(
            self.dispatcher.call(
                self.confirm,
                "Overwrite Confirmation",
                "A stored result already exists for this test point:\n\n"
                f"{description}\n\nOverwrite it with new results?",
                cancel_event=(
                    active.cancel_event if active is not None else None
                ),
            )
        )

    def _on_engine_state(self, state: ExperimentState) -> None:
        self.pause_button.setText(
            "Resume" if state == ExperimentState.PAUSED else "Pause"
        )
        self.refresh_controls()

    def _on_run_finished(self, success: bool, message: str) -> None:
        if self._closing:
            return
        active = self.operations.active
        if active is not None and active.kind == "experiment":
            self.operations.finish(active)
        self._on_engine_state(ExperimentState.IDLE)
        self.smu_state_changed()
        self._reload_devices()
        report = run_finished_report(
            success=success, message=message, validation=False
        )
        self._status.showMessage(report.status)
        if report.dialog is not None:
            self.show_info(*report.dialog)

    def _apply_wavegen(self, after_success=None) -> bool:
        selection = self._read_selection()
        return self.rig.apply_wavegen(
            config=selection["config"],
            frequency_hz=selection["frequency_hz"],
            duty_pct=selection["duty_pct"],
            after_success=after_success,
        )

    def _tune_voltage_now(self) -> None:
        if self.rig.request_voltage_tune_stop():
            return
        selection = self._read_selection()
        target = selection["voltage_v"]
        decision = voltage_tune_precondition(
            hardware_offline=self._hardware_offline,
            safety_tripped=bool(getattr(self.safety, "is_tripped", False)),
            target_peak_v=(None if target is None else float(target)),
            max_vds_peak_v=self.settings.safety.max_vds_peak_v,
            wavegen_pending=self.wavegen_controller.has_pending_changes(
                selection["config"],
                selection["frequency_hz"],
                selection["duty_pct"],
            ),
        )
        launch = partial(
            self.rig.launch_voltage_tune,
            target_peak_v=float(target) if target is not None else 0.0,
            config=selection["config"],
        )
        if decision.action is VoltageTuneAction.HARDWARE_OFFLINE:
            self.hardware_online(HARDWARE_OPERATION_LABELS["voltage_tune"])
        elif decision.action is VoltageTuneAction.REFUSE:
            assert decision.refusal is not None
            self._show_refusal(decision.refusal)
        elif decision.action is VoltageTuneAction.APPLY_WAVEGEN_FIRST:
            assert decision.prompt is not None
            if self.confirm(*decision.prompt):
                self._apply_wavegen(after_success=launch)
        else:
            launch()

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
        self.show_info("Rig Busy", f"Cannot start: waiting to {label}.")

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
        self.device_combo.setEnabled(state.edit_inputs)
        for selector in self.param_selectors.values():
            selector.set_enabled(state.edit_inputs)
        self.duration_entry.setEnabled(state.edit_inputs)
        self.tune_voltage_check.setEnabled(state.edit_inputs)
        self.tune_frequency_check.setEnabled(state.edit_inputs)
        self.apply_button.setEnabled(state.hardware_actions)
        self.start_button.setEnabled(state.hardware_actions)
        self.cancel_button.setEnabled(state.cancel_operation)
        self.bus_off_button.setEnabled(state.shutdown_actions)
        self.reset_safety_button.setEnabled(state.reset_safety)
        if state.stop_voltage_tune:
            self.voltage_tune_button.setEnabled(True)
            self.voltage_tune_button.setText("Stop Voltage Tune")
        else:
            self.voltage_tune_button.setEnabled(state.hardware_actions)
            self.voltage_tune_button.setText("Tune DC Voltage Now")
        # E-stop deliberately untouched: no state may disable it.

    def refresh_confirm(self) -> None:
        """Recall Tuned Frequency arrives with the history screens."""

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
        self.refresh_controls()

    def voltage_tune_stopping(self) -> None:
        self.voltage_tune_button.setEnabled(False)
        self.voltage_tune_button.setText("Stopping...")

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
