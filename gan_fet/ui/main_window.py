"""Main application window.

Owns the window shell, the experiment/configuration tabs, the rig-activity
guard and the close path. Everything else is delegated:

* parameter option sets      -> core.device_profile / core.param_options
* wavegen apply + autotune   -> ui.wavegen_panel.WavegenControls
* manual SMU operations      -> ui.rig_panel.RigPanel
* configuration editors      -> ui.config_tab.ConfigurationTab

Engine and sequence callbacks arrive on worker threads and are marshalled
onto the Tk thread here (`self.after` for notifications, `call_on_ui_thread`
for prompts that need an answer).
"""

from __future__ import annotations

import logging
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Optional, Tuple

from gan_fet.core import param_options
from gan_fet.core.activity import RigActivity, RigOwner
from gan_fet.core.autotune import WavegenController, find_prior_tuned_frequency
from gan_fet.core.device_profile import DeviceProfileStore
from gan_fet.core.experiment import EngineCallbacks, ExperimentEngine
from gan_fet.core.models import (
    ExperimentParams,
    ExperimentState,
    MatrixPoint,
    sanitize_device_name,
)
from gan_fet.core.safety import SafetyMonitor
from gan_fet.core.sequence import AutoSequence, SequenceCallbacks, build_plan
from gan_fet.instruments.smu import Keithley2400
from gan_fet.settings import Settings
from gan_fet.sheets.sync import SheetsSync
from gan_fet.storage import export as export_mod
from gan_fet.storage import reports as reports_mod
from gan_fet.storage.db import Database
from gan_fet.ui.config_tab import ConfigurationTab
from gan_fet.ui.plot import LivePlot
from gan_fet.ui.rig_panel import RigPanel
from gan_fet.ui.tasks import run_in_background
from gan_fet.ui.tracker_view import TrackerView
from gan_fet.ui.wavegen_panel import WavegenControls
from gan_fet.ui.widgets import ParamButtonGroup, StatusBar, call_on_ui_thread

log = logging.getLogger(__name__)

DEFAULT_GEOMETRY = "1700x950"

PARAM_GROUP_LABELS = {
    "frequencies": "Frequency",
    "temperatures": "Temperature",
    "duties": "Duty Cycle",
    "configurations": "Configuration",
    "voltages": "Voltage (Vds pk)",
}
PARAM_GROUP_ORDER = ("frequencies", "temperatures", "duties", "configurations", "voltages")


