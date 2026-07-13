"""Main application window.

All engine/sequence callbacks arrive on worker threads and are marshalled
onto the Tk main thread here (`self.after` for fire-and-forget,
`call_on_ui_thread` for blocking prompts).
"""

from __future__ import annotations

import logging
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Dict, List, Optional, Tuple

from gan_fet.core.autotune import WavegenController, find_prior_tuned_frequency
from gan_fet.core.experiment import EngineCallbacks, ExperimentEngine
from gan_fet.core.models import (
    ExperimentParams,
    ExperimentState,
    MatrixPoint,
    freq_label,
    sanitize_device_name,
)
from gan_fet.core.safety import SafetyMonitor
from gan_fet.core.sequence import AutoSequence, SequenceCallbacks, build_plan
from gan_fet.core.voltage_control import PeakControlError
from gan_fet.instruments.smu import Keithley2400
from gan_fet.settings import Settings
from gan_fet.sheets.sync import SheetsSync
from gan_fet.storage import export as export_mod
from gan_fet.storage import reports as reports_mod
from gan_fet.storage.db import Database
from gan_fet.ui.config_tab import (
    InstrumentConfigEditor,
    ParameterListEditor,
    SmuLimitsEditor,
)
from gan_fet.ui.plot import LivePlot
from gan_fet.ui.tracker_view import TrackerView
from gan_fet.ui.widgets import (
    ColorButton,
    ParamButtonGroup,
    StatusBar,
    call_on_ui_thread,
    show_temporary_popup,
)

log = logging.getLogger(__name__)

DEFAULT_GEOMETRY = "1700x950"

OPTION_KEYS = ("configurations", "frequencies", "duties", "temperatures", "voltages")

# Confirm-button state colours (unchanged from v1)
COLOR_PENDING = "#e53935"    # red — selection differs from what's on the wavegen
COLOR_TUNING = "#FBC02D"     # yellow — a better (tuned) frequency is available
COLOR_OK = "#43a047"         # green — instrument matches the selection


