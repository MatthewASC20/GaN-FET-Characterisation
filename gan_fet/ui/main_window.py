"""Main application window.

All engine/sequence callbacks arrive on worker threads and are marshalled
through the UI-owned dispatcher. Worker threads never invoke Tcl directly.
"""

from __future__ import annotations

import logging
import math
import threading
import time
import tkinter as tk
from enum import IntEnum
from functools import partial
from tkinter import messagebox, simpledialog, ttk
from typing import Any, Callable, Dict, List, Optional, Tuple

from gan_fet.core.autotune import (
    FrequencyRampAborted,
    WavegenController,
    find_prior_tuned_frequency,
)
from gan_fet.ui.command_log_view import CommandLogConsole
from gan_fet.ui.device_actions import (
    Refusal,
    device_after_removal,
    removal_confirmation,
    removal_refusal,
    removal_report,
    validated_new_device_name,
)
from gan_fet.ui.run_request import (
    InputRejected,
    build_experiment_params,
    build_matrix_point,
    require_populated_options,
    tuning_candidate,
)
from gan_fet.ui.operations.rig_ops import ZvsAction, zvs_precondition
from gan_fet.ui.operations.worker_pool import WorkerPool
from gan_fet.ui.panels.device_bar import DeviceBar
from gan_fet.ui.panels.run_controls import RunControls
from gan_fet.ui.panels.smu_panel import SmuPanel
from gan_fet.ui.panels.telemetry_panel import TelemetryPanel
from gan_fet.ui.smu_status import smu_status_line
from gan_fet.ui.telemetry_format import last_current_text
from gan_fet.ui.param_options import (
    OPTION_KEYS as _OPTION_KEYS,
    default_label,
    normalize_options,
)
from gan_fet.core.events import (
    SampleAcquiredEvent,
    StatusUpdatedEvent,
    bus,
)
from gan_fet.core.experiment import EngineCallbacks, ExperimentEngine
from gan_fet.core.models import (
    ExperimentParams,
    ExperimentState,
    MatrixPoint,
    sanitize_device_name,
)
from gan_fet.core.safety import SafetyMonitor, SafetyTrip
from gan_fet.core.sequence import AutoSequence, SequenceCallbacks, build_plan
from gan_fet.instruments.base import SmuInterface
from gan_fet.settings import SIMULATION_DEFAULT_DEVICE_NAME, Settings
from gan_fet.sheets.sync import SheetsSync
from gan_fet.storage import export as export_mod
from gan_fet.storage import reports as reports_mod
from gan_fet.storage.db import Database
from gan_fet.ui.config_tab import (
    InstrumentConfigEditor,
    ParameterListEditor,
    RampRatesSliderEditor,
    SmuLimitsEditor,
)
from gan_fet.ui.analytics_tab import AnalyticsTab
from gan_fet.ui.planner_tab import PlannerTab
from gan_fet.ui.plot import LivePlot
from gan_fet.ui.tracker_view import UpNextView
from gan_fet.ui.widgets import (
    resolve_confirm_presentation,
    OperationCoordinator,
    OperationToken,
    ParamButtonGroup,
    StatusBar,
    UiDispatcher,
    call_on_ui_thread,
    parse_positive_duration,
    resolve_rig_control_state,
    show_temporary_popup,
)

log = logging.getLogger(__name__)

DEFAULT_GEOMETRY = "1700x950"
SIMULATION_VALIDATION_DURATION_MINUTES = 0.1

# Re-exported: tests and sibling modules import these from here, and the
# behaviour now lives in ui/param_options.py where it can be tested headlessly.
OPTION_KEYS = _OPTION_KEYS

EXPERIMENT_CONTROL_COLUMNS = 3
EXPERIMENT_PLOT_COLUMN = EXPERIMENT_CONTROL_COLUMNS


class ExperimentRow(IntEnum):
    """Named rows for the left-hand experiment controls."""

    DEVICE = 0
    PARAMETER_FIRST = 1
    RUN_OPTIONS = 6
    RUN_BUTTONS = 7
    TELEMETRY = 8
    SMU = 9
    ACTIONS = 10
    FLEX_SPACER = 11


EXPERIMENT_PLOT_ROWSPAN = int(ExperimentRow.FLEX_SPACER) + 1

HARDWARE_OPERATION_LABELS = {
    "apply_wavegen": "apply wavegen settings",
    "autotune": "autotune the wavegen",
    "bus_off": "control the bus output",
    "experiment": "start an experiment",
    "reset_safety": "reset the safety interlock",
    "sequence": "start an auto sequence",
    "simulation_validation": "start the simulated validation run",
    "zvs": "run a ZVS search",
}

# Confirm-button state colours (unchanged from v1)
COLOR_PENDING = "#e53935"    # red — selection differs from what's on the wavegen
COLOR_TUNING = "#FBC02D"     # yellow — a better (tuned) frequency is available
COLOR_OK = "#43a047"         # green — instrument matches the selection

#: Confirm-button appearance for each state resolve_confirm_presentation can
#: return. Keeping the mapping beside the colours means adding a state fails
#: here rather than silently falling through to a default.
CONFIRM_STYLES: dict[str, dict[str, str]] = {
    "pending": {"bg": COLOR_PENDING, "fg": "white"},
    "tuning": {"bg": COLOR_TUNING, "fg": "black"},
    "ready": {"bg": COLOR_OK, "fg": "white"},
}


def _default_label(key: str, value: Any) -> str:
    """Kept as a module-level name: it is bound into partials at build time."""
    return default_label(key, value)


def mode_banner_presentation(is_simulated: bool) -> tuple[str, str]:
    """Return the persistent operator-facing mode identity and colour."""
    if is_simulated:
        return (
            "SIMULATION — VIRTUAL INSTRUMENTS — NO BENCH I/O — "
            "NOT MEASURED DATA",
            "#6a1b9a",
        )
    return ("LIVE HARDWARE — REAL BENCH OUTPUTS", "#b71c1c")