class MainWindow(tk.Tk):
    def __init__(
        self,
        settings: Settings,
        db: Database,
        engine: ExperimentEngine,
        wavegen_controller: WavegenController,
        safety: SafetyMonitor,
        smu: Keithley2400,
        sheets: SheetsSync,
    ):
        super().__init__()
        self.title("GaN Device Test Runner")
        self.geometry(DEFAULT_GEOMETRY)
        self.protocol("WM_DELETE_WINDOW", self._on_closing)

        self.settings = settings
        self.db = db
        self.engine = engine
        self.wavegen_controller = wavegen_controller
        self.safety = safety
        self.smu = smu
        self.sheets = sheets

        self.activity = RigActivity()
        self._ui_ready = False
        self._closing = False

        defaults = param_options.defaults_from_values({
            "configurations": settings.default_configurations,
            "frequencies": settings.default_frequencies,
            "duties": settings.default_duties,
            "temperatures": settings.default_temperatures,
            "voltages": settings.default_voltages,
        })
        self.profile = DeviceProfileStore(db, defaults)

        self._init_variables()
        self._wire_engine()
        self.sequence = AutoSequence(
            db, engine, wavegen_controller,
            all_configs=self.profile.values("configurations"),
            callbacks=SequenceCallbacks(
                on_status=self._status_async,
                on_finished=self._on_sequence_finished,
                prompt_operator=self._prompt_operator,
            ),
        )
        self.safety.on_trip = self._on_safety_trip

        self._build_ui()
        self._ui_ready = True
        self._restore_last_params()
        self._setup_traces()
        self._refresh_controls()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    def _init_variables(self) -> None:
        self.device_name_var = tk.StringVar(value="")
        self.config_var = tk.StringVar(value=self.profile.first_value("configurations", ""))
        self.frequency_var = tk.IntVar(value=self.profile.first_value("frequencies", 0))
        self.duty_var = tk.IntVar(value=self.profile.first_value("duties", 0))
        self.temperature_var = tk.IntVar(value=self.profile.first_value("temperatures", 0))
        self.voltage_var = tk.IntVar(value=self.profile.first_value("voltages", 0))
        self.find_zvs_var = tk.BooleanVar(value=bool(self.settings.find_zvs_before_run))

    def _wire_engine(self) -> None:
        self.engine.callbacks = EngineCallbacks(
            on_sample=lambda elapsed, amps: self.after(0, self._on_sample, elapsed, amps),
            on_state=lambda state: self.after(0, self._on_engine_state, state),
            on_status=self._status_async,
            on_finished=lambda ok, msg: self.after(0, self._on_run_finished, ok, msg),
            confirm_overwrite=self._confirm_overwrite,
            report_error=lambda title, msg: self.after(
                0, lambda: messagebox.showerror(title, msg)
            ),
        )
        self.engine.on_run_completed = self.sheets.enqueue_run

    def _build_ui(self) -> None:
        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=0, column=0, sticky="nsew")
        self.experiment_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.experiment_tab, text="Experiment")

        self.main_frame = tk.Frame(self.experiment_tab)
        self.main_frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

        self._build_device_row()
        self._build_parameter_groups()
        self._build_run_controls()

        self.rig_panel = RigPanel(
            self.main_frame,
            smu=self.smu, engine=self.engine, safety=self.safety,
            activity=self.activity,
            target_voltage=lambda: int(self.voltage_var.get()),
            status=self._status_async,
        )
        self.rig_panel.grid(row=8, column=0, columnspan=3, sticky="ew", padx=5, pady=5)

        self._build_action_buttons()
        self._build_plot()
        self._build_tracker()

        self.configuration_tab = ConfigurationTab(
            self.notebook,
            settings=self.settings,
            options=self.profile.options,
            defaults=self.profile.defaults,
            on_options_changed=self._on_options_changed,
            on_settings_applied=lambda: self.status_bar.set_message(
                "Settings updated and saved."
            ),
        )
        self.notebook.add(self.configuration_tab, text="Configuration")

        self.status_bar = StatusBar(self)
        self.status_bar.grid(row=1, column=0, sticky="ew", pady=(5, 0))

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.experiment_tab.grid_rowconfigure(0, weight=1)
        self.experiment_tab.grid_rowconfigure(1, weight=1)
        self.experiment_tab.grid_columnconfigure(0, weight=1)
        self.main_frame.grid_columnconfigure(3, weight=1)
        self.main_frame.grid_rowconfigure(0, weight=1)

    def _build_device_row(self) -> None:
        tk.Label(self.main_frame, text="Device Name:").grid(
            row=0, column=0, sticky="e", padx=5, pady=5
        )
        self.device_dropdown = ttk.Combobox(
            self.main_frame, textvariable=self.device_name_var,
            values=self.profile.known_devices(), width=20, state="normal",
        )
        self.device_dropdown.grid(
            row=0, column=1, sticky="w", padx=5, pady=5, columnspan=2
        )
        for event in ("<<ComboboxSelected>>", "<FocusOut>", "<Return>"):
            self.device_dropdown.bind(event, self._on_device_committed)

    def _build_parameter_groups(self) -> None:
        self.param_groups: dict[str, ParamButtonGroup] = {}
        variables = {
            "frequencies": self.frequency_var,
            "temperatures": self.temperature_var,
            "duties": self.duty_var,
            "configurations": self.config_var,
            "voltages": self.voltage_var,
        }
        for row, key in enumerate(PARAM_GROUP_ORDER, start=1):
            group = ParamButtonGroup(
                self.main_frame, self.profile.options[key],
                variables[key], PARAM_GROUP_LABELS[key],
            )
            group.grid(row=row, column=0, columnspan=3, pady=8, sticky="w")
            self.param_groups[key] = group

    def _build_run_controls(self) -> None:
        row = 6
        ttk.Label(self.main_frame, text="Duration (min):").grid(
            row=row, column=0, sticky="e", padx=5, pady=5
        )
        self.duration_entry = ttk.Entry(self.main_frame, width=10)
        self.duration_entry.insert(
            0, str(self.settings.default_duration_minutes(self.voltage_var.get()))
        )
        self.duration_entry.grid(row=row, column=1, sticky="w")

        ttk.Checkbutton(
            self.main_frame, text="Find ZVS before run", variable=self.find_zvs_var,
        ).grid(row=row, column=2, sticky="w", padx=5)

        self.last_current_label = ttk.Label(self.main_frame, text="Last Current: N/A")
        self.last_current_label.grid(row=row + 1, column=0, sticky="w", padx=5, pady=5)

        self.wavegen_controls = WavegenControls(
            self.main_frame,
            controller=self.wavegen_controller,
            activity=self.activity,
            selection=self._wavegen_selection,
            tuning_candidate=self._tuning_candidate,
            status=self._status_async,
        )
        self.wavegen_controls.grid(row=row + 1, column=1, sticky="w", padx=5, pady=5)

    def _build_action_buttons(self) -> None:
        bar = tk.Frame(self.main_frame)
        bar.grid(row=9, column=0, columnspan=3, pady=10, sticky="w")

        self.start_button = ttk.Button(
            bar, text="Start Experiment", command=self._start_experiment
        )
        self.start_button.pack(side="left", padx=(0, 10))
        self.pause_button = ttk.Button(
            bar, text="Pause", command=self.engine.toggle_pause, state="disabled"
        )
        self.pause_button.pack(side="left", padx=(0, 10))
        self.cancel_button = ttk.Button(
            bar, text="Cancel", command=self._cancel_experiment, state="disabled"
        )
        self.cancel_button.pack(side="left", padx=(0, 10))
        self.sequence_button = ttk.Button(
            bar, text="Start Auto Sequence", command=self._toggle_sequence
        )
        self.sequence_button.pack(side="left", padx=(0, 10))
        ttk.Button(bar, text="Generate Report", command=self._generate_report).pack(
            side="left", padx=(0, 10)
        )
        ttk.Button(bar, text="Export CSV", command=self._export_csv).pack(side="left")

    def _build_plot(self) -> None:
        plot_frame = ttk.LabelFrame(self.main_frame, text="Real-Time Plot")
        plot_frame.grid(row=0, column=3, rowspan=10, padx=10, pady=10, sticky="nsew")
        self.plot = LivePlot(plot_frame)

    def _build_tracker(self) -> None:
        tracker_frame = tk.Frame(self.experiment_tab)
        tracker_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        tracker_frame.grid_rowconfigure(0, weight=1)
        tracker_frame.grid_columnconfigure(0, weight=1)
        self.tracker = TrackerView(
            tracker_frame, self.db,
            get_device_name=lambda: self.device_name_var.get(),
            frequencies=self.profile.options["frequencies"],
            duties=self.profile.options["duties"],
            temperatures=self.profile.options["temperatures"],
            voltages=self.profile.options["voltages"],
            configs=self.profile.options["configurations"],
            on_delete_run=self.sheets.enqueue_clear,
        )
        self.tracker.grid(row=0, column=0, sticky="nsew")

    # ------------------------------------------------------------------
    # selection state
    # ------------------------------------------------------------------

    def _setup_traces(self) -> None:
        for var in (self.config_var, self.frequency_var, self.duty_var):
            var.trace_add("write", lambda *_: self._on_selection_changed())
        self.temperature_var.trace_add("write", lambda *_: self._refresh_controls())
        self.voltage_var.trace_add("write", lambda *_: self._on_voltage_changed())

    def _on_selection_changed(self) -> None:
        if not self._ui_ready:
            return
        self.wavegen_controller.invalidate_tuning()
        self._refresh_controls()

    def _on_voltage_changed(self) -> None:
        if not self._ui_ready:
            return
        self.duration_entry.delete(0, tk.END)
        self.duration_entry.insert(
            0, str(self.settings.default_duration_minutes(self.voltage_var.get()))
        )
        self._refresh_controls()

    def _refresh_controls(self) -> None:
        if not self._ui_ready:
            return
        self.wavegen_controls.refresh()
        self.rig_panel.refresh()

    def _wavegen_selection(self) -> Tuple[str, int, int]:
        return (
            self.config_var.get(),
            int(self.frequency_var.get()),
            int(self.duty_var.get()),
        )

    def _current_point(self) -> Optional[MatrixPoint]:
        device = sanitize_device_name(self.device_name_var.get())
        if not device:
            return None
        config, frequency, duty = self._wavegen_selection()
        return MatrixPoint(
            device_name=device, config=config, frequency_hz=frequency,
            duty_pct=duty, temperature_c=int(self.temperature_var.get()),
            voltage_v=int(self.voltage_var.get()),
        )

    def _tuning_candidate(self):
        point = self._current_point()
        if point is None:
            return None
        prior = find_prior_tuned_frequency(
            self.db, point, self.profile.values("configurations")
        )
        if prior is None:
            return None
        applied = self.wavegen_controller.tuned_freq_hz
        if applied is not None and abs(applied - prior[0]) <= 1:
            return None  # already applied
        return prior

    # ------------------------------------------------------------------
    # device / parameter options
    # ------------------------------------------------------------------

    def _on_device_committed(self, *_args) -> None:
        name = self.device_name_var.get().strip()
        if not name or not self.profile.activate(name):
            return
        self._apply_options_to_ui()
        self.device_dropdown["values"] = self.profile.known_devices()
        self.tracker.refresh()
        self.sheets.enqueue_device_setup(name)
        self.status_bar.set_message(f"Loaded configuration for {name}.")

    def _apply_options_to_ui(self) -> None:
        if not self._ui_ready:
            return
        options = self.profile.options
        for key, group in self.param_groups.items():
            group.set_options(options[key])
        self.configuration_tab.set_options(options)
        self.tracker.update_parameter_space(
            frequencies=options["frequencies"],
            duties=options["duties"],
            temperatures=options["temperatures"],
            voltages=options["voltages"],
            configs=options["configurations"],
        )
        self.sequence.all_configs = self.profile.values("configurations")

    def _on_options_changed(self, key: str, options):
        if not self.profile.active_device:
            messagebox.showerror(
                "Device Required",
                "Select a device before editing configuration options.",
            )
            return None
        normalized = self.profile.set_options(key, options)
        if normalized is None:
            messagebox.showerror("Validation Error", "At least one option is required.")
            return None
        self._apply_options_to_ui()
        return normalized

    def _restore_last_params(self) -> None:
        params = self.settings.last_params or {}
        device = str(params.get("device_name", "")).strip()
        if device:
            self.device_name_var.set(device)
            self.profile.activate(device, persist_previous=False)
            self._apply_options_to_ui()

        def assign(variable, key: str, options_key: str, cast):
            raw = params.get(key)
            if raw is None:
                return
            try:
                value = cast(raw)
            except (TypeError, ValueError):
                return
            if value in self.profile.values(options_key):
                variable.set(value)

        assign(self.config_var, "config", "configurations", str)
        assign(self.frequency_var, "frequency", "frequencies", int)
        assign(self.duty_var, "duty", "duties", int)
        assign(self.temperature_var, "temperature", "temperatures", int)
        assign(self.voltage_var, "voltage", "voltages", int)
        if "duration" in params:
            self.duration_entry.delete(0, tk.END)
            self.duration_entry.insert(0, str(params["duration"]))
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
        self.settings.save()

    # ------------------------------------------------------------------
    # experiment control
    # ------------------------------------------------------------------

    def _build_params(self) -> Optional[ExperimentParams]:
        point = self._current_point()
        if point is None:
            messagebox.showerror("Input Error", "Please enter a device name.")
            return None
        try:
            duration = float(self.duration_entry.get())
        except ValueError:
            messagebox.showerror("Input Error", "Please enter a valid duration.")
            return None
        return ExperimentParams(
            point=point, duration_minutes=duration,
            find_zvs=bool(self.find_zvs_var.get()),
        )

    def _ensure_wavegen_applied(self, point: MatrixPoint) -> bool:
        if not self.wavegen_controller.has_pending_changes(
            point.config, point.frequency_hz, point.duty_pct
        ):
            return True
        if not messagebox.askyesno(
            "Wavegen Settings",
            "The wavegen does not match the selected parameters.\n\nApply them now?",
        ):
            return False
        return self.wavegen_controls.apply()

    def _start_experiment(self) -> None:
        params = self._build_params()
        if params is None:
            return
        if not self._ensure_wavegen_applied(params.point):
            return

        # Claim the rig for the whole run so a manual ZVS search or bus
        # ramp-down cannot drive the SMU underneath the engine.
        if not self.activity.try_acquire(RigOwner.EXPERIMENT):
            messagebox.showinfo(
                "Start Experiment",
                f"The rig is busy: {self.activity.owner.value} in progress.",
            )
            return

        self.plot.reset(params.point.describe())
        if not self.engine.start(params):
            self.activity.release(RigOwner.EXPERIMENT)
            messagebox.showinfo("Info", "Experiment already running.")

    def _cancel_experiment(self) -> None:
        self.activity.request_cancel()
        self.engine.cancel()

    def _confirm_overwrite(self, description: str) -> bool:
        return bool(
            call_on_ui_thread(
                self,
                lambda: messagebox.askyesno(
                    "Overwrite Confirmation",
                    "A stored result already exists for this test point:\n\n"
                    f"{description}\n\nOverwrite it with new results?",
                ),
                default=False,   # closing mid-prompt must not overwrite data
            )
        )

    def _on_sample(self, elapsed: float, amps: float) -> None:
        self.last_current_label.config(text=f"Last Current: {amps:.6f} A")
        self.plot.append(elapsed, amps)
        self.rig_panel.refresh()

    def _on_engine_state(self, state: ExperimentState) -> None:
        running = state in (ExperimentState.RUNNING, ExperimentState.PAUSED)
        self.start_button.config(state="disabled" if running else "normal")
        self.pause_button.config(
            state="normal" if running else "disabled",
            text="Resume" if state == ExperimentState.PAUSED else "Pause",
        )
        self.cancel_button.config(state="normal" if running else "disabled")

    def _on_run_finished(self, success: bool, message: str) -> None:
        # No-op when a sequence owns the rig — it releases at its own end.
        self.activity.release(RigOwner.EXPERIMENT)
        self._on_engine_state(ExperimentState.IDLE)
        self.tracker.refresh()
        self.status_bar.set_message(
            message or ("Experiment complete!" if success else "Stopped.")
        )
        self._refresh_controls()

    def _status_async(self, message: str) -> None:
        try:
            self.after(0, self.status_bar.set_message, message)
        except RuntimeError:
            pass  # window gone

    def _on_safety_trip(self, kind: str, detail: str) -> None:
        def show():
            self.rig_panel.refresh()
            self.status_bar.set_message(f"SAFETY TRIP [{kind}]: {detail}")
            messagebox.showerror(
                "SAFETY TRIP",
                f"The rig was shut down.\n\nReason [{kind}]:\n{detail}\n\n"
                "SMU output and wavegen gates are OFF.",
            )
        self.after(0, show)

    # ------------------------------------------------------------------
    # auto sequence
    # ------------------------------------------------------------------

    def _toggle_sequence(self) -> None:
        if self.sequence.active:
            self.activity.request_cancel()
            self.sequence.cancel()
            self.sequence_button.config(text="Stopping...", state="disabled")
            return

        params = self._build_params()
        if params is None:
            return
        plan = build_plan(
            self.db, params.point.device_name, params.point.frequency_hz,
            configs=self.profile.values("configurations"),
            duties=self.profile.values("duties"),
            voltages=self.profile.values("voltages"),
            temperatures=self.profile.values("temperatures"),
        )
        if not plan:
            messagebox.showwarning(
                "Auto Sequence",
                "All parameter combinations at this frequency are already complete.",
            )
            return
        if not messagebox.askyesno(
            "Start Auto Sequence",
            f"This will run {len(plan)} tests across configurations, duties, voltages "
            "and temperatures.\n\nVoltage is set automatically by the SMU; you will "
            "only be prompted for chamber temperature changes.\n\nContinue?",
        ):
            return

        if not self.activity.try_acquire(RigOwner.SEQUENCE):
            messagebox.showinfo(
                "Auto Sequence",
                f"The rig is busy: {self.activity.owner.value} in progress.",
            )
            return

        if self.sequence.start(plan, params.duration_minutes, params.find_zvs):
            self.sequence_button.config(text="Stop Auto Sequence")
            self.status_bar.set_message("Auto sequence starting...")
        else:
            self.activity.release(RigOwner.SEQUENCE)

    def _prompt_operator(self, title: str, message: str) -> bool:
        return bool(
            call_on_ui_thread(
                self,
                lambda: messagebox.askokcancel(
                    title,
                    f"{message}\n\nClick OK when ready or Cancel to stop the sequence.",
                ),
                default=False,   # closing mid-prompt stops the sequence
            )
        )

    def _on_sequence_finished(self, _success: bool, message: str) -> None:
        def update():
            self.activity.release(RigOwner.SEQUENCE)
            self.sequence_button.config(text="Start Auto Sequence", state="normal")
            self.status_bar.set_message(message)
            self.tracker.refresh()
        self.after(0, update)

    # ------------------------------------------------------------------
    # reports / export
    # ------------------------------------------------------------------

    def _require_device(self) -> Optional[str]:
        device = self.device_name_var.get().strip()
        if not device:
            messagebox.showerror("Input Error", "Please enter a device name.")
            return None
        return device

    def _generate_report(self) -> None:
        device = self._require_device()
        if device is None:
            return
        dest = self.settings.db_path.parent / "reports"

        def done(path, error: Optional[BaseException]) -> None:
            if error is not None:
                messagebox.showerror("Report", f"Report failed: {error}")
            else:
                self.status_bar.set_message(f"Report written: {path}")

        run_in_background(
            self,
            lambda: reports_mod.generate_device_report(self.db, device, dest),
            done,
            name="report",
        )
        self.status_bar.set_message("Generating report...")

    def _export_csv(self) -> None:
        device = self._require_device()
        if device is None:
            return
        dest = self.settings.db_path.parent / "exports"
        try:
            paths = export_mod.export_device(self.db, device, dest)
            self.status_bar.set_message(
                "Exported: " + ", ".join(str(p) for p in paths)
            )
        except Exception as exc:
            messagebox.showerror("Export", f"Export failed: {exc}")

    # ------------------------------------------------------------------
    # shutdown
    # ------------------------------------------------------------------

    def _on_closing(self) -> None:
        if self._closing:
            return
        self._closing = True

        self.profile.persist()
        self._save_last_params()

        was_busy = self.engine.is_busy() or self.sequence.active
        self.activity.request_cancel()
        self.sequence.cancel()
        self.engine.cancel()

        # Hardware shutdown runs off the UI thread: with an unreachable
        # instrument every SCPI write blocks on a connect timeout, which
        # must never freeze the close.
        worker = run_in_background(
            self,
            lambda: self.smu.emergency_off() if self.smu.output_is_on else None,
            name="shutdown",
        )

        deadline = time.monotonic() + (10.0 if was_busy else 1.0)
        self.status_bar.set_message("Shutting down...")

        def poll_shutdown():
            finished = not self.engine.is_busy() and not worker.is_alive()
            if finished or time.monotonic() > deadline:
                self.destroy()
            else:
                self.after(50, poll_shutdown)

        poll_shutdown()