def _default_label(key: str, value: Any) -> str:
    if key == "frequencies":
        return freq_label(value)
    if key == "duties":
        return f"{value}%"
    if key == "temperatures":
        return f"{value}°C"
    if key == "voltages":
        return f"{value}V"
    return str(value)


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

        self._ui_ready = False
        self._tuner_busy = False
        self._active_device = ""

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
        self._ui_ready = True
        self._restore_last_params()
        self._setup_traces()
        self._refresh_confirm_state()
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

    def _first(self, key: str) -> Any:
        options = self.param_options.get(key) or []
        return options[0][0] if options else ""

    def _values(self, key: str) -> List[Any]:
        return [v for v, _ in self.param_options.get(key, [])]

    def _wire_engine(self) -> None:
        self.engine.callbacks = EngineCallbacks(
            on_sample=lambda elapsed, amps: self.after(
                0, self._on_sample, elapsed, amps
            ),
            on_state=lambda state: self.after(0, self._on_engine_state, state),
            on_status=self._status_async,
            on_finished=lambda ok, msg: self.after(0, self._on_run_finished, ok, msg),
            confirm_overwrite=self._confirm_overwrite,
            report_error=lambda title, msg: self.after(
                0, lambda: messagebox.showerror(title, msg)
            ),
        )
        self.engine.on_run_completed = self._on_run_completed_worker

    def _build_ui(self) -> None:
        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=0, column=0, sticky="nsew")
        self.experiment_tab = ttk.Frame(self.notebook)
        self.configuration_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.experiment_tab, text="Experiment")
        self.notebook.add(self.configuration_tab, text="Configuration")

        self.main_frame = tk.Frame(self.experiment_tab)
        self.main_frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

        self._build_device_row()
        self._build_parameter_groups()
        self._build_run_controls()
        self._build_smu_panel()
        self._build_action_buttons()
        self._build_plot()
        self._build_tracker()
        self._build_configuration_tab()

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
            self.main_frame,
            textvariable=self.device_name_var,
            values=self.db.list_devices(),
            width=20,
            state="normal",
        )
        self.device_dropdown.grid(row=0, column=1, sticky="w", padx=5, pady=5, columnspan=2)
        for event in ("<<ComboboxSelected>>", "<FocusOut>", "<Return>"):
            self.device_dropdown.bind(event, self._on_device_committed)

    def _build_parameter_groups(self) -> None:
        specs = [
            ("frequencies", self.frequency_var, "Frequency"),
            ("temperatures", self.temperature_var, "Temperature"),
            ("duties", self.duty_var, "Duty Cycle"),
            ("configurations", self.config_var, "Configuration"),
            ("voltages", self.voltage_var, "Voltage (Vds pk)"),
        ]
        self.param_groups: Dict[str, ParamButtonGroup] = {}
        for row, (key, variable, label) in enumerate(specs, start=1):
            group = ParamButtonGroup(
                self.main_frame, self.param_options[key], variable, label
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
            self.main_frame,
            text="Find ZVS before run",
            variable=self.find_zvs_var,
        ).grid(row=row, column=2, sticky="w", padx=5)

        self.last_current_label = ttk.Label(self.main_frame, text="Last Current: N/A")
        self.last_current_label.grid(row=row + 1, column=0, sticky="w", padx=5, pady=5)

        self.confirm_button = ColorButton(
            self.main_frame,
            text="Apply Wavegen Settings",
            command=self._apply_wavegen,
            width=170, height=36, borderless=1, highlightthickness=1,
        )
        self.confirm_button.grid(row=row + 1, column=1, sticky="w", padx=5, pady=5)

        self.autotune_button = ColorButton(
            self.main_frame,
            text="Autotune Unavailable",
            command=self._start_autotune,
            width=170, height=36, borderless=1, highlightthickness=1,
        )
        self.autotune_button.grid(row=row + 1, column=2, sticky="w", padx=5, pady=5)

    def _build_smu_panel(self) -> None:
        frame = ttk.LabelFrame(self.main_frame, text="SMU (Keithley 2400-series)")
        frame.grid(row=8, column=0, columnspan=3, sticky="ew", padx=5, pady=5)
        self.smu_status_label = ttk.Label(frame, text="Bus: — | I: — | Output: OFF")
        self.smu_status_label.grid(row=0, column=0, sticky="w", padx=8, pady=4)

        self.zvs_button = ttk.Button(frame, text="Find ZVS Now", command=self._find_zvs_now)
        self.zvs_button.grid(row=0, column=1, padx=6, pady=4)

        self.bus_off_button = ttk.Button(frame, text="Bus Off", command=self._bus_off)
        self.bus_off_button.grid(row=0, column=2, padx=6, pady=4)

        self.estop_button = ColorButton(
            frame,
            text="EMERGENCY STOP",
            command=self._emergency_stop,
            width=170, height=36, borderless=1, highlightthickness=1,
        )
        self.estop_button.set_style(bg="#b71c1c", fg="white", active_bg="#7f0000",
                                    active_fg="white")
        self.estop_button.grid(row=0, column=3, padx=10, pady=4)
        frame.grid_columnconfigure(0, weight=1)

    def _build_action_buttons(self) -> None:
        bar = tk.Frame(self.main_frame)
        bar.grid(row=9, column=0, columnspan=3, pady=10, sticky="w")

        self.start_button = ttk.Button(bar, text="Start Experiment", command=self._start_experiment)
        self.start_button.pack(side="left", padx=(0, 10))
        self.pause_button = ttk.Button(bar, text="Pause", command=self._toggle_pause,
                                       state="disabled")
        self.pause_button.pack(side="left", padx=(0, 10))
        self.cancel_button = ttk.Button(bar, text="Cancel", command=self._cancel_experiment,
                                        state="disabled")
        self.cancel_button.pack(side="left", padx=(0, 10))
        self.sequence_button = ttk.Button(bar, text="Start Auto Sequence",
                                          command=self._toggle_sequence)
        self.sequence_button.pack(side="left", padx=(0, 10))
        ttk.Button(bar, text="Generate Report", command=self._generate_report).pack(
            side="left", padx=(0, 10))
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
            tracker_frame,
            self.db,
            get_device_name=lambda: self.device_name_var.get(),
            frequencies=self.param_options["frequencies"],
            duties=self.param_options["duties"],
            temperatures=self.param_options["temperatures"],
            voltages=self.param_options["voltages"],
            configs=self.param_options["configurations"],
            on_delete_run=self.sheets.enqueue_clear,
        )
        self.tracker.grid(row=0, column=0, sticky="nsew")

    def _build_configuration_tab(self) -> None:
        ttk.Label(
            self.configuration_tab,
            text=(
                "Per-device parameter options (stored in the database) and global "
                "instrument/safety settings (stored in settings.json)."
            ),
            wraplength=800,
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(10, 5))

        editors_frame = ttk.Frame(self.configuration_tab)
        editors_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        for col in range(2):
            editors_frame.grid_columnconfigure(col, weight=1, uniform="cfg")

        editor_specs = [
            ("Configurations", "configurations", str.strip, lambda v: v),
            ("Frequencies (Hz)", "frequencies", self._parse_int, lambda v: freq_label(v)),
            ("Duty Cycles (%)", "duties", self._parse_int, lambda v: f"{v}%"),
            ("Temperatures (°C)", "temperatures", self._parse_int, lambda v: f"{v}°C"),
            ("Voltages (V)", "voltages", self._parse_int, lambda v: f"{v}V"),
        ]
        for idx, (title, key, parser, label_factory) in enumerate(editor_specs):
            editor = ParameterListEditor(
                editors_frame,
                title,
                self.param_options[key],
                on_change=lambda opts, k=key: self._on_options_changed(k, opts),
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
        SmuLimitsEditor(side, self.settings, self._on_settings_applied).pack(fill="x")

        self.configuration_tab.grid_columnconfigure(0, weight=3)
        self.configuration_tab.grid_columnconfigure(1, weight=1)
        self.configuration_tab.grid_rowconfigure(1, weight=1)

    @staticmethod
    def _parse_int(value: str) -> int:
        cleaned = value.replace(",", "").strip()
        if not cleaned:
            raise ValueError("empty")
        if any(ch in cleaned.lower() for ch in ("e", ".")):
            return int(float(cleaned))
        return int(cleaned, 10)

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

    def _on_voltage_changed(self) -> None:
        if not self._ui_ready:
            return
        self.duration_entry.delete(0, tk.END)
        self.duration_entry.insert(
            0, str(self.settings.default_duration_minutes(self.voltage_var.get()))
        )
        self._refresh_confirm_state()

    def _on_device_committed(self, *_args) -> None:
        name = self.device_name_var.get().strip()
        if not name or name == self._active_device:
            return
        if self._active_device:
            self._persist_device_options(self._active_device)
        self._active_device = name
        self.db.get_or_create_device(name)
        self._load_device_options(name)
        self._refresh_device_dropdown()
        self.tracker.refresh()
        self.sheets.enqueue_device_setup(name)
        self.status_bar.set_message(f"Loaded configuration for {name}.")

    def _refresh_device_dropdown(self) -> None:
        self.device_dropdown["values"] = self.db.list_devices()

    def _load_device_options(self, device_name: str) -> None:
        stored = self.db.get_device_options(device_name) or {}
        for key in OPTION_KEYS:
            normalized = self._normalize_options(key, stored.get(key))
            self.param_options[key] = normalized or list(self.default_param_options[key])
        self._apply_options_to_ui()

    def _normalize_options(self, key: str, raw: Optional[list]) -> List[Tuple[Any, str]]:
        if not raw:
            return []
        cleaned: List[Tuple[Any, str]] = []
        seen = set()
        for entry in raw:
            if isinstance(entry, dict):
                value, label = entry.get("value"), str(entry.get("label", ""))
            elif isinstance(entry, (list, tuple)) and entry:
                value = entry[0]
                label = str(entry[1]) if len(entry) > 1 else ""
            else:
                value, label = entry, ""
            if key == "configurations":
                value = str(value).strip()
                if not value or value in seen:
                    continue
            else:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
                if value in seen:
                    continue
            seen.add(value)
            cleaned.append((value, label.strip() or _default_label(key, value)))
        cleaned.sort(key=(lambda e: e[1].lower()) if key == "configurations" else (lambda e: e[0]))
        return cleaned

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
        self.sequence.all_configs = self._values("configurations")

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
        if not normalized:
            messagebox.showerror("Validation Error", "At least one option is required.")
            return None
        self.param_options[key] = normalized
        self._apply_options_to_ui()
        self._persist_device_options(device)
        return normalized

    def _persist_device_options(self, device_name: str) -> None:
        if not sanitize_device_name(device_name):
            return
        payload = {
            key: [[value, label] for value, label in options]
            for key, options in self.param_options.items()
        }
        self.db.set_device_options(device_name, payload)

    def _on_settings_applied(self) -> None:
        self.status_bar.set_message("Settings updated and saved.")

    def _restore_last_params(self) -> None:
        params = self.settings.last_params or {}
        device = str(params.get("device_name", "")).strip()
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
    # wavegen apply / autotune
    # ------------------------------------------------------------------

    def _current_point(self) -> Optional[MatrixPoint]:
        device = self.device_name_var.get().strip()
        if not device:
            return None
        return MatrixPoint(
            device_name=sanitize_device_name(device),
            config=self.config_var.get(),
            frequency_hz=int(self.frequency_var.get()),
            duty_pct=int(self.duty_var.get()),
            temperature_c=int(self.temperature_var.get()),
            voltage_v=int(self.voltage_var.get()),
        )

    def _tuning_candidate(self) -> Optional[Tuple[float, str, int]]:
        point = self._current_point()
        if point is None:
            return None
        prior = find_prior_tuned_frequency(self.db, point, self._values("configurations"))
        if prior is None:
            return None
        applied = self.wavegen_controller.tuned_freq_hz
        if applied is not None and abs(applied - prior[0]) <= 1:
            return None  # already there
        return prior

    def _refresh_confirm_state(self) -> None:
        if not self._ui_ready:
            return
        pending = self.wavegen_controller.has_pending_changes(
            self.config_var.get(), int(self.frequency_var.get()), int(self.duty_var.get())
        )
        candidate = self._tuning_candidate()

        if pending:
            self.confirm_button.set_style(bg=COLOR_PENDING, fg="white")
        elif candidate:
            self.confirm_button.set_style(bg=COLOR_TUNING, fg="black")
        else:
            self.confirm_button.set_style(bg=COLOR_OK, fg="white")

        if candidate and not self._tuner_busy:
            freq_mhz = candidate[0] / 1e6
            self.autotune_button.set_style(
                bg=COLOR_OK, fg="white", active_bg="#388e3c",
                text=f"Autotune: {freq_mhz:.2f} MHz", state="normal",
            )
        elif not self._tuner_busy:
            self.autotune_button.set_style(
                bg="#bdbdbd", fg="white",
                text="Autotune Unavailable", state="disabled",
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

    def _apply_wavegen(self) -> None:
        if not self._confirm_high_risk():
            return
        try:
            self.wavegen_controller.apply(
                self.config_var.get(),
                int(self.frequency_var.get()),
                int(self.duty_var.get()),
            )
        except Exception as exc:
            messagebox.showerror("Wavegen Error", f"Failed to configure wavegen: {exc}")
            return
        self.status_bar.set_message("Wavegen parameters applied.")
        self._refresh_confirm_state()

    def _start_autotune(self) -> None:
        if self._tuner_busy:
            return
        candidate = self._tuning_candidate()
        if candidate is None:
            messagebox.showinfo("Autotune", "No tuned frequency is available for the current settings.")
            return
        if self.wavegen_controller.has_pending_changes(
            self.config_var.get(), int(self.frequency_var.get()), int(self.duty_var.get())
        ):
            if not messagebox.askyesno(
                "Autotune", "Wavegen settings have not been applied yet. Apply them first?"
            ):
                return
            self._apply_wavegen()

        target, src_config, src_temp = candidate
        self._tuner_busy = True
        self.autotune_button.set_style(
            bg="#1976D2", fg="white", text="Autotuning...", state="disabled"
        )

        config = self.config_var.get()

        def worker():
            try:
                self.wavegen_controller.ramp_to_frequency(
                    target, config, status=self._status_async
                )
                self.after(0, self._on_autotune_done, target, src_config, src_temp, None)
            except Exception as exc:
                self.after(0, self._on_autotune_done, target, src_config, src_temp, exc)

        threading.Thread(target=worker, daemon=True, name="autotune").start()

    def _on_autotune_done(self, target, src_config, src_temp, error) -> None:
        self._tuner_busy = False
        if error is not None:
            messagebox.showerror("Autotune Error", f"Autotune failed: {error}")
            self.status_bar.set_message("Autotune failed.")
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
        state = "ON" if self.smu.output_is_on else "OFF"
        current = self.engine.last_current
        current_text = f"{current * 1000:.2f} mA" if current is not None else "—"
        self.smu_status_label.config(
            text=f"Bus setpoint: {self.smu.setpoint_v:.1f} V | I: {current_text} | Output: {state}"
        )

    def _find_zvs_now(self) -> None:
        if self.engine.is_busy() or self._tuner_busy:
            messagebox.showinfo("Find ZVS", "Wait for the current operation to finish.")
            return
        target_v = int(self.voltage_var.get())
        self._tuner_busy = True
        self.zvs_button.config(state="disabled")
        self.status_bar.set_message("Bringing the bus up for ZVS search...")

        def worker():
            error = result = None
            try:
                if not self.smu.output_on():
                    raise ConnectionError("Could not enable the SMU output")
                self.engine.peak_controller.achieve_peak(
                    float(target_v), status=self._status_async
                )
                result = self.engine.zvs_tuner.find_minimum(status=self._status_async)
            except Exception as exc:
                error = exc
            self.after(0, self._on_zvs_done, result, error)

        threading.Thread(target=worker, daemon=True, name="zvs").start()

    def _on_zvs_done(self, result, error) -> None:
        self._tuner_busy = False
        self.zvs_button.config(state="normal")
        self._update_smu_panel()
        if error is not None:
            if not isinstance(error, PeakControlError) or str(error) != "cancelled":
                messagebox.showerror("Find ZVS", f"ZVS search failed: {error}")
            self.status_bar.set_message("ZVS search failed.")
        elif result is None:
            self.status_bar.set_message("ZVS search found no improvement.")
        else:
            self.status_bar.set_message(
                f"ZVS point: {result.v_zvs:.1f} V ({result.i_min * 1000:.2f} mA). "
                "Bus is live at the ZVS point."
            )

    def _bus_off(self) -> None:
        if self.engine.is_busy():
            messagebox.showinfo("Bus Off", "An experiment is running — use Cancel or EMERGENCY STOP.")
            return

        def worker():
            try:
                self.smu.ramp_to(0.0)
                self.smu.output_off()
            except Exception:
                self.smu.emergency_off()
            self.after(0, self._update_smu_panel)

        threading.Thread(target=worker, daemon=True).start()
        self.status_bar.set_message("Ramping bus to 0 V...")

    def _emergency_stop(self) -> None:
        self.engine.cancel()
        self.sequence.cancel()
        threading.Thread(target=self.safety.emergency_stop, daemon=True).start()

    def _on_safety_trip(self, kind: str, detail: str) -> None:
        def show():
            self._update_smu_panel()
            self.status_bar.set_message(f"SAFETY TRIP [{kind}]: {detail}")
            messagebox.showerror(
                "SAFETY TRIP",
                f"The rig was shut down.\n\nReason [{kind}]:\n{detail}\n\n"
                "SMU output and wavegen gates are OFF.",
            )
        self.after(0, show)

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
            point=point,
            duration_minutes=duration,
            find_zvs=bool(self.find_zvs_var.get()),
        )

    def _start_experiment(self) -> None:
        params = self._build_params()
        if params is None:
            return
        if self.wavegen_controller.has_pending_changes(
            params.point.config, params.point.frequency_hz, params.point.duty_pct
        ):
            if messagebox.askyesno(
                "Wavegen Settings",
                "The wavegen does not match the selected parameters.\n\nApply them now?",
            ):
                self._apply_wavegen()
            else:
                return
        self.plot.reset(params.point.describe())
        if not self.engine.start(params):
            messagebox.showinfo("Info", "Experiment already running.")

    def _toggle_pause(self) -> None:
        self.engine.toggle_pause()

    def _cancel_experiment(self) -> None:
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
            )
        )

    def _on_sample(self, elapsed: float, amps: float) -> None:
        self.last_current_label.config(text=f"Last Current: {amps:.6f} A")
        self.plot.append(elapsed, amps)
        self._update_smu_panel()

    def _on_engine_state(self, state: ExperimentState) -> None:
        running = state in (ExperimentState.RUNNING, ExperimentState.PAUSED)
        self.start_button.config(state="disabled" if running else "normal")
        self.pause_button.config(
            state="normal" if running else "disabled",
            text="Resume" if state == ExperimentState.PAUSED else "Pause",
        )
        self.cancel_button.config(state="normal" if running else "disabled")

    def _on_run_finished(self, success: bool, message: str) -> None:
        self._on_engine_state(ExperimentState.IDLE)
        self._update_smu_panel()
        self.tracker.refresh()
        self.status_bar.set_message(message or ("Experiment complete!" if success else "Stopped."))
        self._refresh_confirm_state()

    def _on_run_completed_worker(self, record) -> None:
        # worker thread: hand results to the (optional) Sheets mirror
        self.sheets.enqueue_run(record)

    def _status_async(self, message: str) -> None:
        self.after(0, self.status_bar.set_message, message)

    # ------------------------------------------------------------------
    # auto sequence
    # ------------------------------------------------------------------

    def _toggle_sequence(self) -> None:
        if self.sequence.active:
            self.sequence.cancel()
            self.sequence_button.config(text="Stopping...", state="disabled")
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
            )
            return
        if not messagebox.askyesno(
            "Start Auto Sequence",
            f"This will run {len(plan)} tests across configurations, duties, voltages "
            "and temperatures.\n\nVoltage is set automatically by the SMU; you will "
            "only be prompted for chamber temperature changes.\n\nContinue?",
        ):
            return
        if self.sequence.start(plan, params.duration_minutes, params.find_zvs):
            self.sequence_button.config(text="Stop Auto Sequence")
            self.status_bar.set_message("Auto sequence starting...")

    def _prompt_operator(self, title: str, message: str) -> bool:
        return bool(
            call_on_ui_thread(
                self,
                lambda: messagebox.askokcancel(
                    title, f"{message}\n\nClick OK when ready or Cancel to stop the sequence."
                ),
            )
        )

    def _on_sequence_finished(self, _success: bool, message: str) -> None:
        def update():
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

        def worker():
            try:
                path = reports_mod.generate_device_report(self.db, device, dest)
                self.after(0, lambda: self.status_bar.set_message(f"Report written: {path}"))
            except Exception as exc:
                log.exception("report generation failed")
                self.after(0, lambda: messagebox.showerror("Report", f"Report failed: {exc}"))

        threading.Thread(target=worker, daemon=True).start()
        self.status_bar.set_message("Generating report...")

    def _export_csv(self) -> None:
        device = self._require_device()
        if device is None:
            return
        dest = self.settings.db_path.parent / "exports"
        try:
            paths = export_mod.export_device(self.db, device, dest)
            self.status_bar.set_message("Exported: " + ", ".join(str(p) for p in paths))
        except Exception as exc:
            messagebox.showerror("Export", f"Export failed: {exc}")

    # ------------------------------------------------------------------

    def _on_closing(self) -> None:
        if getattr(self, "_closing", False):
            return
        self._closing = True

        device = self.device_name_var.get().strip()
        if device:
            self._persist_device_options(device)
        self._save_last_params()

        was_busy = self.engine.is_busy() or self.sequence.active
        self.sequence.cancel()
        self.engine.cancel()

        # Hardware shutdown happens off the UI thread: with an unreachable
        # instrument every SCPI write blocks on a connect timeout, which
        # must never freeze the close.
        def shutdown_hardware():
            try:
                if self.smu.output_is_on:
                    self.smu.emergency_off()
            except Exception:
                pass

        worker = threading.Thread(target=shutdown_hardware, daemon=True)
        worker.start()

        # Give a running experiment time to ramp the bus down; close almost
        # immediately when the rig was idle.
        deadline = time.monotonic() + (10.0 if was_busy else 1.0)
        self.status_bar.set_message("Shutting down...")

        def poll_shutdown():
            engine_done = not self.engine.is_busy()
            if (engine_done and not worker.is_alive()) or time.monotonic() > deadline:
                self.destroy()
            else:
                self.after(50, poll_shutdown)

        poll_shutdown()