class MainWindow(tk.Tk):
    def __init__(
        self,
        settings: Settings,
        db: Database,
        engine: ExperimentEngine,
        wavegen_controller: WavegenController,
        safety: SafetyMonitor,
        smu: SmuInterface,
        sheets: SheetsSync,
        is_simulated: bool = False,
        hardware_offline: bool = False,
        close_resources: Optional[Callable[[], None]] = None,
    ):
        super().__init__()
        self.ui_dispatcher = UiDispatcher(self)
        self.title("GaN Device Test Runner" + (" [SIMULATION MODE]" if is_simulated else ""))
        self.geometry(DEFAULT_GEOMETRY)
        self.protocol("WM_DELETE_WINDOW", self._on_closing)

        self.settings = settings
        self.db = db
        self.engine = engine
        self.wavegen_controller = wavegen_controller
        self.safety = safety
        self.smu = smu
        self.sheets = sheets
        self.is_simulated = is_simulated
        self.hardware_offline = bool(hardware_offline)
        self._close_resources = close_resources or (lambda: None)

        self._ui_ready = False
        self._tuner_busy = False
        self._simulation_validation_active = False
        self._active_device = ""
        self._closing = False
        self._resources_closed = False
        self.operations = OperationCoordinator()
        self._worker_pool = WorkerPool()
        self._emergency_worker: Optional[threading.Thread] = None
        self._event_unsubscribers: list[Callable[[], None]] = []

        self.default_param_options = {
            key: [(v, _default_label(key, v)) for v in values]
            for key, values in (
                ("configurations", settings.default_configurations),
                ("frequencies", settings.default_frequencies),
                ("duties", settings.default_duties),
                ("temperatures", settings.default_temperatures),
                ("voltages", settings.default_voltages),
            )
        }
        self.param_options: Dict[str, List[Tuple[Any, str]]] = {
            key: list(options) for key, options in self.default_param_options.items()
        }
        self.parameter_editors: Dict[str, ParameterListEditor] = {}

        self._init_variables()
        self._wire_engine()
        self.sequence = AutoSequence(
            db,
            engine,
            wavegen_controller,
            all_configs=self._values("configurations"),
            callbacks=SequenceCallbacks(
                on_status=self._status_async,
                on_finished=self._on_sequence_finished,
                prompt_operator=self._prompt_operator,
            ),
        )
        self.safety.on_trip = self._on_safety_trip

        self._build_ui()
        self._subscribe_events()
        self._ui_ready = True
        self._restore_last_params()

        if self.is_simulated:
            simulation_device = (
                self.device_name_var.get().strip()
                or SIMULATION_DEFAULT_DEVICE_NAME
            )
            self.device_name_var.set(simulation_device)
            self._active_device = simulation_device
            self.db.get_or_create_device(simulation_device)
            self._load_device_options(simulation_device)
            self._refresh_device_dropdown()

        if not self.device_name_var.get().strip():
            devices = self.db.list_devices()
            if devices:
                self.device_name_var.set(devices[0])
                self._active_device = devices[0]
                self._load_device_options(devices[0])

        self._apply_options_to_ui()
        self._setup_traces()
        self._refresh_confirm_state()
        self._update_smu_panel()
        self._refresh_control_states()

        if self.hardware_offline:
            self.notebook.select(self.configuration_tab)
            self.after(
                300,
                lambda: messagebox.showwarning(
                    "Instruments Unreachable",
                    "Could not connect to instruments at configured IP addresses.\n\n"
                    "The Configuration tab has been automatically selected so you can "
                    "update instrument IP addresses and ports.",
                    parent=self,
                ),
            )

    def _subscribe_events(self) -> None:
        """Route engine bus events into the UI-owned dispatcher queue."""
        self._event_unsubscribers.extend(
            (
                bus.subscribe(
                    SampleAcquiredEvent,
                    self._queue_sample_event,
                ),
                bus.subscribe(
                    StatusUpdatedEvent,
                    self._queue_status_event,
                ),
            )
        )

    def _queue_sample_event(self, event: SampleAcquiredEvent) -> None:
        self.ui_dispatcher.post(self._on_sample_event, event)

    def _queue_status_event(self, event: StatusUpdatedEvent) -> None:
        self.ui_dispatcher.post(self.status_bar.set_message, event.message)

    def _on_sample_event(self, event: SampleAcquiredEvent) -> None:
        self.last_current_label.config(text=last_current_text(event.amps))
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
        self._update_smu_panel()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    def _init_variables(self) -> None:
        self.device_name_var = tk.StringVar(value="")
        self.config_var = tk.StringVar(value=self._first("configurations"))
        self.frequency_var = tk.IntVar(value=self._first("frequencies"))
        self.duty_var = tk.IntVar(value=self._first("duties"))
        self.temperature_var = tk.IntVar(value=self._first("temperatures"))
        self.voltage_var = tk.IntVar(value=self._first("voltages"))
        self.find_zvs_var = tk.BooleanVar(value=self.settings.find_zvs_before_run)
        self.tune_frequency_var = tk.BooleanVar(
            value=self.settings.tune_frequency_at_operating_point
        )
        self.show_zvs_sweep_var = tk.BooleanVar(
            value=self.settings.show_zvs_voltage_sweep
        )

    def _on_tune_frequency_toggled(self) -> None:
        self.settings.tune_frequency_at_operating_point = bool(
            self.tune_frequency_var.get()
        )

    def _on_show_zvs_sweep_toggled(self) -> None:
        self.settings.show_zvs_voltage_sweep = bool(self.show_zvs_sweep_var.get())
        self._apply_zvs_visibility()
        self._refresh_control_states()

    def _apply_zvs_visibility(self) -> None:
        """Show or hide the voltage-only ZVS control per the Config setting."""
        controls = getattr(self, "run_controls", None)
        if controls is None:
            return
        controls.set_zvs_sweep_visible(bool(self.show_zvs_sweep_var.get()))

    def _first(self, key: str) -> Any:
        options = self.param_options.get(key) or []
        return options[0][0] if options else ""

    def _values(self, key: str) -> List[Any]:
        return [v for v, _ in self.param_options.get(key, [])]

    def _wire_engine(self) -> None:
        self.engine.callbacks = EngineCallbacks(
            # Sampling/status are delivered once through typed EventBus events.
            on_sample=lambda _elapsed, _amps: None,
            on_state=self._queue_engine_state,
            on_status=lambda _message: None,
            on_finished=self._queue_run_finished,
            confirm_overwrite=self._confirm_overwrite,
            report_error=self._queue_engine_error,
        )
        self.engine.on_run_completed = self._on_run_completed_worker

    def _queue_engine_state(self, state: ExperimentState) -> None:
        self.ui_dispatcher.post(self._on_engine_state, state)

    def _queue_run_finished(self, success: bool, message: str) -> None:
        self.ui_dispatcher.post(self._on_run_finished, success, message)

    def _queue_engine_error(self, title: str, message: str) -> None:
        self.ui_dispatcher.post(
            messagebox.showerror, title, message, parent=self
        )

    def _build_ui(self) -> None:
        banner_text, banner_colour = mode_banner_presentation(self.is_simulated)
        self.mode_banner = tk.Label(
            self,
            text=banner_text,
            bg=banner_colour,
            fg="white",
            font=("TkDefaultFont", 11, "bold"),
            padx=10,
            pady=7,
        )
        self.mode_banner.grid(row=0, column=0, sticky="ew")

        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=1, column=0, sticky="nsew")
        self.experiment_tab = ttk.Frame(self.notebook)
        self.planner_tab = PlannerTab(
            self.notebook,
            db=self.db,
            get_device_name=lambda: self.device_name_var.get(),
            on_start_sequence=self._start_planned_sequence,
            on_delete_run=self.sheets.enqueue_clear,
            can_delete_run=lambda: not self.operations.busy and not self._closing,
        )
        self.analytics_tab = AnalyticsTab(
            self.notebook,
            db=self.db,
            get_device_name=lambda: self.device_name_var.get(),
            on_generate_report=self._generate_report,
        )
        self.configuration_tab = ttk.Frame(self.notebook)
        self.command_log_tab = CommandLogConsole(
            self.notebook,
            is_simulated=self.is_simulated,
            on_emergency_stop=self._emergency_stop,
        )
        self.notebook.add(self.experiment_tab, text="Experiment")
        self.notebook.add(self.planner_tab, text="Test Planner")
        self.notebook.add(self.analytics_tab, text="Analytics")
        self.notebook.add(self.command_log_tab, text="Command Log")
        self.notebook.add(self.configuration_tab, text="Configuration")

        self.main_frame = tk.Frame(self.experiment_tab)
        self.main_frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

        self._build_device_row()
        self._build_parameter_groups()
        self._build_run_controls()
        self._build_telemetry_panel()
        self._build_smu_panel()
        self._build_action_buttons()
        self._build_plot()
        self._build_tracker()
        self._build_configuration_tab()

        self.status_bar = StatusBar(self)
        self.status_bar.grid(row=2, column=0, sticky="ew", pady=(5, 0))

        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.experiment_tab.grid_rowconfigure(0, weight=1)
        self.experiment_tab.grid_rowconfigure(1, weight=1)
        self.experiment_tab.grid_columnconfigure(0, weight=1)
        self.main_frame.grid_columnconfigure(EXPERIMENT_PLOT_COLUMN, weight=1)
        self.main_frame.grid_rowconfigure(ExperimentRow.FLEX_SPACER, weight=1)

    def _build_device_row(self) -> None:
        self.device_bar = DeviceBar(
            self.main_frame,
            row=ExperimentRow.DEVICE,
            textvariable=self.device_name_var,
            devices=self.db.list_devices(),
            on_commit=self._on_device_committed,
            on_add=self._prompt_add_device,
            on_remove=self._prompt_remove_device,
        )

    def _show_refusal(self, refusal: Refusal) -> None:
        show = {
            "info": messagebox.showinfo,
            "error": messagebox.showerror,
        }.get(refusal.severity, messagebox.showwarning)
        show(refusal.title, refusal.message, parent=self)

    def _prompt_add_device(self) -> None:
        try:
            clean_name = validated_new_device_name(
                simpledialog.askstring(
                    "Add Device",
                    "Enter new GaN device name (e.g. EPC2001C, GS66508B):",
                    parent=self,
                )
            )
        except InputRejected as rejected:
            messagebox.showerror(rejected.title, rejected.message, parent=self)
            return
        if clean_name is None:
            return

        self.db.get_or_create_device(clean_name)
        self.device_name_var.set(clean_name)
        self._on_device_committed()
        messagebox.showinfo(
            "Device Added",
            f"Device '{clean_name}' successfully added and selected.",
            parent=self,
        )

    def _prompt_remove_device(self) -> None:
        device_name = self.device_name_var.get().strip()
        refusal = removal_refusal(
            busy=self.operations.busy,
            device_name=device_name,
            known_devices=self.db.list_devices(),
        )
        if refusal is not None:
            self._show_refusal(refusal)
            return

        title, message = removal_confirmation(device_name)
        if not messagebox.askyesno(title, message, parent=self, icon="warning"):
            return

        if not self.db.delete_device(device_name):
            return
        self._active_device = ""
        next_device = device_after_removal(self.db.list_devices())
        self.device_name_var.set(next_device)
        self._refresh_device_dropdown()
        if next_device:
            self._on_device_committed()
        else:
            self._clear_device_context()
        messagebox.showinfo(*removal_report(device_name), parent=self)

    def _build_parameter_groups(self) -> None:
        specs = [
            ("frequencies", self.frequency_var, "Frequency"),
            ("temperatures", self.temperature_var, "Temperature"),
            ("duties", self.duty_var, "Duty Cycle"),
            ("configurations", self.config_var, "Configuration"),
            ("voltages", self.voltage_var, "Voltage (Vds pk)"),
        ]
        self.param_groups: Dict[str, ParamButtonGroup] = {}
        for row, (key, variable, label) in enumerate(
            specs, start=ExperimentRow.PARAMETER_FIRST
        ):
            group = ParamButtonGroup(
                self.main_frame, self.param_options[key], variable, label
            )
            group.grid(row=row, column=0, columnspan=3, pady=8, sticky="w")
            self.param_groups[key] = group

    def _build_run_controls(self) -> None:
        self.run_controls = RunControls(
            self.main_frame,
            options_row=ExperimentRow.RUN_OPTIONS,
            buttons_row=ExperimentRow.RUN_BUTTONS,
            default_duration=str(
                self.settings.default_duration_minutes(self.voltage_var.get())
            ),
            find_zvs_var=self.find_zvs_var,
            on_apply_wavegen=self._apply_wavegen,
            on_autotune=self._start_autotune,
        )
        # Enabled state and confirm/autotune styling are resolved by the
        # window, so these stay addressable from it.
        self.duration_entry = self.run_controls.duration_entry
        self.find_zvs_checkbox = self.run_controls.find_zvs_checkbox
        self.last_current_label = self.run_controls.last_current_label
        self.confirm_button = self.run_controls.confirm_button
        self.autotune_button = self.run_controls.autotune_button
        self._apply_zvs_visibility()

    def _build_telemetry_panel(self) -> None:
        self.telemetry_panel = TelemetryPanel(self.main_frame)
        self.telemetry_panel.grid(
            row=ExperimentRow.TELEMETRY,
            column=0,
            columnspan=EXPERIMENT_CONTROL_COLUMNS,
            sticky="ew",
            padx=5,
            pady=5,
        )
        # Seed the frequency card with the selection so it reads the chosen
        # frequency before the first sample rather than a hardcoded default.
        self.telemetry_panel.update_values(freq_hz=self.frequency_var.get())

    def update_telemetry(
        self,
        freq_hz: Optional[float] = None,
        vds_peak: Optional[float] = None,
        dc_volts: Optional[float] = None,
        dc_current_a: Optional[float] = None,
        rms_current_a: Optional[float] = None,
        isw_rms_a: Optional[float] = None,
    ) -> None:
        """Push readings to the panel, filling gaps from what the rig knows.

        Resolving a fallback needs the instruments and the selection, so it
        stays here; the panel only renders what it is handed.
        """
        # A reported 0 Hz is the gate not running, not a measurement, so it
        # falls back to the selection like a missing reading does.
        if (freq_hz is None or freq_hz <= 0) and hasattr(self, "frequency_var"):
            try:
                freq_hz = float(self.frequency_var.get())
            except (ValueError, tk.TclError):
                freq_hz = None
        if dc_volts is None and self.smu is not None:
            dc_volts = getattr(self.smu, "setpoint_v", None)
        if dc_current_a is None:
            dc_current_a = self.engine.last_current

        self.telemetry_panel.update_values(
            freq_hz=freq_hz,
            vds_peak=vds_peak,
            dc_volts=dc_volts,
            dc_current_a=dc_current_a,
            rms_current_a=rms_current_a,
            isw_rms_a=isw_rms_a,
        )

    def _build_smu_panel(self) -> None:
        self.smu_panel = SmuPanel(
            self.main_frame,
            on_find_zvs=self._find_zvs_now,
            on_bus_off=self._bus_off,
            on_reset_safety=self._reset_safety,
            on_emergency_stop=self._emergency_stop,
        )
        self.smu_panel.grid(
            row=ExperimentRow.SMU,
            column=0,
            columnspan=EXPERIMENT_CONTROL_COLUMNS,
            sticky="ew",
            padx=5,
            pady=5,
        )
        # Enabled state is decided by resolve_rig_control_state and applied
        # here, so the buttons stay addressable from the window.
        self.smu_status_label = self.smu_panel.status_label
        self.zvs_button = self.smu_panel.zvs_button
        self.bus_off_button = self.smu_panel.bus_off_button
        self.reset_safety_button = self.smu_panel.reset_safety_button
        self.estop_button = self.smu_panel.estop_button

    def _build_action_buttons(self) -> None:
        bar = tk.Frame(self.main_frame)
        bar.grid(
            row=ExperimentRow.ACTIONS,
            column=0,
            columnspan=EXPERIMENT_CONTROL_COLUMNS,
            pady=10,
            sticky="w",
        )

        start_label = (
            "Start Simulated Experiment"
            if self.is_simulated
            else "Start Experiment"
        )
        self.start_button = ttk.Button(
            bar, text=start_label, command=self._start_experiment
        )
        self.start_button.pack(side="left", padx=(0, 10))
        self.pause_button = ttk.Button(bar, text="Pause", command=self._toggle_pause,
                                       state="disabled")
        self.pause_button.pack(side="left", padx=(0, 10))
        self.cancel_button = ttk.Button(bar, text="Cancel", command=self._cancel_experiment,
                                        state="disabled")
        self.cancel_button.pack(side="left", padx=(0, 10))
        sequence_label = (
            "Start Simulated Sequence"
            if self.is_simulated
            else "Start Auto Sequence"
        )
        self.sequence_button = ttk.Button(
            bar, text=sequence_label, command=self._toggle_sequence
        )
        self.sequence_button.pack(side="left", padx=(0, 10))
        self.report_button = ttk.Button(
            bar, text="Generate Report", command=self._generate_report
        )
        self.report_button.pack(side="left", padx=(0, 10))
        self.export_button = ttk.Button(
            bar, text="Export CSV", command=self._export_csv
        )
        self.export_button.pack(side="left", padx=(0, 10))

        if self.is_simulated:
            self.sim_validation_button = ttk.Button(
                bar,
                text="Run 6 s Procedure Validation",
                command=self._run_simulation_validation,
            )
            self.sim_validation_button.pack(side="left", padx=(10, 0))

    def _run_simulation_validation(self) -> None:
        """Execute a short stored run through the normal production engine."""
        if not self.is_simulated:
            return
        if self._safety_is_tripped():
            messagebox.showwarning(
                "Simulation Safety Interlock",
                "Reset the simulated safety trip before starting validation.",
                parent=self,
            )
            return
        if self.operations.busy:
            self._begin_operation("simulation_validation")
            return

        point = self._validated_current_point()
        if point is None:
            return
        params = ExperimentParams(
            point=point,
            duration_minutes=SIMULATION_VALIDATION_DURATION_MINUTES,
            find_zvs=bool(self.find_zvs_var.get()),
            tune_frequency=bool(self.tune_frequency_var.get()),
        )

        def launch() -> None:
            self._simulation_validation_active = True
            if not self._launch_experiment(params):
                self._simulation_validation_active = False

        self.status_bar.set_message(
            "Preparing a 6-second validation through the normal experiment workflow..."
        )
        if self.wavegen_controller.has_pending_changes(
            params.point.config,
            params.point.frequency_hz,
            params.point.duty_pct,
        ):
            self._apply_wavegen(after_success=launch)
        else:
            launch()

    def _build_plot(self) -> None:
        plot_frame = ttk.LabelFrame(self.main_frame, text="Real-Time Plot")
        plot_frame.grid(
            row=ExperimentRow.DEVICE,
            column=EXPERIMENT_PLOT_COLUMN,
            rowspan=EXPERIMENT_PLOT_ROWSPAN,
            padx=10,
            pady=10,
            sticky="nsew",
        )
        self.plot = LivePlot(plot_frame)

    def _build_tracker(self) -> None:
        tracker_frame = tk.Frame(self.experiment_tab)
        tracker_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        tracker_frame.grid_rowconfigure(0, weight=1)
        tracker_frame.grid_columnconfigure(0, weight=1)

        self.up_next_view = UpNextView(
            tracker_frame,
            self.db,
            get_device_name=lambda: self.device_name_var.get(),
            get_current_params=lambda: (
                self.frequency_var.get(),
                self._values("configurations"),
                self._values("duties"),
                self._values("voltages"),
                self._values("temperatures"),
            ) if hasattr(self, "frequency_var") else None,
        )
        self.up_next_view.grid(row=0, column=0, sticky="nsew")

        self.tracker = self.planner_tab.tracker

    def _build_configuration_tab(self) -> None:
        if self.is_simulated:
            configuration_description = (
                "Simulation settings only. Instrument addresses shown here are "
                "ignored because every connection is virtual. Changes are saved "
                f"to the isolated profile at {self.settings.settings_path}."
            )
        else:
            configuration_description = (
                "Per-device parameter options (stored in the database) and global "
                "instrument/safety settings (stored in settings.json)."
            )
        ttk.Label(
            self.configuration_tab,
            text=configuration_description,
            foreground="#6a1b9a" if self.is_simulated else "",
            wraplength=800,
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(10, 5))

        editors_frame = ttk.Frame(self.configuration_tab)
        editors_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        for col in range(2):
            editors_frame.grid_columnconfigure(col, weight=1, uniform="cfg")

        editor_specs: list[
            tuple[str, str, Callable[[str], Any], Callable[[Any], str]]
        ] = [
            (
                "Configurations",
                "configurations",
                self._parse_configuration,
                partial(_default_label, "configurations"),
            ),
            (
                "Frequencies (Hz)",
                "frequencies",
                self._parse_int,
                partial(_default_label, "frequencies"),
            ),
            (
                "Duty Cycles (%)",
                "duties",
                self._parse_int,
                partial(_default_label, "duties"),
            ),
            (
                "Temperatures (°C)",
                "temperatures",
                self._parse_int,
                partial(_default_label, "temperatures"),
            ),
            (
                "Voltages (V)",
                "voltages",
                self._parse_int,
                partial(_default_label, "voltages"),
            ),
        ]
        for idx, (title, key, parser, label_factory) in enumerate(editor_specs):
            editor = ParameterListEditor(
                editors_frame,
                title,
                self.param_options[key],
                on_change=partial(self._on_options_changed, key),
                value_parser=parser,
                default_label_factory=label_factory,
                default_options=self.default_param_options[key],
            )
            editor.grid(row=idx // 2, column=idx % 2, sticky="nsew", padx=10, pady=10)
            self.parameter_editors[key] = editor

        side = ttk.Frame(self.configuration_tab)
        side.grid(row=1, column=1, sticky="nsew", padx=10, pady=(0, 10))
        InstrumentConfigEditor(side, self.settings, self._on_settings_applied).pack(
            fill="x", pady=(0, 10))
        RampRatesSliderEditor(side, self.settings, self._on_settings_applied).pack(
            fill="x", pady=(0, 10))
        SmuLimitsEditor(side, self.settings, self._on_settings_applied).pack(fill="x")

        advanced = ttk.LabelFrame(side, text="Advanced")
        advanced.pack(fill="x", pady=(10, 0))
        ttk.Checkbutton(
            advanced,
            text="Tune frequency at operating point",
            variable=self.tune_frequency_var,
            command=self._on_tune_frequency_toggled,
        ).pack(anchor="w", padx=8, pady=(6, 0))
        ttk.Label(
            advanced,
            text=(
                "Runs after the bus reaches the target Vds peak, holding\n"
                "that peak at every frequency tried. Off means running at\n"
                "nominal, which historically differs from the tuned\n"
                "frequency by a median of 8.5% and up to 21.7%."
            ),
            justify="left",
        ).pack(anchor="w", padx=8, pady=(0, 6))
        ttk.Separator(advanced, orient="horizontal").pack(fill="x", padx=8)
        ttk.Checkbutton(
            advanced,
            text="Show voltage-only ZVS sweep",
            variable=self.show_zvs_sweep_var,
            command=self._on_show_zvs_sweep_toggled,
        ).pack(anchor="w", padx=8, pady=6)
        ttk.Label(
            advanced,
            text=(
                "The frequency search holds Vds peak on target.\n"
                "The ZVS sweep moves the bus off it, and is kept\n"
                "mainly to exercise the two independently."
            ),
            justify="left",
        ).pack(anchor="w", padx=8, pady=(0, 6))

        self.configuration_tab.grid_columnconfigure(0, weight=3)
        self.configuration_tab.grid_columnconfigure(1, weight=1)
        self.configuration_tab.grid_rowconfigure(1, weight=1)

    @staticmethod
    def _parse_configuration(value: str) -> str:
        return value.strip()

    @staticmethod
    def _parse_int(value: str) -> int:
        cleaned = value.replace(",", "").strip()
        if not cleaned:
            raise ValueError("empty")
        number = float(cleaned)
        if not math.isfinite(number) or not number.is_integer():
            raise ValueError("expected a finite whole number")
        return int(number)

    # ------------------------------------------------------------------
    # thread and operation coordination
    # ------------------------------------------------------------------

    def _start_worker(
        self, target: Callable[[], None], *, name: str
    ) -> threading.Thread:
        """Start a tracked, non-daemon worker."""
        return self._worker_pool.start(target, name=name)

    def _join_workers(self, timeout: float) -> bool:
        return self._worker_pool.join_all(timeout)

    def _begin_operation(
        self, kind: str, *, show_busy: bool = True
    ) -> Optional[OperationToken]:
        if self._closing:
            return None
        action = HARDWARE_OPERATION_LABELS.get(kind)
        if action is not None and not self._ensure_hardware_online(action):
            return None
        token = self.operations.try_begin(kind)
        if token is None and show_busy:
            active = self.operations.active_kind or "another operation"
            messagebox.showinfo(
                "Rig Busy",
                f"Wait for {active.replace('_', ' ')} to finish, or use EMERGENCY STOP.",
                parent=self,
            )
        self._refresh_control_states()
        return token

    def _finish_operation(self, token: OperationToken) -> None:
        self.operations.finish(token)
        self._refresh_control_states()
        self._refresh_confirm_state()

    def _safety_is_tripped(self) -> bool:
        return bool(getattr(self.safety, "is_tripped", False))

    def _control_state(self):
        return resolve_rig_control_state(
            active_kind=self.operations.active_kind,
            closing=self._closing,
            safety_tripped=self._safety_is_tripped(),
            hardware_offline=self.hardware_offline,
            engine_running=self.engine.state
            in (ExperimentState.RUNNING, ExperimentState.PAUSED),
            bus_energised=self._bus_is_energised(),
        )

    def _bus_is_energised(self) -> bool:
        """Whether the SMU output is on, from cached state rather than I/O."""
        smu = getattr(self, "smu", None)
        if smu is None:
            return False
        try:
            return bool(smu.output_is_on)
        except Exception:  # pragma: no cover - defensive
            # Unknown state is treated as energised: the conservative reading
            # denies a frequency move rather than permitting one.
            return True

    def _ensure_hardware_online(self, action: str) -> bool:
        """Block rig commands in offline mode; emergency stop remains available."""

        if not self.hardware_offline:
            return True
        if hasattr(self, "notebook") and hasattr(self, "configuration_tab"):
            self.notebook.select(self.configuration_tab)
        message = (
            f"Cannot {action} while the configured instruments are offline. "
            "Update the connections in Configuration, then restart the application."
        )
        if hasattr(self, "status_bar"):
            self.status_bar.set_message(message)
        messagebox.showwarning("Hardware Offline", message, parent=self)
        return False

    def _refresh_control_states(self) -> None:
        if not getattr(self, "_ui_ready", False):
            return
        controls = self._control_state()

        def state(widget, enabled: bool) -> None:
            if widget is not None:
                try:
                    widget.config(state="normal" if enabled else "disabled")
                except (tk.TclError, AttributeError):
                    pass

        state(getattr(self, "start_button", None), controls.hardware_actions)
        if hasattr(self, "zvs_button"):
            if controls.stop_zvs:
                self.zvs_button.config(state="normal", text="Stop ZVS")
            else:
                self.zvs_button.config(
                    state="normal" if controls.hardware_actions else "disabled",
                    text="Find ZVS Now",
                )
        state(getattr(self, "bus_off_button", None), controls.shutdown_actions)
        state(getattr(self, "confirm_button", None), controls.hardware_actions)
        device_bar = getattr(self, "device_bar", None)
        if device_bar is not None:
            device_bar.set_enabled(controls.edit_inputs)
        state(getattr(self, "duration_entry", None), controls.edit_inputs)
        state(getattr(self, "find_zvs_checkbox", None), controls.edit_inputs)
        state(
            getattr(self, "sim_validation_button", None),
            controls.hardware_actions,
        )
        state(getattr(self, "report_button", None), controls.local_actions)
        state(getattr(self, "export_button", None), controls.local_actions)
        for group in getattr(self, "param_groups", {}).values():
            group.set_enabled(controls.edit_inputs)
        if hasattr(self, "notebook") and hasattr(self, "configuration_tab"):
            self.notebook.tab(
                self.configuration_tab,
                state="normal" if controls.configuration else "disabled",
            )
        state(
            getattr(self, "reset_safety_button", None),
            controls.reset_safety,
        )
        planner_run = getattr(getattr(self, "planner_tab", None), "run_btn", None)
        planner_points = getattr(
            getattr(self, "planner_tab", None), "current_plan", None
        )
        state(
            planner_run,
            controls.hardware_actions
            and bool(getattr(planner_points, "points", ())),
        )

        if hasattr(self, "sequence_button"):
            if controls.stop_sequence:
                self.sequence_button.config(
                    state="normal",
                    text=(
                        "Stop Simulated Sequence"
                        if self.is_simulated
                        else "Stop Auto Sequence"
                    ),
                )
            else:
                self.sequence_button.config(
                    state="normal" if controls.hardware_actions else "disabled",
                    text=(
                        "Start Simulated Sequence"
                        if self.is_simulated
                        else "Start Auto Sequence"
                    ),
                )

        state(getattr(self, "pause_button", None), controls.pause_experiment)
        state(
            getattr(self, "cancel_button", None),
            controls.cancel_operation,
        )

        if hasattr(self, "autotune_button") and not controls.frequency_actions:
            self.autotune_button.config(state="disabled")

    def _reset_safety(self) -> None:
        if not self._ensure_hardware_online(
            HARDWARE_OPERATION_LABELS["reset_safety"]
        ):
            return
        if not self._safety_is_tripped():
            messagebox.showinfo(
                "Reset Safety", "No safety trip is currently latched.", parent=self
            )
            return
        reason = getattr(self.safety, "trip_reason", None)
        detail = f"\n\nLatched reason: {reason[0]} — {reason[1]}" if reason else ""
        if not messagebox.askyesno(
            "Reset Safety Interlock",
            "Confirm that the rig has been inspected and both outputs are OFF."
            f"{detail}\n\nReset the safety latch?",
            icon="warning",
            parent=self,
        ):
            return
        token = self._begin_operation("reset_safety")
        if token is None:
            return

        def worker() -> None:
            error: Optional[BaseException] = None
            try:
                self.safety.reset_trip()
            except BaseException as exc:
                error = exc
            self.ui_dispatcher.post(self._on_reset_safety_done, token, error)

        self._start_worker(worker, name="reset-safety")

    def _on_reset_safety_done(
        self, token: OperationToken, error: Optional[BaseException]
    ) -> None:
        self._finish_operation(token)
        if error is not None:
            messagebox.showerror(
                "Reset Safety",
                f"Safety reset failed: {error}",
                parent=self,
            )
            self.status_bar.set_message("Safety latch remains active.")
        else:
            self.status_bar.set_message("Safety latch reset. Rig remains disarmed.")
        self._update_smu_panel()
        self._refresh_control_states()

    # ------------------------------------------------------------------
    # parameter/device management
    # ------------------------------------------------------------------

    def _setup_traces(self) -> None:
        for var in (self.config_var, self.frequency_var, self.duty_var):
            var.trace_add("write", lambda *_: self._on_selection_changed())
        self.temperature_var.trace_add("write", lambda *_: self._refresh_confirm_state())
        self.voltage_var.trace_add("write", lambda *_: self._on_voltage_changed())

    def _on_selection_changed(self) -> None:
        if not self._ui_ready:
            return
        self.wavegen_controller.invalidate_tuning()
        self._refresh_confirm_state()
        self.update_telemetry()
        if hasattr(self, "up_next_view"):
            self.up_next_view.refresh()

    def _on_voltage_changed(self) -> None:
        if not self._ui_ready:
            return
        self.run_controls.set_duration(
            str(self.settings.default_duration_minutes(self.voltage_var.get()))
        )
        self._refresh_confirm_state()
        self.update_telemetry()

    def _on_device_committed(self, *_args) -> None:
        raw_name = self.device_name_var.get().strip()
        name = sanitize_device_name(raw_name)
        if not name or name != raw_name:
            messagebox.showerror(
                "Invalid Device Name",
                "Device names may contain only letters, numbers, spaces, '_' or '-'.",
                parent=self,
            )
            self.device_name_var.set(self._active_device)
            return
        if not name or name == self._active_device:
            return
        if self._active_device:
            self._persist_device_options(self._active_device)
        self._active_device = name
        self.db.get_or_create_device(name)
        self._load_device_options(name)
        self._refresh_device_dropdown()
        self.tracker.refresh()
        if hasattr(self, "up_next_view"):
            self.up_next_view.refresh()
        self.planner_tab.generate_plan(show_errors=False)
        self.analytics_tab.refresh()
        self.sheets.enqueue_device_setup(name)
        self.status_bar.set_message(f"Loaded configuration for {name}.")
        self._refresh_control_states()

    def _refresh_device_dropdown(self) -> None:
        self.device_bar.set_devices(self.db.list_devices())

    def _clear_device_context(self) -> None:
        """Clear every device-dependent control and view after the last deletion."""

        self._active_device = ""
        self.device_name_var.set("")
        for key in OPTION_KEYS:
            self.param_options[key] = []
        self._apply_options_to_ui()
        if hasattr(self, "plot"):
            self.plot.reset()
        if hasattr(self, "last_current_label"):
            self.last_current_label.config(text=last_current_text(None))
        if hasattr(self, "status_bar"):
            self.status_bar.set_message("No device selected.")
        self._refresh_confirm_state()

    def _load_device_options(self, device_name: str) -> None:
        stored = self.db.get_device_options(device_name) or {}
        for key in OPTION_KEYS:
            if key in stored:
                self.param_options[key] = self._normalize_options(key, stored.get(key))
            else:
                self.param_options[key] = list(self.default_param_options[key])
        self._apply_options_to_ui()

    def _normalize_options(self, key: str, raw: Optional[list]) -> List[Tuple[Any, str]]:
        """Clean one stored option set against the current safety ceiling."""
        return normalize_options(
            key, raw, max_vds_peak_v=self.settings.safety.max_vds_peak_v
        )

    def _apply_options_to_ui(self) -> None:
        if not self._ui_ready:
            return
        for key, group in self.param_groups.items():
            group.set_options(self.param_options[key])
        for key, editor in self.parameter_editors.items():
            editor.set_options(self.param_options[key])
        self.tracker.update_parameter_space(
            frequencies=self.param_options["frequencies"],
            duties=self.param_options["duties"],
            temperatures=self.param_options["temperatures"],
            voltages=self.param_options["voltages"],
            configs=self.param_options["configurations"],
        )
        self.planner_tab.update_parameter_options(
            frequencies=self.param_options["frequencies"],
            configurations=self.param_options["configurations"],
            duties=self.param_options["duties"],
            voltages=self.param_options["voltages"],
            temperatures=self.param_options["temperatures"],
        )
        self.analytics_tab.update_filter_options(
            frequencies=self.param_options["frequencies"],
            duties=self.param_options["duties"],
            temperatures=self.param_options["temperatures"],
            configurations=self.param_options["configurations"],
            voltages=self.param_options["voltages"],
        )
        if hasattr(self, "up_next_view"):
            self.up_next_view.refresh()
        self.sequence.all_configs = self._values("configurations")
        self._refresh_control_states()

    def _on_options_changed(
        self, key: str, options: List[Tuple[Any, str]]
    ) -> Optional[List[Tuple[Any, str]]]:
        device = self.device_name_var.get().strip()
        if not device:
            messagebox.showerror(
                "Device Required", "Select a device before editing configuration options."
            )
            return None
        normalized = self._normalize_options(key, [list(o) for o in options])
        if len(normalized) != len(options):
            messagebox.showerror(
                "Invalid Parameter",
                "The value is unsupported or outside the safe hardware range.",
                parent=self,
            )
            return None
        self.param_options[key] = normalized
        self._apply_options_to_ui()
        self._persist_device_options(device)
        return normalized

    def _persist_device_options(self, device_name: str) -> None:
        if sanitize_device_name(device_name) != device_name:
            return
        payload = {
            key: [[value, label] for value, label in options]
            for key, options in self.param_options.items()
        }
        self.db.set_device_options(device_name, payload)

    def _on_settings_applied(self) -> None:
        if self.is_simulated:
            self.status_bar.set_message(
                "Isolated simulation settings saved. Live settings were not changed."
            )
        else:
            self.status_bar.set_message(
                "Settings saved. Restart before the next hardware run."
            )

    def _restore_last_params(self) -> None:
        params = self.settings.last_params or {}
        device = sanitize_device_name(str(params.get("device_name", "")).strip())
        if device:
            self.device_name_var.set(device)
            self._active_device = device
            self._load_device_options(device)

        def assign(variable, key: str, options_key: str, cast):
            raw = params.get(key)
            if raw is None:
                return
            try:
                value = cast(raw)
            except (TypeError, ValueError):
                return
            if value in self._values(options_key):
                variable.set(value)

        assign(self.config_var, "config", "configurations", str)
        assign(self.frequency_var, "frequency", "frequencies", int)
        assign(self.duty_var, "duty", "duties", int)
        assign(self.temperature_var, "temperature", "temperatures", int)
        assign(self.voltage_var, "voltage", "voltages", int)
        if "duration" in params:
            self.run_controls.set_duration(str(params["duration"]))
        self.tracker.refresh()

    def _save_last_params(self) -> None:
        self.settings.last_params = {
            "device_name": self.device_name_var.get(),
            "config": self.config_var.get(),
            "frequency": self.frequency_var.get(),
            "duty": self.duty_var.get(),
            "temperature": self.temperature_var.get(),
            "voltage": self.voltage_var.get(),
            "duration": self.duration_entry.get(),
        }
        self.settings.find_zvs_before_run = bool(self.find_zvs_var.get())
        self.settings.tune_frequency_at_operating_point = bool(
            self.tune_frequency_var.get()
        )
        self.settings.show_zvs_voltage_sweep = bool(
            self.show_zvs_sweep_var.get()
        )
        self.settings.save()

    # ------------------------------------------------------------------
    # wavegen apply / autotune
    # ------------------------------------------------------------------

    def _read_selection(self) -> dict:
        """Current widget values, before any validation."""
        return {
            "device_name": self.device_name_var.get(),
            "config": self.config_var.get(),
            "frequency_hz": self.frequency_var.get(),
            "duty_pct": self.duty_var.get(),
            "temperature_c": self.temperature_var.get(),
            "voltage_v": self.voltage_var.get(),
        }

    def _current_point(self) -> Optional[MatrixPoint]:
        """The selected point, or ``None`` when the selection is unusable.

        Silent: used to refresh button states on every keystroke, where a
        half-typed field is normal rather than an error worth reporting.
        """
        try:
            return build_matrix_point(**self._read_selection())
        except (InputRejected, tk.TclError):
            return None

    def _tuning_candidate(self) -> Optional[Tuple[float, str, int]]:
        point = self._current_point()
        if point is None:
            return None
        return tuning_candidate(
            find_prior_tuned_frequency(
                self.db, point, self._values("configurations")
            ),
            self.wavegen_controller.tuned_freq_hz,
        )

    def _refresh_confirm_state(self) -> None:
        """Apply the resolved confirm/autotune presentation to the widgets."""
        if not self._ui_ready or self._closing:
            return
        try:
            pending = self.wavegen_controller.has_pending_changes(
                self.config_var.get(),
                int(self.frequency_var.get()),
                int(self.duty_var.get()),
            )
        except (ValueError, tk.TclError):
            # Unreadable inputs cannot be confirmed as applied, so treat the
            # wavegen as out of date rather than assume it matches.
            pending = True

        candidate = self._tuning_candidate()
        controls = self._control_state()
        presentation = resolve_confirm_presentation(
            wavegen_pending=pending,
            tuning_candidate_hz=candidate[0] if candidate else None,
            tuner_busy=self._tuner_busy,
            hardware_actions=controls.hardware_actions,
            frequency_actions=controls.frequency_actions,
        )

        self.confirm_button.set_style(**CONFIRM_STYLES[presentation.confirm_state])
        self.confirm_button.config(
            state="normal" if presentation.confirm_enabled else "disabled"
        )

        if not self._tuner_busy:
            if presentation.autotune_enabled:
                self.autotune_button.set_style(
                    bg=COLOR_OK,
                    fg="white",
                    active_bg="#388e3c",
                    text=presentation.autotune_text,
                    state="normal",
                )
            else:
                self.autotune_button.set_style(
                    bg="#bdbdbd",
                    fg="white",
                    text=presentation.autotune_text,
                    state="disabled",
                )

    def _confirm_high_risk(self) -> bool:
        warnings = []
        previous_duty = self.wavegen_controller.applied_duty
        new_duty = int(self.duty_var.get())
        if previous_duty is not None and new_duty != previous_duty:
            warnings.append(f"Duty cycle change from {previous_duty}% to {new_duty}%.")
        if not warnings:
            return True
        return messagebox.askyesno(
            "Confirm High-Risk Change",
            "The following high-risk changes were detected:\n\n"
            + "\n".join(f"- {w}" for w in warnings)
            + "\n\nThese changes may damage the device. Proceed?",
            icon="warning",
        )

    def _apply_wavegen(
        self, after_success: Optional[Callable[[], object]] = None
    ) -> bool:
        # ``after_success`` returns ``object`` so callbacks that report a value
        # (e.g. ``_launch_experiment``) can be passed without a wrapper; the
        # result is deliberately discarded.
        if not self._ensure_hardware_online(
            HARDWARE_OPERATION_LABELS["apply_wavegen"]
        ):
            return False
        if not self._confirm_high_risk():
            return False
        token = self._begin_operation("apply_wavegen")
        if token is None:
            return False
        config = self.config_var.get()
        frequency = int(self.frequency_var.get())
        duty = int(self.duty_var.get())
        self.status_bar.set_message("Applying wavegen settings...")

        def worker() -> None:
            error: Optional[BaseException] = None
            try:
                self.wavegen_controller.apply(
                    config,
                    frequency,
                    duty,
                    duty_rate_pct_s=self.settings.wavegen.duty_ramp_rate_pct_s,
                    freq_rate_khz_s=self.settings.wavegen.freq_ramp_rate_khz_s,
                    cancel_check=token.cancel_event.is_set,
                    status=self._status_async,
                )
                if token.cancel_event.is_set():
                    raise InterruptedError("wavegen configuration cancelled")
            except BaseException as exc:
                error = exc
            self.ui_dispatcher.post(
                self._on_apply_wavegen_done, token, error, after_success
            )

        self._start_worker(worker, name="apply-wavegen")
        return True

    def _on_apply_wavegen_done(
        self,
        token: OperationToken,
        error: Optional[BaseException],
        after_success: Optional[Callable[[], None]],
    ) -> None:
        self._finish_operation(token)
        if error is not None:
            if isinstance(error, InterruptedError):
                self.status_bar.set_message("Wavegen configuration cancelled.")
            else:
                messagebox.showerror(
                    "Wavegen Error",
                    f"Failed to configure wavegen: {error}",
                    parent=self,
                )
                self.status_bar.set_message("Wavegen configuration failed.")
            return
        self.status_bar.set_message("Wavegen parameters applied.")
        self._refresh_confirm_state()
        if after_success is not None and not self._closing:
            after_success()

    def _start_autotune(self) -> None:
        if not self._ensure_hardware_online(
            HARDWARE_OPERATION_LABELS["autotune"]
        ):
            return
        # Checked here as well as on the button: widget state is refreshed by
        # callbacks and can lag the rig, and this operation moves the gate
        # frequency with no closed-loop peak control behind it.
        if self._bus_is_energised():
            messagebox.showwarning(
                "Autotune",
                "The SMU bus is energised.\n\n"
                "Autotune ramps the gate frequency, which moves the resonant "
                "operating point and therefore Vds peak, with no closed-loop "
                "peak control.\n\n"
                "Switch the bus off first, or use 'Find frequency before run', "
                "which holds Vds peak on target throughout the search.",
                parent=self,
            )
            return
        candidate = self._tuning_candidate()
        if candidate is None:
            messagebox.showinfo("Autotune", "No tuned frequency is available for the current settings.")
            return
        if self.wavegen_controller.has_pending_changes(
            self.config_var.get(), int(self.frequency_var.get()), int(self.duty_var.get())
        ):
            if not messagebox.askyesno(
                "Autotune",
                "Wavegen settings have not been applied yet. Apply them first?",
                parent=self,
            ):
                return
            self._apply_wavegen(
                after_success=partial(self._launch_autotune, candidate)
            )
            return

        self._launch_autotune(candidate)

    def _launch_autotune(self, candidate: Tuple[float, str, int]) -> None:
        token = self._begin_operation("autotune")
        if token is None:
            return
        target, src_config, src_temp = candidate
        self._tuner_busy = True
        self.autotune_button.set_style(
            bg="#1976D2", fg="white", text="Autotuning...", state="disabled"
        )

        config = self.config_var.get()

        def worker() -> None:
            error: Optional[BaseException] = None
            try:
                self.wavegen_controller.ramp_to_frequency(
                    target,
                    config,
                    rate_khz_s=self.settings.zvs.autotune_freq_rate_khz_s,
                    cancel_check=token.cancel_event.is_set,
                    status=self._status_async,
                )
                actual = self.wavegen_controller.tuned_freq_hz
                if token.cancel_event.is_set():
                    raise InterruptedError("autotune cancelled")
                if actual is None or abs(actual - target) > 1.0:
                    raise RuntimeError(
                        "Autotune stopped before the target frequency was applied"
                    )
            except BaseException as exc:
                error = exc
            self.ui_dispatcher.post(
                self._on_autotune_done,
                token,
                target,
                src_config,
                src_temp,
                error,
            )

        self._start_worker(worker, name="autotune")

    def _on_autotune_done(
        self, token, target, src_config, src_temp, error
    ) -> None:
        self._tuner_busy = False
        self._finish_operation(token)
        if error is not None:
            if isinstance(error, FrequencyRampAborted):
                # Not a trip: the ramp stopped short deliberately, so the gate
                # is at an intermediate frequency and the rig is still live.
                messagebox.showwarning(
                    "Autotune Stopped",
                    f"{error}\n\n"
                    "The gate is left at the frequency reached, not the "
                    "target. Reduce the bus voltage before retrying.",
                    parent=self,
                )
                self.status_bar.set_message(
                    f"Autotune stopped at {error.frequency_hz / 1e6:.4f} MHz "
                    f"(Vds peak {error.peak_v:.0f} V)"
                )
            elif not isinstance(error, InterruptedError):
                messagebox.showerror(
                    "Autotune Error", f"Autotune failed: {error}", parent=self
                )
                self.status_bar.set_message("Autotune failed.")
            else:
                self.status_bar.set_message("Autotune cancelled.")
        else:
            show_temporary_popup(
                self,
                f"Tuned frequency {int(target)} Hz applied\n({src_config} @ {src_temp}°C)",
                duration_ms=2000,
            )
            self.status_bar.set_message(
                f"Tuned frequency {int(target)} Hz applied from {src_config} @ {src_temp}°C"
            )
        self._refresh_confirm_state()

    # ------------------------------------------------------------------
    # SMU panel: ZVS / bus off / E-STOP
    # ------------------------------------------------------------------

    def _update_smu_panel(self) -> None:
        self.smu_status_label.config(
            text=smu_status_line(
                setpoint_v=self.smu.setpoint_v,
                current_a=self.engine.last_current,
                output_on=bool(self.smu.output_is_on),
            )
        )
        self.update_telemetry()

    def _find_zvs_now(self) -> None:
        # Stopping comes first and reads nothing. An operator reaching for Stop
        # while the hardware has dropped offline, or after a trip, still needs
        # the search to stop — so it must not depend on any of the state that
        # decides whether one could be *started*.
        if self._request_zvs_stop():
            return
        try:
            target_v: Optional[float] = float(self.voltage_var.get())
        except (ValueError, tk.TclError):
            target_v = None
        try:
            wavegen_pending = self.wavegen_controller.has_pending_changes(
                self.config_var.get(),
                int(self.frequency_var.get()),
                int(self.duty_var.get()),
            )
        except (ValueError, tk.TclError):
            # Unreadable inputs cannot match what is on the wavegen, so treat
            # it as out of date rather than assume it agrees.
            wavegen_pending = True

        decision = zvs_precondition(
            hardware_offline=self.hardware_offline,
            safety_tripped=self._safety_is_tripped(),
            target_peak_v=target_v,
            max_vds_peak_v=float(self.settings.safety.max_vds_peak_v),
            wavegen_pending=wavegen_pending,
        )

        if decision.action is ZvsAction.HARDWARE_OFFLINE:
            self._ensure_hardware_online(HARDWARE_OPERATION_LABELS["zvs"])
        elif decision.action is ZvsAction.REFUSE:
            assert decision.refusal is not None
            self._show_refusal(decision.refusal)
        elif decision.action is ZvsAction.APPLY_WAVEGEN_FIRST:
            assert decision.prompt is not None
            if messagebox.askyesno(*decision.prompt, parent=self):
                self._apply_wavegen(after_success=self._launch_zvs)
        else:
            self._launch_zvs()

    def _request_zvs_stop(self) -> bool:
        """Request cooperative cancellation of the active manual ZVS search."""

        if self.operations.active_kind != "zvs":
            return False
        self.operations.cancel_active()
        if hasattr(self, "zvs_button"):
            self.zvs_button.config(text="Stopping...", state="disabled")
        if hasattr(self, "status_bar"):
            self.status_bar.set_message("Stopping ZVS search safely...")
        return True

    def _launch_zvs(self) -> None:
        token = self._begin_operation("zvs")
        if token is None:
            return
        target_v = int(self.voltage_var.get())
        config = self.config_var.get()
        self._tuner_busy = True
        self.status_bar.set_message("Preparing the HDO4054-verified ZVS search...")

        def worker() -> None:
            error = result = None
            try:
                if token.cancel_event.is_set():
                    raise InterruptedError("ZVS search cancelled")
                reason = self.safety.trip_reason
                if reason is not None:
                    raise SafetyTrip(*reason)

                self._status_async("Verifying LeCroy HDO4054 identity...")
                identity = self.engine.scope.verify_identity()
                log.info(
                    "Verified oscilloscope identity for manual ZVS: %s",
                    identity,
                )

                if token.cancel_event.is_set():
                    raise InterruptedError("ZVS search cancelled")
                reason = self.safety.trip_reason
                if reason is not None:
                    raise SafetyTrip(*reason)

                armed = self.safety.arm_wavegen(config)
                if not armed or not self.wavegen_controller.outputs_armed:
                    raise RuntimeError("Wavegen outputs did not confirm armed")

                if token.cancel_event.is_set():
                    raise InterruptedError("ZVS search cancelled")
                reason = self.safety.trip_reason
                if reason is not None:
                    raise SafetyTrip(*reason)

                if not self.safety.enable_bus():
                    raise ConnectionError("Could not enable the SMU output")
                self.engine.peak_controller.achieve_peak(
                    float(target_v),
                    cancel_check=token.cancel_event.is_set,
                    status=self._status_async,
                )
                result = self.engine.zvs_tuner.find_minimum(
                    cancel_check=token.cancel_event.is_set,
                    status=self._status_async,
                )
                if token.cancel_event.is_set():
                    raise InterruptedError("ZVS search cancelled")
            except BaseException as exc:
                error = exc
            finally:
                if error is None:
                    try:
                        self.smu.ramp_to(0.0)
                        if not self.smu.output_off():
                            raise RuntimeError(
                                "SMU output-off was not acknowledged"
                            )
                        self.wavegen_controller.disarm_outputs()
                        if (
                            self.smu.output_is_on
                            or self.wavegen_controller.outputs_armed
                        ):
                            raise RuntimeError(
                                "Output readback did not confirm the rig is safe"
                            )
                    except BaseException as cleanup_exc:
                        error = RuntimeError(
                            f"ZVS cleanup was not confirmed: {cleanup_exc}"
                        )
                        try:
                            # A failed output-off confirmation is a safety
                            # event, even if the emergency retry succeeds.
                            if not self.safety.emergency_stop():
                                error = RuntimeError(
                                    f"{error}; emergency output-off was unconfirmed"
                                )
                        except Exception:
                            log.exception(
                                "Emergency fallback failed after ZVS cleanup"
                            )
                else:
                    try:
                        confirmed = self.safety.shutdown_outputs()
                        if not confirmed:
                            if not self.safety.emergency_stop():
                                error = RuntimeError(
                                    f"{error}; emergency output-off was unconfirmed"
                                )
                    except Exception:
                        log.exception(
                            "Failed to make outputs safe after ZVS search error"
                        )
            self.ui_dispatcher.post(self._on_zvs_done, token, result, error)

        self._start_worker(worker, name="zvs")

    def _on_zvs_done(self, token, result, error) -> None:
        self._tuner_busy = False
        self._finish_operation(token)
        self._update_smu_panel()
        if error is not None:
            if not isinstance(error, InterruptedError):
                messagebox.showerror("Find ZVS", f"ZVS search failed: {error}")
                self.status_bar.set_message("ZVS search failed.")
            else:
                self.status_bar.set_message("ZVS search cancelled; outputs are OFF.")
        elif result is None:
            self.status_bar.set_message("ZVS search found no improvement.")
        else:
            self.status_bar.set_message(
                f"ZVS point: {result.v_zvs:.1f} V ({result.i_min * 1000:.2f} mA). "
                "Search complete; bus and gate outputs are OFF."
            )

    def _bus_off(self) -> None:
        if not self._ensure_hardware_online(HARDWARE_OPERATION_LABELS["bus_off"]):
            return
        token = self._begin_operation("bus_off")
        if token is None:
            return

        def worker() -> None:
            error: Optional[BaseException] = None
            try:
                self.smu.ramp_to(0.0, cancel_check=token.cancel_event.is_set)
                if not self.smu.output_off():
                    raise RuntimeError("SMU output-off was not acknowledged")
                self.wavegen_controller.disarm_outputs()
                if self.smu.output_is_on or self.wavegen_controller.outputs_armed:
                    raise RuntimeError(
                        "Output readback did not confirm the rig is safe"
                    )
            except BaseException as exc:
                error = exc
                try:
                    if not self.safety.shutdown_outputs():
                        if not self.safety.emergency_stop():
                            error = RuntimeError(
                                f"{error}; emergency output-off was unconfirmed"
                            )
                except Exception:
                    log.exception("Emergency fallback failed during Bus Off")
            self.ui_dispatcher.post(self._on_bus_off_done, token, error)

        self._start_worker(worker, name="bus-off")
        self.status_bar.set_message("Ramping bus to 0 V...")

    def _on_bus_off_done(
        self, token: OperationToken, error: Optional[BaseException]
    ) -> None:
        self._finish_operation(token)
        self._update_smu_panel()
        self.status_bar.set_message(
            "Bus and gate outputs are OFF."
            if error is None
            else f"Bus Off required the emergency shutdown path: {error}"
        )

    def _emergency_stop(self) -> None:
        if self._emergency_worker is not None and self._emergency_worker.is_alive():
            return

        self.status_bar.set_message("EMERGENCY STOP requested — shutting outputs down...")
        self._refresh_control_states()
        sequence_was_active = (
            self.operations.active_kind == "sequence" or self.sequence.active
        )

        def worker() -> None:
            error: Optional[BaseException] = None
            try:
                # These APIs latch the interlock before exposing cancellation
                # to the experiment worker.  Calling cancel() first can race a
                # real E-stop into an ordinary "cancelled" run outcome.
                shutdown_thread = (
                    self.sequence.request_emergency_stop()
                    if sequence_was_active
                    else self.engine.request_emergency_stop()
                )
                self.operations.cancel_active()
                shutdown_thread.join(timeout=10.0)
                problems = []
                if shutdown_thread.is_alive():
                    problems.append("emergency output shutdown did not finish")
                if not self.safety.is_tripped:
                    problems.append("emergency-stop safety latch was not confirmed")
                if not self.sequence.join(timeout=10.0):
                    problems.append("auto-sequence worker did not stop")
                self.engine.join(timeout=10.0)
                if self.engine.is_busy():
                    problems.append("experiment worker did not stop")
                if self.smu.output_is_on or self.wavegen_controller.outputs_armed:
                    problems.append("output shutdown could not be confirmed")
                if problems:
                    raise RuntimeError("; ".join(problems))
            except BaseException as exc:
                error = exc
                log.exception("Emergency-stop worker failed")
            self.ui_dispatcher.post(self._on_emergency_stop_done, error)

        self._emergency_worker = self._start_worker(
            worker, name="emergency-stop"
        )

    def _on_emergency_stop_done(
        self, error: Optional[BaseException]
    ) -> None:
        if error is None:
            self.status_bar.set_message(
                "EMERGENCY STOP complete. Outputs are OFF; safety is latched."
            )
        else:
            self.status_bar.set_message(
                f"EMERGENCY STOP encountered an error: {error}"
            )
        self._update_smu_panel()
        self._refresh_control_states()

    def _on_safety_trip(self, kind: str, detail: str) -> None:
        self.operations.cancel_active()

        def show() -> None:
            outputs_confirmed = (
                not self.smu.output_is_on
                and not self.wavegen_controller.outputs_armed
            )
            output_message = (
                "SMU output and wavegen gates are confirmed OFF."
                if outputs_confirmed
                else (
                    "OUTPUT SHUTDOWN WAS NOT CONFIRMED. Disconnect power and "
                    "inspect the rig before continuing."
                )
            )
            self._update_smu_panel()
            self.status_bar.set_message(
                f"SAFETY TRIP [{kind}]: {detail} — {output_message}"
            )
            self._refresh_control_states()
            messagebox.showerror(
                "SAFETY TRIP",
                f"The safety interlock triggered.\n\nReason [{kind}]:\n{detail}\n\n"
                f"{output_message}",
                parent=self,
            )
        self.ui_dispatcher.post(show)

    # ------------------------------------------------------------------
    # experiment control
    # ------------------------------------------------------------------

    def _validated_current_point(self) -> Optional[MatrixPoint]:
        """Validate shared matrix inputs without imposing a run duration.

        Reports why, unlike ``_current_point``: this runs when the operator has
        asked for something, so silence would leave them with a dead button and
        no explanation.
        """
        try:
            require_populated_options(
                {key: self._values(key) for key in OPTION_KEYS}, OPTION_KEYS
            )
            return build_matrix_point(**self._read_selection())
        except InputRejected as rejected:
            self._show_rejection(rejected)
            return None
        except tk.TclError:
            self._show_rejection(
                InputRejected("Input Error", "Invalid parameter selection.")
            )
            return None

    def _show_rejection(self, rejected: InputRejected) -> None:
        """Single place the validation reasons become dialogs."""
        messagebox.showerror(rejected.title, rejected.message, parent=self)

    def _build_params(self) -> Optional[ExperimentParams]:
        point = self._validated_current_point()
        if point is None:
            return None
        try:
            return build_experiment_params(
                point,
                self.duration_entry.get(),
                find_zvs=bool(self.find_zvs_var.get()),
                tune_frequency=bool(self.tune_frequency_var.get()),
            )
        except InputRejected as rejected:
            self._show_rejection(rejected)
            return None

    def _start_experiment(self) -> None:
        if not self._ensure_hardware_online(
            HARDWARE_OPERATION_LABELS["experiment"]
        ):
            return
        if self._safety_is_tripped():
            messagebox.showwarning(
                "Safety Interlock",
                "Reset the latched safety trip before starting an experiment.",
                parent=self,
            )
            return
        if self.operations.busy:
            self._begin_operation("experiment")
            return
        params = self._build_params()
        if params is None:
            return
        if self.wavegen_controller.has_pending_changes(
            params.point.config, params.point.frequency_hz, params.point.duty_pct
        ):
            if messagebox.askyesno(
                "Wavegen Settings",
                "The wavegen does not match the selected parameters.\n\nApply them now?",
                parent=self,
            ):
                self._apply_wavegen(
                    after_success=partial(self._launch_experiment, params)
                )
            else:
                return
            return
        self._launch_experiment(params)

    def _launch_experiment(self, params: ExperimentParams) -> bool:
        token = self._begin_operation("experiment")
        if token is None:
            return False
        self.plot.reset(params.point.describe())
        if not self.engine.start(params):
            self._finish_operation(token)
            messagebox.showinfo(
                "Rig Busy", "The experiment engine could not start.", parent=self
            )
            return False
        return True

    def _toggle_pause(self) -> None:
        self.engine.toggle_pause()

    def _cancel_experiment(self) -> None:
        if self._request_zvs_stop():
            return
        self.operations.cancel_active()
        if self.operations.active_kind == "sequence":
            self.sequence.cancel()
            self.sequence_button.config(text="Stopping...", state="disabled")
        else:
            self.engine.cancel()

    def _confirm_overwrite(self, description: str) -> bool:
        return bool(
            call_on_ui_thread(
                self,
                lambda: messagebox.askyesno(
                    "Overwrite Confirmation",
                    "A stored result already exists for this test point:\n\n"
                    f"{description}\n\nOverwrite it with new results?",
                    parent=self,
                ),
                cancel_event=(
                    self.operations.active.cancel_event
                    if self.operations.active is not None
                    else None
                ),
            )
        )

    def _on_sample(self, elapsed: float, amps: float) -> None:
        """Compatibility callback for older engines; not wired in this UI."""
        self.last_current_label.config(text=last_current_text(amps))
        self.plot.append(elapsed, amps)
        self._update_smu_panel()

    def _on_engine_state(self, state: ExperimentState) -> None:
        self.pause_button.config(
            text="Resume" if state == ExperimentState.PAUSED else "Pause"
        )
        self._refresh_control_states()

    def _on_run_finished(self, success: bool, message: str) -> None:
        if self._closing:
            return
        validation_run = bool(
            self.is_simulated
            and getattr(self, "_simulation_validation_active", False)
        )
        self._simulation_validation_active = False
        active = self.operations.active
        if active is not None and active.kind == "experiment":
            self._finish_operation(active)
        self._on_engine_state(ExperimentState.IDLE)
        self._update_smu_panel()
        self.tracker.refresh()
        if hasattr(self, "up_next_view"):
            self.up_next_view.refresh()
        self.planner_tab.generate_plan(show_errors=False)
        self.analytics_tab.refresh()
        status_message = message or (
            "Experiment complete!" if success else "Stopped."
        )
        if validation_run and success:
            outcome = self.engine.last_outcome
            sample_count = 0
            screenshot_path = None
            if outcome is not None and outcome.run_id is not None:
                sample_count = len(self.db.samples_for_run(outcome.run_id))
                if outcome.record is not None:
                    screenshot_path = outcome.record.screenshot_path
            status_message = (
                f"Simulation validation complete: {sample_count} samples saved "
                f"under {self.db.path.parent}."
            )
            self.notebook.select(self.analytics_tab)
            screenshot_note = (
                "A synthetic HDO4054 screen capture was saved and can be opened "
                "from the completed point's right-click menu."
                if screenshot_path
                else "No simulated scope capture was stored."
            )
            messagebox.showinfo(
                "Simulation Validation Complete",
                "The normal experiment procedure completed using virtual "
                f"instruments and stored {sample_count} samples.\n\n"
                f"{screenshot_note}\n\n"
                "Review results in Analytics, or return to Experiment and "
                "right-click the completed matrix point for details and plots.\n\n"
                f"Isolated data: {self.db.path.parent}",
                parent=self,
            )
        self.status_bar.set_message(status_message)
        self._refresh_confirm_state()
        self._refresh_control_states()

    def _on_run_completed_worker(self, record) -> None:
        # worker thread: hand results to the (optional) Sheets mirror
        self.sheets.enqueue_run(record)

    def _status_async(self, message: str) -> None:
        self.ui_dispatcher.post(self.status_bar.set_message, message)

    # ------------------------------------------------------------------
    # auto sequence
    # ------------------------------------------------------------------

    def _toggle_sequence(self) -> None:
        if self.operations.active_kind == "sequence" or self.sequence.active:
            self.operations.cancel_active()
            self.sequence.cancel()
            self.sequence_button.config(text="Stopping...", state="disabled")
            return
        if not self._ensure_hardware_online(
            HARDWARE_OPERATION_LABELS["sequence"]
        ):
            return
        if self._safety_is_tripped():
            messagebox.showwarning(
                "Safety Interlock",
                "Reset the latched safety trip before starting a sequence.",
                parent=self,
            )
            return
        if self.operations.busy:
            self._begin_operation("sequence")
            return
        params = self._build_params()
        if params is None:
            return
        plan = build_plan(
            self.db,
            params.point.device_name,
            params.point.frequency_hz,
            configs=self._values("configurations"),
            duties=self._values("duties"),
            voltages=self._values("voltages"),
            temperatures=self._values("temperatures"),
        )
        if not plan:
            messagebox.showwarning(
                "Auto Sequence",
                "All parameter combinations at this frequency are already complete.",
                parent=self,
            )
            return
        if not messagebox.askyesno(
            "Start Auto Sequence",
            f"This will run {len(plan)} tests across configurations, duties, voltages "
            "and temperatures.\n\nVoltage is set automatically by the SMU; you will "
            "only be prompted for chamber temperature changes.\n\nContinue?",
            parent=self,
        ):
            return
        token = self._begin_operation("sequence")
        if token is None:
            return
        if self.sequence.start(plan, params.duration_minutes, params.find_zvs):
            self.sequence_button.config(
                text=(
                    "Stop Simulated Sequence"
                    if self.is_simulated
                    else "Stop Auto Sequence"
                )
            )
            self.status_bar.set_message("Auto sequence starting...")
        else:
            self._finish_operation(token)
            messagebox.showerror(
                "Auto Sequence", "The sequence could not start.", parent=self
            )

    def _start_planned_sequence(
        self, plan_summary: Any, duration_minutes: float, find_zvs: bool
    ) -> None:
        if not self._ensure_hardware_online(
            HARDWARE_OPERATION_LABELS["sequence"]
        ):
            return
        if self._safety_is_tripped():
            messagebox.showwarning(
                "Safety Interlock",
                "Reset the latched safety trip before starting a sequence.",
                parent=self,
            )
            return
        if self.operations.busy or self.sequence.active:
            messagebox.showinfo(
                "Auto Sequence", "Another rig operation is already running.", parent=self
            )
            return
        plan = [p.point for p in plan_summary.points]
        if not plan:
            messagebox.showwarning(
                "Auto Sequence", "No test points in plan to execute.", parent=self
            )
            return
        if not messagebox.askyesno(
            "Start Planned Sequence",
            f"This will run {len(plan)} planned test point(s).\n\n"
            f"Estimated duration: {plan_summary.estimated_duration_minutes:.1f} minutes.\n\n"
            "Voltage is set automatically by the SMU; you will only be prompted for chamber temperature changes.\n\n"
            "Continue?",
            parent=self,
        ):
            return
        try:
            duration_minutes = parse_positive_duration(str(duration_minutes))
        except (TypeError, ValueError) as exc:
            messagebox.showerror(
                "Auto Sequence", f"Invalid test duration: {exc}", parent=self
            )
            return
        token = self._begin_operation("sequence")
        if token is None:
            return
        if self.sequence.start(plan, duration_minutes, find_zvs):
            self.sequence_button.config(
                text=(
                    "Stop Simulated Sequence"
                    if self.is_simulated
                    else "Stop Auto Sequence"
                )
            )
            self.status_bar.set_message("Planned auto sequence starting...")
            self.notebook.select(self.experiment_tab)
        else:
            self._finish_operation(token)
            messagebox.showerror(
                "Auto Sequence", "The planned sequence could not start.", parent=self
            )

    def _prompt_operator(self, title: str, message: str) -> bool:
        if self.is_simulated:
            title = f"SIMULATED OPERATOR STEP — {title}"
            message = (
                "No physical chamber change is required. Use this prompt to "
                "rehearse and verify the operator hand-off.\n\n"
                + message
            )
        return bool(
            call_on_ui_thread(
                self,
                lambda: messagebox.askokcancel(
                    title,
                    f"{message}\n\nClick OK when ready or Cancel to stop the sequence.",
                    parent=self,
                ),
                cancel_event=(
                    self.operations.active.cancel_event
                    if self.operations.active is not None
                    else None
                ),
            )
        )

    def _on_sequence_finished(self, _success: bool, message: str) -> None:
        def update() -> None:
            if self._closing:
                return
            active = self.operations.active
            if active is not None and active.kind == "sequence":
                self._finish_operation(active)
            self.status_bar.set_message(message)
            self.tracker.refresh()
            if hasattr(self, "up_next_view"):
                self.up_next_view.refresh()
            self.planner_tab.generate_plan(show_errors=False)
            self.analytics_tab.refresh()
            self._refresh_control_states()
        self.ui_dispatcher.post(update)

    # ------------------------------------------------------------------
    # reports / export
    # ------------------------------------------------------------------

    def _require_device(self) -> Optional[str]:
        raw_device = self.device_name_var.get().strip()
        device = sanitize_device_name(raw_device)
        if not device or device != raw_device:
            messagebox.showerror("Input Error", "Please enter a device name.")
            return None
        return device

    def _generate_report(self) -> None:
        device = self._require_device()
        if device is None:
            return
        token = self._begin_operation("report")
        if token is None:
            return
        dest = self.db.path.parent / "reports"

        def worker() -> None:
            path = None
            error: Optional[BaseException] = None
            try:
                path = reports_mod.generate_device_report(
                    self.db,
                    device,
                    dest,
                    is_simulated=self.is_simulated,
                )
            except BaseException as exc:
                error = exc
                log.exception("report generation failed")
            self.ui_dispatcher.post(
                self._on_report_done, token, path, error
            )

        self._start_worker(worker, name="generate-report")
        self.status_bar.set_message("Generating report...")

    def _on_report_done(self, token, path, error) -> None:
        self._finish_operation(token)
        if error is None:
            self.status_bar.set_message(f"Report written: {path}")
        else:
            messagebox.showerror(
                "Report", f"Report failed: {error}", parent=self
            )

    def _export_csv(self) -> None:
        device = self._require_device()
        if device is None:
            return
        token = self._begin_operation("export")
        if token is None:
            return
        dest = self.db.path.parent / "exports"
        try:
            paths = export_mod.export_device(
                self.db,
                device,
                dest,
                is_simulated=self.is_simulated,
            )
            self.status_bar.set_message("Exported: " + ", ".join(str(p) for p in paths))
        except Exception as exc:
            messagebox.showerror("Export", f"Export failed: {exc}", parent=self)
        finally:
            self._finish_operation(token)

    # ------------------------------------------------------------------

    def _on_closing(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.operations.begin_stopping()
        self._refresh_control_states()
        self.status_bar.set_message(
            "Closing safely: cancelling work and shutting outputs down..."
        )

        device = sanitize_device_name(self.device_name_var.get().strip())
        try:
            if device:
                self._persist_device_options(device)
            self._save_last_params()
        except Exception:
            log.exception("Could not persist UI state during shutdown")

        for unsubscribe in self._event_unsubscribers:
            try:
                unsubscribe()
            except Exception:
                pass
        self._event_unsubscribers.clear()

        self.sequence.cancel()
        self.engine.cancel()
        def shutdown_worker() -> None:
            attempt = 0
            while True:
                attempt += 1
                try:
                    outputs_safe = self.safety.shutdown_outputs()
                except Exception:
                    outputs_safe = False
                    log.exception("Output shutdown attempt failed during close")
                try:
                    sequence_stopped = self.sequence.cancel_and_join(
                        timeout=2.0
                    )
                except Exception:
                    sequence_stopped = False
                    log.exception("Sequence join attempt failed during close")
                try:
                    self.engine.cancel_and_join(timeout=2.0)
                    engine_stopped = not self.engine.is_busy()
                except Exception:
                    engine_stopped = False
                    log.exception("Engine join attempt failed during close")
                workers_stopped = self._join_workers(timeout=2.0)
                if (
                    outputs_safe
                    and sequence_stopped
                    and engine_stopped
                    and workers_stopped
                ):
                    break
                detail = (
                    "Waiting for safe shutdown "
                    f"(outputs={outputs_safe}, sequence={sequence_stopped}, "
                    f"engine={engine_stopped}, workers={workers_stopped})"
                )
                log.critical("%s; attempt %d", detail, attempt)
                self.ui_dispatcher.post(
                    self.status_bar.set_message, detail
                )
                time.sleep(0.25)

            try:
                self._close_resources()
                self._resources_closed = True
            except BaseException as exc:
                log.exception("Application resource shutdown failed")
                self.ui_dispatcher.post(self._finish_close, exc)
                return
            self.ui_dispatcher.post(self._finish_close, None)

        self._start_worker(shutdown_worker, name="app-shutdown")

    def _finish_close(self, error: Optional[BaseException]) -> None:
        if error is not None:
            log.error("Closing after resource cleanup error: %s", error)
        self.ui_dispatcher.close()
        self.destroy()
