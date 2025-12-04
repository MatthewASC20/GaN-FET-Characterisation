import tkinter as tk
from tkinter import ttk, messagebox
from tkmacosx import Button as MacButton
from tracker import ExperimentTracker
from config import *
import config
from instrument_utils import *
from data_utils import generate_filename, log_data_to_csv, freq_label, DEVICE_DATA_ROOT
from experiment import *
import matplotlib
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.pyplot as plt
from collections import deque
import threading
import os
import json
from sheet_utils import log_final_results_to_sheet, log_timeseries_sample_to_sheet
from typing import Callable, Optional, Dict, Any, List, Tuple
import copy
from dataclasses import dataclass
from enum import Enum
import csv
from api import setup_device_in_drive, load_mappings
from pathlib import Path
import time

matplotlib.use('TkAgg')

# Constants
PARAMS_FILE = "last_params.json"
PLOT_MAX_POINTS = 100
VOLTAGE_TOLERANCE = 2.0
DEFAULT_GEOMETRY = "1700x950"
AUTOTUNE_STEP_HZ = 100_000  # 0.1 MHz
AUTOTUNE_STEP_DELAY = 0.5   # seconds between steps
AUTOTUNE_BUTTON_WIDTH = 160
AUTOTUNE_BUTTON_HEIGHT = 36
TIME_SERIES_LOG_INTERVAL = 10  # seconds between sheet time-series uploads


def sanitize_device_name(raw_name: str) -> str:
    """Sanitize device names for filesystem usage."""
    if not raw_name:
        return ""
    return "".join(
        c for c in raw_name.strip() if c.isalnum() or c in (" ", "_", "-")
    )


class DeviceConfigStore:
    """Persist per-device parameter configurations."""

    CONFIG_FILENAME = "device_config.json"

    def __init__(self, base_folder: Path = DEVICE_DATA_ROOT):
        self.base_folder = Path(base_folder)

    def _config_path(self, device_name: str) -> Optional[Path]:
        sanitized = sanitize_device_name(device_name)
        if not sanitized:
            return None
        return self.base_folder / sanitized / self.CONFIG_FILENAME

    def load(self, device_name: str) -> Optional[Dict[str, Any]]:
        path = self._config_path(device_name)
        if not path or not path.is_file():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:
            print(f"Warning: failed to read device config '{path}': {exc}")
            return None

    def save(self, device_name: str, data: Dict[str, Any]) -> bool:
        path = self._config_path(device_name)
        if not path:
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            return True
        except Exception as exc:
            print(f"Warning: failed to save device config '{path}': {exc}")
            return False

    def exists(self, device_name: str) -> bool:
        path = self._config_path(device_name)
        return bool(path and path.is_file())

class ExperimentState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLED = "cancelled"

@dataclass
class ExperimentParams:
    """Data class to hold experiment parameters"""
    device_name: str
    config: str
    frequency: int
    duty: int
    temperature: int
    voltage: int
    duration_minutes: float


class StatusBar(tk.Frame):
    """Status bar component for displaying messages"""
    
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.label = tk.Label(self, text="Ready.", anchor="w")
        self.label.pack(fill=tk.X)
        # Changed from pack to grid to be consistent with parent
        self.grid(row=999, column=0, columnspan=4, sticky="ew", pady=(5, 0))

    def set_message(self, message: str):
        """Set status message"""
        self.label.config(text=message)
        self.label.update_idletasks()

    def clear(self):
        """Clear status message"""
        self.set_message("")

class NotificationManager:
    """Handles popup notifications"""
    
    @staticmethod
    def show_temporary_popup(root, message: str, duration_ms: int = 1000):
        """Show a temporary popup that auto-closes"""
        popup = tk.Toplevel(root)
        popup.title("Notification")
        popup.geometry("300x100")
        popup.attributes("-topmost", True)
        popup.resizable(False, False)
        
        label = tk.Label(popup, text=message, font=("Arial", 14))
        label.pack(expand=True, fill="both", padx=10, pady=10)
        
        popup.after(duration_ms, popup.destroy)
        popup.grab_set()

class ParamButtonGroup(tk.Frame):
    def __init__(self, parent, options, variable, label_text):
        super().__init__(parent)
        self.variable = variable
        self.buttons: Dict[Any, tk.Radiobutton] = {}
        self.options = list(options)  # Store options for reference

        self.grid_columnconfigure(1, weight=1)

        self.label_widget = ttk.Label(self, text=label_text)
        self.label_widget.grid(row=0, column=0, sticky="e", padx=(0, 12))

        self.buttons_frame = tk.Frame(self)
        self.buttons_frame.grid(row=0, column=1, sticky="ew")

        self._build_buttons()

        # Trace variable changes
        self.variable.trace_add("write", lambda *args: self.after(1, self._highlight_selected))

        # Initial highlight after a short delay
        self.after(100, self._highlight_selected)

    def _on_button_click(self, value):
        self.variable.set(value)
        # Force immediate update
        self._highlight_selected()

    def _build_buttons(self):
        for child in self.buttons_frame.winfo_children():
            child.destroy()
        self.buttons.clear()

        uniform_id = f"param_btn_cols_{id(self)}"
        for col in range(3):
            self.buttons_frame.grid_columnconfigure(col, weight=1, uniform=uniform_id, minsize=120)

        button_kwargs = {
            "indicatoron": False,
            "selectcolor": "#2196F3",
            "background": "#E0E0E0",
            "foreground": "black",
            "activebackground": "#1976D2",
            "activeforeground": "white",
            "highlightthickness": 0,
            "borderwidth": 1,
            "relief": "raised",
            "padx": 10,
            "pady": 6,
        }

        for idx, (value, text) in enumerate(self.options):
            row = idx // 3
            col = idx % 3
            button = tk.Radiobutton(
                self.buttons_frame,
                variable=self.variable,
                value=value,
                text=text,
                command=lambda v=value: self._on_button_click(v),
                **button_kwargs,
            )
            button.grid(row=row, column=col, padx=6, pady=4, sticky="nsew")
            self.buttons[value] = button

        if self.options:
            remainder = len(self.options) % 3
            if remainder:
                last_row = (len(self.options) - 1) // 3
                for col in range(remainder, 3):
                    spacer = tk.Frame(self.buttons_frame, height=1, bd=0, relief="flat")
                    spacer.grid(row=last_row, column=col, padx=6, pady=4, sticky="nsew")
        else:
            for col in range(3):
                spacer = tk.Frame(self.buttons_frame, height=1, bd=0, relief="flat")
                spacer.grid(row=0, column=col, padx=6, pady=4, sticky="nsew")

        self._ensure_variable_in_options()

    def _highlight_selected(self):
        """Update button highlighting based on current variable value"""
        try:
            selected = self.variable.get()

            for value, button in self.buttons.items():
                if value == selected:
                    # Selected button - blue background
                    button.config(
                        bg="#2196F3",
                        fg="white",
                        activebackground="#1976D2",
                        activeforeground="white",
                        selectcolor="#2196F3",
                        relief="sunken",
                    )
                else:
                    # Unselected button - gray background
                    button.config(
                        bg="#E0E0E0",
                        fg="black",
                        activebackground="#D0D0D0",
                        activeforeground="black",
                        selectcolor="#E0E0E0",
                        relief="raised",
                    )

            # Multiple update methods to force refresh
            self.update_idletasks()

        except Exception as e:
            print(f"Error in _highlight_selected: {e}")

    def refresh(self):
        """Public method to force refresh the visual state"""
        self._highlight_selected()

    def _ensure_variable_in_options(self):
        current_value = self.variable.get()
        option_values = [value for value, _ in self.options]
        if option_values and current_value not in option_values:
            self.variable.set(option_values[0])

    def set_options(self, options: List[Tuple[Any, str]]):
        """Replace option set and rebuild buttons"""
        self.options = list(options)
        self._build_buttons()
        self.after(10, self._highlight_selected)


class ParameterListEditor(ttk.LabelFrame):
    """Reusable editor for managing parameter option lists."""

    def __init__(
        self,
        master,
        title: str,
        initial_options: List[Tuple[Any, str]],
        on_change: Callable[[List[Tuple[Any, str]]], Optional[List[Tuple[Any, str]]]],
        *,
        value_parser: Callable[[str], Any],
        default_label_factory: Callable[[Any], str],
        default_options: List[Tuple[Any, str]],
    ):
        super().__init__(master, text=title)
        self.on_change = on_change
        self.value_parser = value_parser
        self.default_label_factory = default_label_factory
        self.default_options = list(default_options)
        self.options: List[Tuple[Any, str]] = []

        self._build_widgets()
        self.set_options(initial_options)

    def _build_widgets(self):
        self.tree = ttk.Treeview(
            self,
            columns=("value",),
            show="headings",
            height=6,
        )
        self.tree.heading("value", text="Value")
        self.tree.column("value", anchor="center", width=160)
        self.tree.grid(row=0, column=0, columnspan=3, sticky="nsew", padx=5, pady=5)

        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        ttk.Label(self, text="Value").grid(row=1, column=0, sticky="w", padx=5)

        self.value_entry = ttk.Entry(self)
        self.value_entry.grid(row=2, column=0, columnspan=2, sticky="ew", padx=5, pady=(0, 5))

        self.add_button = ttk.Button(self, text="Add / Update", command=self._handle_add_or_update)
        self.add_button.grid(row=2, column=2, sticky="ew", padx=5, pady=(0, 5))

        self.delete_button = ttk.Button(self, text="Delete Selected", command=self._handle_delete)
        self.delete_button.grid(row=3, column=0, sticky="ew", padx=5, pady=(0, 5))

        self.reset_button = ttk.Button(self, text="Reset to Defaults", command=self._handle_reset)
        self.reset_button.grid(row=3, column=1, sticky="ew", padx=5, pady=(0, 5))

        self.clear_button = ttk.Button(self, text="Clear Field", command=self._clear_entries)
        self.clear_button.grid(row=3, column=2, sticky="ew", padx=5, pady=(0, 5))

        for col in range(3):
            weight = 1 if col == 0 or col == 1 else 0
            self.grid_columnconfigure(col, weight=weight)
        self.grid_rowconfigure(0, weight=1)

    def set_options(self, options: List[Tuple[Any, str]]):
        self.options = list(options)
        for row in self.tree.get_children():
            self.tree.delete(row)
        for value, _label in self.options:
            self.tree.insert("", "end", values=(value,))
        self._clear_entries()

    def _handle_add_or_update(self):
        value_raw = self.value_entry.get().strip()
        if not value_raw:
            messagebox.showerror("Input Error", "Please provide a value.")
            return

        try:
            parsed_value = self.value_parser(value_raw)
        except Exception:
            messagebox.showerror("Input Error", f"Invalid value: {value_raw}")
            return

        label = str(self.default_label_factory(parsed_value))

        updated = self._merge_options((parsed_value, label))
        normalized = self.on_change(updated)
        if normalized is not None:
            self.set_options(normalized)

    def _merge_options(self, entry: Tuple[Any, str]) -> List[Tuple[Any, str]]:
        value, label = entry
        merged: List[Tuple[Any, str]] = []
        found = False
        for existing_value, existing_label in self.options:
            if existing_value == value:
                merged.append((value, label))
                found = True
            else:
                merged.append((existing_value, existing_label))
        if not found:
            merged.append((value, label))
        return merged

    def _handle_delete(self):
        selected_items = self.tree.selection()
        if not selected_items:
            messagebox.showerror("Selection Error", "Please select an entry to delete.")
            return

        remaining: List[Tuple[Any, str]] = []
        selected_values = {
            self.tree.item(item, "values")[0]
            for item in selected_items
        }

        for value, label in self.options:
            if str(value) not in selected_values:
                remaining.append((value, label))

        normalized = self.on_change(remaining)
        if normalized is not None:
            self.set_options(normalized)

    def _handle_reset(self):
        normalized = self.on_change(list(self.default_options))
        if normalized is not None:
            self.set_options(normalized)

    def _clear_entries(self):
        self.value_entry.delete(0, tk.END)

    def _on_tree_select(self, _event):
        selection = self.tree.selection()
        if not selection:
            return
        item = selection[0]
        (value,) = self.tree.item(item, "values")
        self.value_entry.delete(0, tk.END)
        self.value_entry.insert(0, value)



class InstrumentConfigEditor(ttk.LabelFrame):
    """Editor for instrument IP/port configuration."""

    def __init__(
        self,
        master,
        *,
        initial_configs: Dict[str, Dict[str, Any]],
        default_configs: Dict[str, Dict[str, Any]],
        on_change: Callable[[Dict[str, Dict[str, Any]]], Optional[Dict[str, Dict[str, Any]]]],
    ):
        super().__init__(master, text="Instrument Connections")
        self.on_change = on_change
        self.default_configs = copy.deepcopy(default_configs)
        self.entries: Dict[str, Dict[str, ttk.Entry]] = {}
        self.current_configs: Dict[str, Dict[str, Any]] = copy.deepcopy(initial_configs)
        self._build_widgets(self.current_configs)

    def _build_widgets(self, configs: Dict[str, Dict[str, Any]]):
        for child in self.winfo_children():
            child.destroy()
        self.entries.clear()

        ttk.Label(self, text="Instrument").grid(row=0, column=0, padx=5, pady=(5, 2), sticky="w")
        ttk.Label(self, text="IP Address").grid(row=0, column=1, padx=5, pady=(5, 2), sticky="w")
        ttk.Label(self, text="Port").grid(row=0, column=2, padx=5, pady=(5, 2), sticky="w")

        for row, (name, settings) in enumerate(configs.items(), start=1):
            ttk.Label(self, text=name).grid(row=row, column=0, padx=5, pady=2, sticky="w")

            ip_entry = ttk.Entry(self)
            ip_entry.insert(0, settings.get("ip", ""))
            ip_entry.grid(row=row, column=1, padx=5, pady=2, sticky="ew")

            port_entry = ttk.Entry(self, width=8)
            port_entry.insert(0, str(settings.get("port", "")))
            port_entry.grid(row=row, column=2, padx=5, pady=2, sticky="ew")

            self.entries[name] = {"ip": ip_entry, "port": port_entry}

        button_row = len(configs) + 1
        apply_btn = ttk.Button(self, text="Apply Changes", command=self._apply_changes)
        apply_btn.grid(row=button_row, column=0, padx=5, pady=(10, 5), sticky="ew")

        reset_btn = ttk.Button(self, text="Reset to Defaults", command=self._reset_to_defaults)
        reset_btn.grid(row=button_row, column=1, padx=5, pady=(10, 5), sticky="ew")

        reload_btn = ttk.Button(self, text="Reload Current", command=self._reload_current)
        reload_btn.grid(row=button_row, column=2, padx=5, pady=(10, 5), sticky="ew")

        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_columnconfigure(2, weight=0)

    def set_configs(self, configs: Dict[str, Dict[str, Any]]):
        self.current_configs = copy.deepcopy(configs)
        if set(configs.keys()) != set(self.entries.keys()):
            self._build_widgets(self.current_configs)
            return

        for name, fields in self.entries.items():
            ip_entry = fields["ip"]
            port_entry = fields["port"]
            ip_entry.delete(0, tk.END)
            ip_entry.insert(0, configs.get(name, {}).get("ip", ""))
            port_entry.delete(0, tk.END)
            port_entry.insert(0, str(configs.get(name, {}).get("port", "")))

    def _reload_current(self):
        self.set_configs(self.current_configs)

    def _apply_changes(self):
        updated: Dict[str, Dict[str, Any]] = {}
        for name, fields in self.entries.items():
            ip = fields["ip"].get().strip()
            port_raw = fields["port"].get().strip()

            if not ip:
                messagebox.showerror("Validation Error", f"IP address is required for {name}.")
                return

            try:
                port = int(port_raw, 10)
            except ValueError:
                messagebox.showerror("Validation Error", f"Port must be a number for {name}.")
                return

            if not (0 < port <= 65535):
                messagebox.showerror("Validation Error", f"Port out of range for {name}.")
                return

            updated[name] = {"ip": ip, "port": port}

        normalized = self.on_change(updated)
        if normalized is not None:
            self.set_configs(normalized)

    def _reset_to_defaults(self):
        normalized = self.on_change(copy.deepcopy(self.default_configs))
        if normalized is not None:
            self.set_configs(normalized)
class PlotManager:
    """Manages the real-time plotting functionality"""
    
    def __init__(self, parent_frame):
        self.parent_frame = parent_frame
        self.time_vals = deque(maxlen=PLOT_MAX_POINTS)
        self.current_vals = deque(maxlen=PLOT_MAX_POINTS)
        self.start_time = None
        self._setup_plot()
    
    def _setup_plot(self):
        """Initialize the plot"""
        self.fig, self.ax = plt.subplots(figsize=(7, 4))
        self.ax.set_xlabel('Time (s)')
        self.ax.set_ylabel('Current (A)')
        self.ax.set_title('Real-Time DC Current')
        self.ax.grid(True)
        self.ax.legend()
        
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.parent_frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=1)
    
    def reset_plot(self, title: str):
        """Reset plot for new experiment"""
        self.time_vals.clear()
        self.current_vals.clear()
        self.start_time = time.time()
        
        self.ax.cla()
        self.ax.set_xlabel('Time (s)')
        self.ax.set_ylabel('Current (A)')
        self.ax.set_title(title)
        self.ax.grid(True)
        self.ax.legend()
        self.canvas.draw()
    
    def update_plot(self, current: float):
        """Update plot with new current value"""
        if self.start_time is None:
            self.start_time = time.time()
        
        self.time_vals.append(time.time() - self.start_time)
        self.current_vals.append(current)
        
        self.ax.cla()
        self.ax.set_xlabel('Time (s)')
        self.ax.set_ylabel('Current (A)')
        self.ax.grid(True)
        self.ax.plot(self.time_vals, self.current_vals, label='DC Current (A)')
        
        # Set Y-axis limits
        if self.current_vals:
            y_min = 0.9 * min(self.current_vals)
            y_max = 1.1 * max(self.current_vals)
            self.ax.set_ylim(y_min, y_max)
        
        self.ax.legend()
        self.canvas.draw()

class WavegenController:
    """Handles wavegen configuration and confirmation"""
    
    def __init__(self, gui_ref):
        self.gui = gui_ref
        self.last_confirmed_config = None
        self.last_confirmed_freq = None
        self.last_confirmed_duty = None
        self.last_applied_freq = None  # Track the actual frequency applied (including tuned)
    
    def has_changes(self) -> bool:
        """Check if there are unconfirmed changes"""
        # If we haven't confirmed anything yet, we have changes
        if self.last_confirmed_config is None:
            return True
            
        # Check against what was actually applied, not just what was selected
        current_freq = self.gui.frequency_var.get()
        effective_freq = self.last_applied_freq if self.last_applied_freq else self.last_confirmed_freq
        
        return (
            self.gui.config_var.get() != self.last_confirmed_config or
            current_freq != self.last_confirmed_freq or  # Compare against selected, not applied
            self.gui.duty_var.get() != self.last_confirmed_duty
        )
    
    def confirm_changes(self, applied_freq=None):
        """Confirm and apply wavegen changes"""
        new_config = self.gui.config_var.get()
        new_freq = self.gui.frequency_var.get()
        new_duty = self.gui.duty_var.get()
        
        try:
            freq_to_apply = applied_freq if applied_freq is not None else new_freq

            # Apply changes based on what changed
            if new_config != self.last_confirmed_config:
                configure_wavegen(new_config, freq_to_apply, new_duty)
            else:
                # Only update frequency if no tuned frequency is being applied
                if applied_freq is None:
                    if new_freq != self.last_confirmed_freq:
                        query_scpi("SDG6022X", f"C1:BSWV FRQ,{new_freq}")
                        if new_config == "Dual Conduction":
                            query_scpi("SDG6022X", f"C2:BSWV FRQ,{new_freq}")

                if new_duty != self.last_confirmed_duty:
                    query_scpi("SDG6022X", f"C1:BSWV DUTY,{new_duty}")
                    if new_config == "Dual Conduction":
                        query_scpi("SDG6022X", f"C2:BSWV DUTY,{new_duty}")
            
            # Update confirmed state
            self.last_confirmed_config = new_config
            self.last_confirmed_freq = new_freq
            self.last_confirmed_duty = new_duty
            self.last_applied_freq = applied_freq if applied_freq else new_freq
            
            return True
            
        except Exception as e:
            messagebox.showerror("Wavegen Error", f"Failed to configure wavegen: {e}")
            return False

class ExperimentRunner:
    """Handles experiment execution logic"""
    
    def __init__(self, gui_ref):
        self.gui = gui_ref
        self.serial_connection = None
        self.state = ExperimentState.IDLE
        self.experiment_thread = None
        self._rms_current_samples: List[float] = []
        self._last_timeseries_sheet_log = 0.0
    
    def start_experiment(self, params: ExperimentParams):
        """Start experiment in separate thread"""
        if self.state != ExperimentState.IDLE:
            messagebox.showinfo("Info", "Experiment already running.")
            return
        
        self.state = ExperimentState.RUNNING
        self.experiment_thread = threading.Thread(
            target=self._run_experiment, 
            args=(params,), 
            daemon=True
        )
        self.experiment_thread.start()
    
    def pause_experiment(self):
        """Toggle experiment pause state"""
        if self.state == ExperimentState.RUNNING:
            self.state = ExperimentState.PAUSED
        elif self.state == ExperimentState.PAUSED:
            self.state = ExperimentState.RUNNING
    
    def cancel_experiment(self):
        """Cancel running experiment"""
        if self.state in (ExperimentState.RUNNING, ExperimentState.PAUSED):
            self.state = ExperimentState.CANCELLED
    
    def _run_experiment(self, params: ExperimentParams):
        """Run the actual experiment (runs in separate thread)"""
        try:
            # Initialize connection
            self.serial_connection = initialize_connection(SERIAL_PORT, BAUD_RATE, TIMEOUT)
            if not self.serial_connection:
                messagebox.showerror("Serial Error", "Could not open serial port.")
                return
            
            configure_prologix(self.serial_connection, GPIB_ADDR)
            
            # Run experiment
            success = self._execute_experiment(params)
            
            # Cleanup
            reset_to_local(self.serial_connection)
            self.serial_connection.close()
            self.serial_connection = None
            
                
        except Exception as e:
            messagebox.showerror("Error", f"Experiment failed: {e}")
        finally:
            self.state = ExperimentState.IDLE
            self.gui.after(0, self.gui._reset_ui_after_experiment)
    
    def _execute_experiment(self, params: ExperimentParams) -> bool:
        """Execute the main experiment logic"""
        filename = generate_filename(
            params.device_name, params.temperature, params.frequency,
            params.voltage, params.config, params.duty
        )

        # Clear per-run RMS snapshots
        self._rms_current_samples = []
        
        # Check for file overwrite
        if not self._check_file_overwrite(filename):
            return False
        
        # Validate peak voltage
        if not self._validate_peak_voltage(params.voltage):
            return False
        
        # Setup data logging
        header = ['Timestamp', 'Device Name', 'Temperature (°C)', 'Frequency (Hz)', 
                 'Voltage (V)', 'Configuration', 'Duty Cycle (%)', 'Current (A)']
        
        # Reset plot
        plot_title = (f'{params.device_name}: {params.temperature}°C, '
                     f'{params.frequency/1e6:.0f}MHz, {params.voltage}V, '
                     f'{params.config}, {params.duty}% Duty')
        self.gui.after(0, lambda: self.gui.plot_manager.reset_plot(plot_title))
        
        # Run measurement loop
        duration_seconds = int(params.duration_minutes * 60)
        start_time = time.time()
        
        while (self.state in (ExperimentState.RUNNING, ExperimentState.PAUSED) and 
               (time.time() - start_time) < duration_seconds):
            
            if self.state == ExperimentState.CANCELLED:
                return False
            
            # Wait while paused
            while self.state == ExperimentState.PAUSED:
                time.sleep(0.1)
                if self.state == ExperimentState.CANCELLED:
                    return False
            
            # Measure current
            current = measure_dc_current(self.serial_connection)
            if current is not None:
                rms_sample = self._safe_read_rms_current()
                if rms_sample is not None:
                    self._rms_current_samples.append(rms_sample)

                timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
                
                # Log data
                log_data_to_csv(filename, [
                    timestamp, params.device_name, params.temperature,
                    params.frequency, params.voltage, params.config,
                    params.duty, current
                ], header=header)

                # Periodically log RMS/DC voltage snapshots to Google Sheets
                self._maybe_log_timeseries_to_sheet(
                    params=params,
                    timestamp=timestamp,
                    dc_current=current,
                    rms_snapshot=rms_sample,
                )
                
                # Update UI
                self.gui.after(0, lambda c=current: self.gui._update_current_display(c))
                self.gui.after(0, lambda c=current: self.gui.plot_manager.update_plot(c))
        
        # Capture final readings
        if self.state != ExperimentState.CANCELLED:
            self._capture_final_readings(filename, params)
        
        return True

    def _safe_read_rms_current(self) -> Optional[float]:
        """Read RMS current safely for per-run averaging."""
        raw_value = None
        try:
            raw_value = get_oscilloscope_rms_current()
            if raw_value is None:
                return None
            return float(raw_value)
        except (TypeError, ValueError):
            print(f"Warning: RMS current reading invalid: {raw_value}")
        except Exception as exc:
            print(f"Warning: RMS current read failed: {exc}")
        return None

    def _safe_read_isw_rms(self) -> Optional[float]:
        """Read Isw RMS current safely."""
        raw_value = None
        try:
            raw_value = get_oscilloscope_isw_rms()
            if raw_value is None:
                return None
            return float(raw_value)
        except (TypeError, ValueError):
            print(f"Warning: Isw RMS reading invalid: {raw_value}")
        except Exception as exc:
            print(f"Warning: Isw RMS read failed: {exc}")
        return None

    def _safe_read_dc_voltage(self) -> Optional[float]:
        """Read DC voltage safely."""
        raw_value = None
        try:
            raw_value = get_multimeter_voltage()
            if raw_value is None:
                return None
            return float(raw_value)
        except (TypeError, ValueError):
            print(f"Warning: DC voltage reading invalid: {raw_value}")
        except Exception as exc:
            print(f"Warning: DC voltage read failed: {exc}")
        return None

    def _maybe_log_timeseries_to_sheet(
        self,
        params: ExperimentParams,
        timestamp: str,
        dc_current: Optional[float],
        rms_snapshot: Optional[float],
    ) -> None:
        """Append periodic RMS/DC voltage snapshots to the Google Sheet."""
        now = time.time()
        if now - self._last_timeseries_sheet_log < TIME_SERIES_LOG_INTERVAL:
            return

        try:
            dc_voltage = self._safe_read_dc_voltage()
            rms_current = rms_snapshot if rms_snapshot is not None else self._safe_read_rms_current()
            isw_rms = self._safe_read_isw_rms()

            success, error_message = log_timeseries_sample_to_sheet(
                device_name=params.device_name,
                freq=params.frequency,
                temp=params.temperature,
                config=params.config,
                duty=params.duty,
                voltage=params.voltage,
                timestamp=timestamp,
                dc_voltage=dc_voltage,
                rms_current=rms_current,
                isw_rms=isw_rms,
                dc_current=dc_current,
            )

            if not success:
                detail = f": {error_message}" if error_message else "."
                print(f"Failed to log time-series sample to Google Sheet{detail}")
        except Exception as exc:
            print(f"Warning: time-series sheet logging failed: {exc}")
        finally:
            self._last_timeseries_sheet_log = now
    
    def _check_file_overwrite(self, filename: str) -> bool:
        """Check if file exists and confirm overwrite"""
        if os.path.isfile(filename):
            result = messagebox.askyesno(
                "Overwrite Confirmation",
                (
                    "An existing data log was found for this test setup.\n\n"
                    "File:\n"
                    f"  {os.path.basename(filename)}\n\n"
                    "Overwrite the current file with new results?"
                )
            )
            if not result:
                messagebox.showinfo("Experiment Cancelled", "Test was cancelled.")
                return False
            else:
                # Clear the file
                with open(filename, "w"):
                    pass
        return True
    
    def _validate_peak_voltage(self, expected_voltage: int) -> bool:
        """Validate that peak voltage matches expected value"""
        def _read_peak() -> float:
            raw = get_oscilloscope_peak_voltage()
            return float(raw)

        try:
            peak_voltage = _read_peak()

            # If the first read looks stale (often showing last test), retry once after a short delay.
            if abs(peak_voltage - expected_voltage) > VOLTAGE_TOLERANCE:
                time.sleep(0.3)
                peak_voltage = _read_peak()

            if abs(peak_voltage - expected_voltage) > VOLTAGE_TOLERANCE:
                result = messagebox.askyesno(
                    "Validation Error",
                    f"Peak voltage ({peak_voltage}V) is not within "
                    f"±{VOLTAGE_TOLERANCE}V of selected voltage ({expected_voltage}V).\n\n"
                    f"Do you want to continue?"
                )
                if not result:
                    messagebox.showinfo("Experiment Cancelled", "Test was cancelled.")
                    return False
            return True
        except Exception as e:
            messagebox.showerror("Instrument Error", f"Peak voltage check failed: {e}")
            return False
    
    def _capture_final_readings(self, filename: str, params: ExperimentParams):
        """Capture and log final instrument readings"""
        try:
            def _get_float_reading_with_retries(
                fetcher,
                label: str,
                *,
                retries: int = 2,
                delay_s: float = 0.2,
                allow_none: bool = False,
            ) -> Optional[float]:
                """Safely convert instrument readings to float with simple retries."""
                last_error: Optional[Exception] = None
                for attempt in range(retries + 1):
                    raw_value = fetcher()
                    if raw_value is None:
                        last_error = ValueError(f"{label} reading unavailable")
                    else:
                        try:
                            return float(raw_value)
                        except (TypeError, ValueError):
                            last_error = ValueError(f"{label} reading invalid: {raw_value}")
                    if attempt < retries:
                        time.sleep(delay_s)
                if allow_none:
                    print(f"Warning: {label} unavailable after retries: {last_error}")
                    return None
                raise last_error if last_error else ValueError(f"{label} reading failed")

            def _average_rms_current() -> float:
                """Use runtime average if available, otherwise take a fresh reading."""
                samples = [val for val in self._rms_current_samples if val is not None]
                if samples:
                    return sum(samples) / len(samples)
                return _get_float_reading_with_retries(get_oscilloscope_rms_current, "RMS current")

            dc_voltage = _get_float_reading_with_retries(get_multimeter_voltage, "DC voltage")
            rms_current = _average_rms_current()
            isw_rms = _get_float_reading_with_retries(get_oscilloscope_isw_rms, "Isw RMS current")
            vds_pk = _get_float_reading_with_retries(
                get_oscilloscope_peak_voltage,
                "Vds peak voltage",
                retries=3,
                delay_s=0.3,
                allow_none=True,
            )
            dc_current = self.gui.last_current_value
            if dc_current is None:
                raise ValueError("DC current reading unavailable")
            measured_freq = _get_float_reading_with_retries(get_wavegen_frequency, "Wavegen frequency")

            # Log to CSV
            log_data_to_csv(filename, [
                "FINAL_READINGS", dc_voltage, rms_current, dc_current,
                measured_freq, isw_rms, vds_pk
            ])

            # Log to Google Sheet
            success, error_message = log_final_results_to_sheet(
                device_name=params.device_name,
                freq=params.frequency,
                temp=params.temperature,
                config=params.config,
                duty=params.duty,
                voltage=params.voltage,
                vin=dc_voltage,
                iin=dc_current,
                fsw=measured_freq / 1e6,
                irms=rms_current,
                vds_pk=vds_pk,
                isw=isw_rms
            )

            print("\n=== Final Instrument Readings ===")
            print(f"DC Voltage: {dc_voltage} V")
            print(f"RMS Current: {rms_current} A")
            print(f"DC Current: {dc_current} A")
            print(f"Frequency: {measured_freq} Hz")
            print(f"Vds Peak: {vds_pk if vds_pk is not None else 'N/A'} V")

            if params.config == "Dual Conduction":
                print(f"Isw RMS Current: {isw_rms} A")

            if success:
                print("Logged final readings to Google Sheet.")
            else:
                detail = f": {error_message}" if error_message else "."
                print(f"Failed to log final readings to Google Sheet{detail}")

        except Exception as e:
            print(f"Error capturing final readings: {e}")

        # Screenshot handling (always runs, even if logging fails)
        capture_oscilloscope_screenshot_pyvisa(OSCILLOSCOPE, filename)



class ParameterManager:
    """Manages parameter saving and loading"""
    
    def __init__(self, gui_ref):
        self.gui = gui_ref
    
    def save_params(self):
        """Save current parameters to file"""
        params = {
            "device_name": self.gui.device_name_var.get(),
            "config": self.gui.config_var.get(),
            "frequency": self.gui.frequency_var.get(),
            "duty": self.gui.duty_var.get(),
            "temperature": self.gui.temperature_var.get(),
            "voltage": self.gui.voltage_var.get(),
            "duration": self.gui.duration_entry.get()
        }
        
        try:
            with open(PARAMS_FILE, "w") as f:
                json.dump(params, f, indent=2)
        except Exception as e:
            print(f"Error saving parameters: {e}")
    
    def load_params(self, preloaded_params: Optional[Dict[str, Any]] = None):
        """Load parameters from file"""
        params: Optional[Dict[str, Any]] = None
        try:
            if preloaded_params is not None:
                params = preloaded_params
            elif os.path.isfile(PARAMS_FILE):
                with open(PARAMS_FILE, "r", encoding="utf-8") as fh:
                    params = json.load(fh)

            if not params:
                return

            self.gui.device_name_var.set(params.get("device_name", ""))
            self._assign_option(self.gui.config_var, params.get("config"), "configurations", cast=str)
            self._assign_option(self.gui.frequency_var, params.get("frequency"), "frequencies", cast=int)
            self._assign_option(self.gui.duty_var, params.get("duty"), "duties", cast=int)
            self._assign_option(self.gui.temperature_var, params.get("temperature"), "temperatures", cast=int)
            self._assign_option(self.gui.voltage_var, params.get("voltage"), "voltages", cast=int)

            duration = params.get(
                "duration",
                VOLTAGE_DEFAULT_MINUTES.get(self.gui.voltage_var.get(), 1),
            )
            self.gui._cached_duration = duration

        except Exception as exc:
            print(f"Error loading parameters: {exc}")
            self.gui._cached_duration = VOLTAGE_DEFAULT_MINUTES.get(self.gui.voltage_var.get(), 1)

    def _assign_option(self, variable, raw_value, key: str, *, cast):
        if raw_value is None:
            return
        try:
            value = cast(raw_value)
        except (TypeError, ValueError):
            return

        available = [option_value for option_value, _ in self.gui.param_options.get(key, [])]
        if value in available:
            variable.set(value)
        elif available:
            variable.set(available[0])

class GaNExperimentGUI(tk.Tk):
    """Main GUI application class"""
    
    def __init__(self):
        super().__init__()
        self.title("GaN Device Test Runner")
        self.geometry(DEFAULT_GEOMETRY)
        self.protocol("WM_DELETE_WINDOW", self._on_closing)

        # Track default and current parameter option sets
        self.default_param_options = {
            "configurations": [tuple(option) for option in CONFIGURATIONS],
            "frequencies": [tuple(option) for option in FREQUENCIES],
            "duties": [tuple(option) for option in DUTIES],
            "temperatures": [tuple(option) for option in TEMPERATURES],
            "voltages": [tuple(option) for option in VOLTAGES],
        }

        self.device_config_store = DeviceConfigStore()
        self._ui_ready = False
        self._active_device_display_name = ""
        self._active_device_sanitized = ""
        self._autotune_running = False
        self._auto_sequence_active = False
        self._auto_sequence_cancel = threading.Event()
        self._auto_sequence_thread: Optional[threading.Thread] = None
        self._auto_sequence_duration = 0.0
        self.auto_sequence_button = None
        self._autotune_voltage_prompt_pending = False
        self._last_confirmed_voltage: Optional[int] = None

        initial_params = self._read_last_params_file()
        initial_device = initial_params.get("device_name", "") if initial_params else ""

        self.param_options = self._load_device_param_options(initial_device)
        self.parameter_editors: Dict[str, ParameterListEditor] = {}

        self.default_instrument_configs = copy.deepcopy(INSTRUMENTS)
        self.instrument_configs = copy.deepcopy(INSTRUMENTS)

        # Initialize variables
        self._init_variables()
        self._drive_setup_cache = self._load_drive_setup_cache()

        # Initialize managers
        self.param_manager = ParameterManager(self)
        self.wavegen_controller = WavegenController(self)
        self.experiment_runner = ExperimentRunner(self)
        
        # Load parameters and build UI
        self.param_manager.load_params(initial_params)
        self._active_device_display_name = self.device_name_var.get().strip()
        self._active_device_sanitized = sanitize_device_name(self._active_device_display_name)
        self._activate_device(self._active_device_display_name, persist_previous=False, force_reload=True)

        self._build_ui()
        self._ui_ready = True
        self._apply_current_param_options_to_ui()
        self._setup_callbacks()
        
        # Initialize state
        self.last_current_value = None
        if not hasattr(self, "_cached_duration"):
            self._cached_duration = VOLTAGE_DEFAULT_MINUTES.get(self.voltage_var.get(), 1)

        # Update UI after loading    

    def _read_last_params_file(self) -> Optional[Dict[str, Any]]:
        """Return cached parameter selections if available."""
        if not os.path.isfile(PARAMS_FILE):
            return None
        try:
            with open(PARAMS_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except Exception as exc:
            print(f"Warning: failed to read '{PARAMS_FILE}': {exc}")
        return None

    def _init_variables(self):
        """Initialize tkinter variables"""
        self.device_name_var = tk.StringVar(value="")
        self.config_var = tk.StringVar(value=self._get_default_option_value("configurations", ""))
        self.frequency_var = tk.IntVar(value=self._get_default_option_value("frequencies", 0))
        self.duty_var = tk.IntVar(value=self._get_default_option_value("duties", 0))
        self.temperature_var = tk.IntVar(value=self._get_default_option_value("temperatures", 0))
        self.voltage_var = tk.IntVar(value=self._get_default_option_value("voltages", 0))

    def _build_ui(self):
        """Build the main UI"""
        # Root layout uses notebook tabs for experiment + configuration
        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=0, column=0, sticky="nsew")

        self.experiment_tab = ttk.Frame(self.notebook)
        self.configuration_tab = ttk.Frame(self.notebook)

        self.notebook.add(self.experiment_tab, text="Experiment")
        self.notebook.add(self.configuration_tab, text="Configuration")

        # Main experiment frame
        self.main_frame = tk.Frame(self.experiment_tab)
        self.main_frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

        # Device name entry
        self._build_device_entry()

        # Parameter groups
        self._build_parameter_groups()
        
        # Duration entry
        self._build_duration_entry()
        
        # Current display and confirm button
        self._build_current_display()
        
        # Control buttons
        self._build_control_buttons()
        
        # Plot frame
        self._build_plot_frame()

        # Experiment tracker
        self._build_tracker_frame()

        # Configuration editor tab
        self._build_configuration_tab()

        # Status bar
        self.status_bar = StatusBar(self)

        # Configure grid weights
        self._configure_grid_weights()
    
    def _get_known_device_names(self):
        base_path = DEVICE_DATA_ROOT
        if not base_path.is_dir():
            return []
        return sorted(entry.name for entry in base_path.iterdir() if entry.is_dir())


    def _build_device_entry(self):
        """Build device name entry with dropdown + auto-create Drive folders"""
        tk.Label(self.main_frame, text="Device Name:").grid(
            row=0, column=0, sticky="e", padx=5, pady=5
        )

        known_devices = self._get_known_device_names()

        self.device_dropdown = ttk.Combobox(
            self.main_frame,
            textvariable=self.device_name_var,
            values=known_devices,
            width=20,
            state="normal"  # allows typing new name
        )
        self.device_dropdown.grid(row=0, column=1, sticky="w", padx=5, pady=5, columnspan=2)

        # Bind updates
        self.device_dropdown.bind("<<ComboboxSelected>>", self._on_device_committed)
        self.device_dropdown.bind("<FocusOut>", self._on_device_committed)
        self.device_dropdown.bind("<Return>", self._on_device_committed)

    def _on_device_committed(self, *args):
        name = self.device_name_var.get().strip()
        if not name:
            return

        self._activate_device(name)
        if hasattr(self, "tracker"):
            self.tracker.update_tracker()
        self._ensure_drive_setup(name, notify_on_success=True)

    def _ensure_drive_setup(self, device_name: str, *, notify_on_success: bool) -> bool:
        """Create Drive folder + spreadsheets for the device if needed."""
        if not device_name:
            return True

        if device_name in self._drive_setup_cache:
            return True

        try:
            setup_device_in_drive(device_name)
        except Exception as e:
            messagebox.showerror(
                "Drive Setup Failed",
                f"Google Drive setup failed:\n{e}"
            )
            return False

        # Update cache and dropdown values
        self._drive_setup_cache.add(device_name)
        current_values = self.device_dropdown.cget("values")
        if isinstance(current_values, str):
            current_values = [current_values] if current_values else []
        else:
            current_values = list(current_values)

        if device_name not in current_values:
            current_values.append(device_name)
            current_values.sort()
            self.device_dropdown["values"] = tuple(current_values)

        if notify_on_success:
            messagebox.showinfo("Folder Created", f"Drive setup created for: {device_name}")

        return True

    def _load_drive_setup_cache(self):
        """Return a set of device names that already have Drive mappings."""
        try:
            mappings = load_mappings()
        except Exception as e:
            print(f"Warning: failed to load Drive mappings: {e}")
            return set()

        cache = set()
        for key in mappings.keys():
            if "_" in key:
                cache.add(key.rsplit("_", 1)[0])
        return cache

    def _activate_device(
        self,
        device_name: str,
        *,
        persist_previous: bool = True,
        force_reload: bool = False,
    ) -> None:
        """Load parameter options for the selected device and persist previous."""
        raw_name = device_name.strip()
        new_sanitized = sanitize_device_name(raw_name)
        previous_sanitized = self._active_device_sanitized or ""

        if not force_reload and new_sanitized == previous_sanitized:
            self._active_device_display_name = raw_name
            return

        if persist_previous and previous_sanitized:
            self._persist_device_configuration(self._active_device_display_name, silent=True)

        self._active_device_display_name = raw_name
        self._active_device_sanitized = new_sanitized

        self.param_options = self._load_device_param_options(raw_name)

        if raw_name and not self.device_config_store.exists(raw_name):
            self._persist_device_configuration(raw_name, silent=True)

        self._apply_current_param_options_to_ui()

        if hasattr(self, "status_bar") and raw_name:
            self.status_bar.set_message(f"Loaded configuration for {raw_name}.")

    def _load_device_param_options(self, device_name: str) -> Dict[str, List[Tuple[Any, str]]]:
        """Return parameter options for the given device."""
        stored = self.device_config_store.load(device_name) or {}
        options: Dict[str, List[Tuple[Any, str]]] = {}

        for key, defaults in self.default_param_options.items():
            raw_values = stored.get(key)
            normalized = self._normalize_stored_options(key, raw_values)
            if normalized:
                options[key] = normalized
            else:
                options[key] = copy.deepcopy(defaults)
        return options

    def _normalize_stored_options(
        self,
        key: str,
        stored: Optional[List[Any]],
    ) -> List[Tuple[Any, str]]:
        if not stored:
            return []

        cleaned: List[Tuple[Any, str]] = []
        seen: set[Any] = set()

        for entry in stored:
            value: Any
            label: str = ""

            if isinstance(entry, dict):
                value = entry.get("value")
                label = str(entry.get("label", ""))
            elif isinstance(entry, (list, tuple)):
                if not entry:
                    continue
                value = entry[0]
                label = str(entry[1]) if len(entry) > 1 else ""
            else:
                value = entry

            if key == "configurations":
                value_str = str(value).strip()
                if not value_str or value_str in seen:
                    continue
                label_str = label.strip() or self._default_label_for(key, value_str)
                cleaned.append((value_str, label_str))
                seen.add(value_str)
            else:
                try:
                    value_int = int(value)
                except (TypeError, ValueError):
                    continue
                if value_int in seen:
                    continue
                label_str = label.strip() or self._default_label_for(key, value_int)
                cleaned.append((value_int, label_str))
                seen.add(value_int)

        if not cleaned:
            return []

        if key == "configurations":
            cleaned.sort(key=lambda item: item[1].lower())
        else:
            cleaned.sort(key=lambda item: item[0])
        return cleaned

    def _apply_current_param_options_to_ui(self) -> None:
        """Refresh all parameter controls using current option sets."""
        if not getattr(self, "_ui_ready", False):
            return

        group_map = {
            "configurations": getattr(self, "config_group", None),
            "frequencies": getattr(self, "frequency_group", None),
            "duties": getattr(self, "duty_group", None),
            "temperatures": getattr(self, "temperature_group", None),
            "voltages": getattr(self, "voltage_group", None),
        }

        for key, group in group_map.items():
            if group is not None and key in self.param_options:
                group.set_options(self.param_options[key])

        for key, editor in getattr(self, "parameter_editors", {}).items():
            if key in self.param_options:
                editor.set_options(self.param_options[key])

        if hasattr(self, "tracker"):
            self.tracker.update_parameter_space(
                configs=self.param_options.get("configurations", []),
                frequencies=self.param_options.get("frequencies", []),
                duties=self.param_options.get("duties", []),
                temperatures=self.param_options.get("temperatures", []),
                voltages=self.param_options.get("voltages", []),
            )

        self._update_duration_default()

    def _serialize_param_options(self) -> Dict[str, List[List[Any]]]:
        """Convert parameter options to a JSON-friendly structure."""
        payload: Dict[str, List[List[Any]]] = {}
        for key, entries in self.param_options.items():
            payload[key] = [[value, label] for value, label in entries]
        return payload

    def _persist_device_configuration(self, device_name: str, *, silent: bool = False) -> None:
        """Write the current options to disk for the given device."""
        sanitized = sanitize_device_name(device_name)
        if not sanitized:
            return

        saved = self.device_config_store.save(device_name, self._serialize_param_options())
        if saved and not silent and hasattr(self, "status_bar"):
            self.status_bar.set_message(f"Saved configuration for {device_name}.")
    
    def _build_parameter_groups(self):
        """Build parameter selection groups"""
        self.frequency_group = ParamButtonGroup(
            self.main_frame, self.param_options["frequencies"], self.frequency_var, "Frequency (Hz)"
        )
        self.frequency_group.grid(row=1, column=0, columnspan=3, pady=10, sticky="w")

        self.temperature_group = ParamButtonGroup(
            self.main_frame, self.param_options["temperatures"], self.temperature_var, "Temperature (°C)"
        )
        self.temperature_group.grid(row=2, column=0, columnspan=3, pady=10, sticky="w")

        self.duty_group = ParamButtonGroup(
            self.main_frame, self.param_options["duties"], self.duty_var, "Duty Cycle"
        )
        self.duty_group.grid(row=3, column=0, columnspan=3, pady=10, sticky="w")

        self.config_group = ParamButtonGroup(
            self.main_frame, self.param_options["configurations"], self.config_var, "Configuration"
        )
        self.config_group.grid(row=4, column=0, columnspan=3, pady=10, sticky="w")

        self.voltage_group = ParamButtonGroup(
            self.main_frame, self.param_options["voltages"], self.voltage_var, "Voltage (V)"
        )
        self.voltage_group.grid(row=5, column=0, columnspan=3, pady=10, sticky="w")

    
    def _build_duration_entry(self):
        """Build duration entry"""
        ttk.Label(self.main_frame, text="Duration (min):").grid(
            row=6, column=0, sticky="e", padx=5, pady=5
        )
        self.duration_entry = ttk.Entry(self.main_frame, width=10)
        init_minutes = self._get_cached_or_default_duration()
        self.duration_entry.insert(0, str(init_minutes))
        self.duration_entry.grid(row=6, column=1, sticky="w")
    
    def _build_current_display(self):
        """Build current display and confirm button"""
        self.last_current_label = ttk.Label(
            self.main_frame, text="Last Current: N/A A"
        )
        self.last_current_label.grid(row=7, column=0, sticky="w", padx=5, pady=5)
        
        self.confirm_button = MacButton(
            self.main_frame, text="Confirm Changes", 
            command=self._confirm_wavegen_changes,
            width=120, height=40, borderless=1, highlightthickness=1
        )
        self.confirm_button.grid(row=7, column=2, sticky="w", padx=5, pady=5)

        self.autotune_button = MacButton(
            self.main_frame,
            text="Autotune Unavailable",
            command=self._start_autotune_sequence,
            width=AUTOTUNE_BUTTON_WIDTH,
            height=AUTOTUNE_BUTTON_HEIGHT,
            borderless=1,
            highlightthickness=1,
        )
        self.autotune_button.grid(row=7, column=1, sticky="w", padx=5, pady=5)
        self._set_autotune_button_style(
            text="Autotune Unavailable",
            state="disabled",
            bg="#bdbdbd",
            fg="white",
            active_bg="#bdbdbd",
            active_fg="white",
        )
    
    def _build_control_buttons(self):
        """Build experiment control buttons"""
        self.button_frame = tk.Frame(self.main_frame)
        self.button_frame.grid(row=8, column=0, columnspan=3, pady=10, sticky="w")
        
        self.start_button = ttk.Button(
            self.button_frame, text="Start Experiment", 
            command=self._start_experiment
        )
        self.start_button.pack(side="left", padx=(0, 10))
        
        self.pause_button = ttk.Button(
            self.button_frame, text="Pause", 
            command=self._toggle_pause, state="disabled"
        )
        self.pause_button.pack(side="left", padx=(0, 10))
        
        self.cancel_button = ttk.Button(
            self.button_frame, text="Cancel", 
            command=self._cancel_experiment, state="disabled"
        )
        self.cancel_button.pack(side="left")
    
    def _build_plot_frame(self):
        """Build plot frame"""
        self.plot_frame = ttk.LabelFrame(self.main_frame, text="Real-Time Plot")
        self.plot_frame.grid(row=0, column=3, rowspan=9, padx=10, pady=10, sticky="nsew")
        
        self.plot_manager = PlotManager(self.plot_frame)
    
    def _build_tracker_frame(self):
        """Build experiment tracker frame"""
        self.tracker_frame = tk.Frame(self.experiment_tab)
        self.tracker_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

        self.tracker = ExperimentTracker(
            self.tracker_frame,
            get_device_name=lambda: self.device_name_var.get(),
            frequencies=self.param_options["frequencies"],
            duties=self.param_options["duties"],
            temperatures=self.param_options["temperatures"],
            voltages=self.param_options["voltages"],
            configs=self.param_options["configurations"],
        )
        self.tracker.grid(row=0, column=0, columnspan=3, sticky="nsew")

    def _build_configuration_tab(self):
        """Build runtime configuration editor tab"""

        instructions = (
            "Update the available parameter buttons without restarting the app. "
            "Changes apply immediately to the experiment controls and tracker."
        )
        ttk.Label(
            self.configuration_tab,
            text=instructions,
            wraplength=700,
            justify="left",
        ).grid(row=0, column=0, sticky="w", padx=10, pady=(10, 5))

        # Align the configuration editors in a balanced two-column grid
        editors_frame = ttk.Frame(self.configuration_tab)
        editors_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        editors_frame.grid_columnconfigure(0, weight=1, uniform="config_cols")
        editors_frame.grid_columnconfigure(1, weight=1, uniform="config_cols")
        editors_frame.grid_rowconfigure(0, weight=1)
        editors_frame.grid_rowconfigure(1, weight=1)

        editor_specs = [
            ("Configurations", "configurations", self._parse_string_entry, lambda v: v),
            ("Frequencies (Hz)", "frequencies", self._parse_int_entry, lambda v: freq_label(v)),
            ("Duty Cycles (%)", "duties", self._parse_int_entry, lambda v: f"{v}%"),
            ("Temperatures (°C)", "temperatures", self._parse_int_entry, lambda v: f"{v}°C"),
        ]

        for idx, (title, key, parser, label_factory) in enumerate(editor_specs, start=1):
            row = (idx - 1) // 2
            col = (idx - 1) % 2
            editor = ParameterListEditor(
                editors_frame,
                title,
                self.param_options[key],
                on_change=lambda opts, option_key=key: self._on_parameter_options_changed(option_key, opts),
                value_parser=parser,
                default_label_factory=label_factory,
                default_options=self.default_param_options[key],
            )
            editor.grid(row=row, column=col, sticky="nsew", padx=10, pady=10)
            self.parameter_editors[key] = editor

        voltages_editor = ParameterListEditor(
            self.configuration_tab,
            "Voltages (V)",
            self.param_options["voltages"],
            on_change=lambda opts: self._on_parameter_options_changed("voltages", opts),
            value_parser=self._parse_int_entry,
            default_label_factory=lambda v: f"{v}V",
            default_options=self.default_param_options["voltages"],
        )
        voltages_editor.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self.parameter_editors["voltages"] = voltages_editor

        self.instrument_config_editor = InstrumentConfigEditor(
            self.configuration_tab,
            initial_configs=self.instrument_configs,
            default_configs=self.default_instrument_configs,
            on_change=self._on_instrument_config_changed,
        )
        self.instrument_config_editor.grid(row=3, column=0, sticky="nsew", padx=10, pady=(0, 10))

        # Allow the editors to resize proportionally with the window
        self.configuration_tab.grid_columnconfigure(0, weight=1)
        self.configuration_tab.grid_rowconfigure(1, weight=1)
        self.configuration_tab.grid_rowconfigure(2, weight=1)
        self.configuration_tab.grid_rowconfigure(3, weight=1)

    @staticmethod
    def _parse_string_entry(value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Empty string")
        return cleaned

    @staticmethod
    def _parse_int_entry(value: str) -> int:
        cleaned = value.replace(",", "").strip()
        if not cleaned:
            raise ValueError("Empty value")
        try:
            if any(ch in cleaned.lower() for ch in ("e", ".")):
                return int(float(cleaned))
            return int(cleaned, 10)
        except ValueError as exc:
            raise ValueError("Invalid integer") from exc

    def _default_label_for(self, key: str, value: Any) -> str:
        if key == "frequencies":
            return freq_label(value)
        if key == "duties":
            return f"{value}%"
        if key == "temperatures":
            return f"{value}°C"
        if key == "voltages":
            return f"{value}V"
        return str(value)

    def _on_parameter_options_changed(
        self,
        key: str,
        options: List[Tuple[Any, str]],
    ) -> Optional[List[Tuple[Any, str]]]:
        """Validate and apply updated parameter options."""
        device_name = self.device_name_var.get().strip()
        if not device_name:
            messagebox.showerror(
                "Device Required",
                "Select a device before editing configuration options.",
            )
            return None

        sanitized: List[Tuple[Any, str]] = []
        seen = set()

        for value, label in options:
            if key == "configurations":
                normalized_value = str(value).strip()
                if not normalized_value:
                    messagebox.showerror("Validation Error", "Configuration value cannot be empty.")
                    return None
            else:
                try:
                    normalized_value = int(value)
                except (TypeError, ValueError):
                    messagebox.showerror("Validation Error", f"Invalid numeric value: {value}")
                    return None

            normalized_label = str(label).strip() if label else ""
            if not normalized_label:
                normalized_label = self._default_label_for(key, normalized_value)

            if normalized_value in seen:
                messagebox.showerror("Validation Error", "Duplicate values are not allowed.")
                return None
            seen.add(normalized_value)
            sanitized.append((normalized_value, normalized_label))

        if not sanitized:
            messagebox.showerror("Validation Error", "At least one option is required.")
            return None

        if key == "configurations":
            sanitized.sort(key=lambda item: item[1].lower())
        else:
            sanitized.sort(key=lambda item: item[0])

        self.param_options[key] = sanitized
        self._refresh_parameter_controls(key)
        self._persist_device_configuration(device_name)

        return sanitized

    def _refresh_parameter_controls(self, key: str):
        group_map = {
            "configurations": self.config_group,
            "frequencies": self.frequency_group,
            "duties": self.duty_group,
            "temperatures": self.temperature_group,
            "voltages": self.voltage_group,
        }

        if key in group_map and group_map[key] is not None:
            group_map[key].set_options(self.param_options[key])

        editors = getattr(self, "parameter_editors", {})
        if key in editors:
            editors[key].set_options(self.param_options[key])

        tracker_kwargs: Dict[str, List[Tuple[Any, str]]] = {}
        if hasattr(self, "tracker"):
            if key == "configurations":
                tracker_kwargs["configs"] = self.param_options[key]
            elif key == "frequencies":
                tracker_kwargs["frequencies"] = self.param_options[key]
            elif key == "duties":
                tracker_kwargs["duties"] = self.param_options[key]
            elif key == "temperatures":
                tracker_kwargs["temperatures"] = self.param_options[key]
            elif key == "voltages":
                tracker_kwargs["voltages"] = self.param_options[key]

            if tracker_kwargs:
                self.tracker.update_parameter_space(**tracker_kwargs)

        if key == "voltages":
            self._update_duration_default()

        self._force_visual_update()

    def _get_option_values(self, key: str) -> List[Any]:
        return [value for value, _ in self.param_options.get(key, [])]

    def _get_default_option_value(self, key: str, fallback: Any) -> Any:
        options = self.param_options.get(key, [])
        return options[0][0] if options else fallback

    def _on_instrument_config_changed(
        self, configs: Dict[str, Dict[str, Any]]
    ) -> Optional[Dict[str, Dict[str, Any]]]:
        sanitized: Dict[str, Dict[str, Any]] = {}

        for name, settings in configs.items():
            ip_raw = str(settings.get("ip", "")).strip()
            port_raw = settings.get("port")

            if not ip_raw:
                messagebox.showerror("Validation Error", f"IP address is required for {name}.")
                return None

            try:
                port_int = int(port_raw)
            except (TypeError, ValueError):
                messagebox.showerror("Validation Error", f"Invalid port for {name}.")
                return None

            if not (0 < port_int <= 65535):
                messagebox.showerror("Validation Error", f"Port out of range for {name}.")
                return None

            sanitized[name] = {"ip": ip_raw, "port": port_int}

        config.INSTRUMENTS.clear()
        config.INSTRUMENTS.update(copy.deepcopy(sanitized))
        self.instrument_configs = copy.deepcopy(sanitized)

        if hasattr(self, "status_bar"):
            self.status_bar.set_message("Instrument configuration updated.")

        return copy.deepcopy(self.instrument_configs)

    def _configure_grid_weights(self):
        """Configure grid weights for proper resizing"""
        # Root window
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Notebook tabs
        self.experiment_tab.grid_rowconfigure(0, weight=1)
        self.experiment_tab.grid_rowconfigure(1, weight=1)
        self.experiment_tab.grid_columnconfigure(0, weight=1)

        self.configuration_tab.grid_rowconfigure(0, weight=0)
        self.configuration_tab.grid_rowconfigure(1, weight=1)
        self.configuration_tab.grid_rowconfigure(2, weight=1)
        self.configuration_tab.grid_rowconfigure(3, weight=1)
        self.configuration_tab.grid_rowconfigure(4, weight=0)
        for col in range(2):
            self.configuration_tab.grid_columnconfigure(col, weight=1)

        # Main frame columns
        for col in range(3):
            self.main_frame.grid_columnconfigure(col, weight=0)
        self.main_frame.grid_columnconfigure(3, weight=1)

        # Main frame rows (allow plot to expand)
        self.main_frame.grid_rowconfigure(0, weight=1)
        for r in range(1, 9):
            self.main_frame.grid_rowconfigure(r, weight=0)

        # Tracker frame sizing
        self.tracker_frame.grid_rowconfigure(0, weight=1)
        for i in range(3):
            self.tracker_frame.grid_columnconfigure(i, weight=1)
    
    def _setup_callbacks(self):
        # Param changes
        self.config_var.trace_add("write", self._on_param_change)
        self.frequency_var.trace_add("write", self._on_param_change)
        self.duty_var.trace_add("write", self._on_param_change)
        self.temperature_var.trace_add("write", self._on_temperature_change)
        self.voltage_var.trace_add("write", self._on_voltage_change)

        # Device name updates (NEW)
        self.device_dropdown.bind("<KeyRelease>", lambda e: self.tracker.update_tracker())
        self.device_dropdown.bind("<<ComboboxSelected>>", lambda e: self.tracker.update_tracker())
    
    # Fixed _on_param_change to reset the applied frequency when parameters change
    def _on_param_change(self, *args):
        self.status_bar.set_message("Ready.")
        # Reset applied frequency when parameters change
        self.wavegen_controller.last_applied_freq = None
        self._update_confirm_button_color()

    def _on_voltage_change(self, *args):
        self._update_duration_default()
        self.status_bar.set_message("Ready.")
        self._update_confirm_button_color()

    def _on_temperature_change(self, *args):
        self.status_bar.set_message("Ready.")
        self._update_confirm_button_color()

    def _get_cached_or_default_duration(self):
        """Get cached duration or default for current voltage"""
        if hasattr(self, "_cached_duration"):
            return self._cached_duration
        voltage = self.voltage_var.get()
        return VOLTAGE_DEFAULT_MINUTES.get(voltage, 1)
    
    def _update_duration_default(self):
        """Update duration entry when voltage changes"""
        voltage = self.voltage_var.get()
        default_min = VOLTAGE_DEFAULT_MINUTES.get(voltage, 1)
        self.duration_entry.delete(0, tk.END)
        self.duration_entry.insert(0, str(default_min))

    def _sanitize_device_name(self, raw_name: str) -> str:
        return sanitize_device_name(raw_name)

    def _canonical_config(self, value: str) -> str:
        for canonical, label in CONFIGURATIONS:
            if value == canonical or value == label:
                return canonical
        return value

    def _find_prior_tuned_frequency(
        self,
        device: str,
        temp: int,
        freq: int,
        volt: int,
        duty: int,
        config: str,
    ):
        if not device:
            return None

        config_canonical = self._canonical_config(config)
        search_order = [(temp, config_canonical)]

        for cfg_value, _ in CONFIGURATIONS:
            cfg_canonical = self._canonical_config(cfg_value)
            if cfg_canonical != config_canonical:
                search_order.append((temp, cfg_canonical))

        if temp != 25:
            search_order.append((25, config_canonical))
            for cfg_value, _ in CONFIGURATIONS:
                cfg_canonical = self._canonical_config(cfg_value)
                if cfg_canonical != config_canonical:
                    search_order.append((25, cfg_canonical))

        seen = set()
        for temp_candidate, config_candidate in search_order:
            key = (temp_candidate, config_candidate)
            if key in seen:
                continue
            seen.add(key)

            path = generate_filename(
                device,
                temp_candidate,
                freq,
                volt,
                config_candidate,
                duty,
                ensure_dirs=False,
            )
            freq_val = self._load_tuned_frequency_from_csv(path)
            if freq_val is not None:
                return freq_val, config_candidate, temp_candidate, path

        return None

    def _get_tuning_candidate(self) -> Optional[Tuple[float, str, float, str]]:
        device = self._sanitize_device_name(self.device_name_var.get())
        freq = self.frequency_var.get()
        duty = self.duty_var.get()
        volt = self.voltage_var.get()
        temp = self.temperature_var.get()

        candidate = self._find_prior_tuned_frequency(
            device,
            temp,
            freq,
            volt,
            duty,
            self.config_var.get(),
        )

        if not candidate:
            return None

        freq_val, source_config, source_temp, path = candidate
        last_applied = self.wavegen_controller.last_applied_freq
        if last_applied is not None and abs(last_applied - freq_val) <= 1:
            return None
        return freq_val, source_config, source_temp, path

    def _tuning_available(self):
        return self._get_tuning_candidate() is not None

    def _format_frequency_display(self, freq_hz: float) -> str:
        """Return a human-friendly frequency label for UI elements."""
        freq = float(freq_hz)
        abs_freq = abs(freq)
        if abs_freq >= 1_000_000:
            return f"{freq / 1_000_000:.2f} MHz".rstrip("0").rstrip(".")
        if abs_freq >= 1_000:
            return f"{freq / 1_000:.1f} kHz".rstrip("0").rstrip(".")
        return f"{freq:.0f} Hz"

    def _update_confirm_button_color(self):
        """Update confirm button color based on state"""
        # First check if there are unconfirmed changes
        if self.wavegen_controller.has_changes():
            # Changes pending - show red
            self.confirm_button.config(bg="#e53935", fg="white", activebackground="#e53935")
        # Then check if tuning is available (only if no changes pending)
        elif self._tuning_available():
            # No changes but tuning available - show yellow
            self.confirm_button.config(bg="#FBC02D", fg="black", activebackground="#FBC02D")
        else:
            # No changes and no tuning available - show green
            self.confirm_button.config(bg="#43a047", fg="white", activebackground="#43a047")
        self._update_autotune_button_state()

    def _update_autotune_button_state(self):
        if not hasattr(self, "autotune_button"):
            return
        if getattr(self, "_autotune_running", False):
            return

        candidate = self._get_tuning_candidate()
        if candidate:
            freq_val, source_config, source_temp, _ = candidate
            freq_lbl = self._format_frequency_display(freq_val)
            text = f"Autotun: {freq_lbl}"
            self._set_autotune_button_style(
                text=text,
                state="normal",
                bg="#43a047",
                fg="white",
                active_bg="#388e3c",
                active_fg="white",
            )
        else:
            self._set_autotune_button_style(
                text="Autotune Unavailable",
                state="disabled",
                bg="#bdbdbd",
                fg="white",
                active_bg="#bdbdbd",
                active_fg="white",
            )

    def _set_autotune_button_style(
        self,
        *,
        text: str,
        state: str,
        bg: str,
        fg: str,
        active_bg: str,
        active_fg: str,
    ):
        if not hasattr(self, "autotune_button"):
            return
        self.autotune_button.config(
            text=text,
            state=state,
            bg=bg,
            fg=fg,
            activebackground=active_bg,
            activeforeground=active_fg,
            width=AUTOTUNE_BUTTON_WIDTH,
            height=AUTOTUNE_BUTTON_HEIGHT,
            borderless=1,
            highlightthickness=1,
        )

    def _confirm_high_risk_change(self, new_voltage: int, new_duty: int) -> bool:
        warnings: List[str] = []
        previous_voltage = self._last_confirmed_voltage
        if previous_voltage is not None and new_voltage != previous_voltage:
            if previous_voltage >= 400 and new_voltage <= 200:
                warnings.append(f"Voltage drop from {previous_voltage} V to {new_voltage} V.")

        previous_duty = self.wavegen_controller.last_confirmed_duty
        if previous_duty is not None and new_duty != previous_duty:
            warnings.append(f"Duty cycle change from {previous_duty}% to {new_duty}%.")

        if not warnings:
            return True

        message_lines = [
            "The following high-risk changes were detected:",
            "",
            *[f"- {item}" for item in warnings],
            "",
            "These changes may damage the device. Proceed?"
        ]
        response = messagebox.askyesno(
            "Confirm High-Risk Change",
            "\n".join(message_lines),
            icon="warning",
        )
        return bool(response)

    def _confirm_wavegen_changes(self, *, apply_tuned_immediately: bool = True) -> bool:
        """Confirm wavegen changes and apply prior tuning if available"""
        tuning_applied = False
        applied_freq = None
        config = self.config_var.get()
        new_voltage = self.voltage_var.get()
        new_duty = self.duty_var.get()

        if not self._confirm_high_risk_change(new_voltage, new_duty):
            return False

        skip_tuning = getattr(self, "_autotune_voltage_prompt_pending", False)
        tuning_candidate = None if skip_tuning else self._get_tuning_candidate()

        if tuning_candidate:
            freq_val, source_config, source_temp, _ = tuning_candidate
            if apply_tuned_immediately:
                config_canonical = self._canonical_config(config)
                try:
                    query_scpi("SDG6022X", f"C1:BSWV FRQ,{freq_val}")
                    if config_canonical == "Dual Conduction":
                        query_scpi("SDG6022X", f"C2:BSWV FRQ,{freq_val}")
                    NotificationManager.show_temporary_popup(
                        self,
                        f"Tuned frequency {int(freq_val)} Hz applied\n({source_config} @ {int(source_temp)}°C)",
                        duration_ms=2000
                    )
                    self.status_bar.set_message(
                        f"Tuned frequency {int(freq_val)} Hz applied from {source_config} @ {int(source_temp)}°C"
                    )
                    tuning_applied = True
                    applied_freq = freq_val
                except Exception as e:
                    messagebox.showerror("Wavegen Error", f"Failed to set tuned frequency: {e}")
            else:
                self.status_bar.set_message(
                    f"Tuned frequency {int(freq_val)} Hz ready (source: {source_config} @ {int(source_temp)}°C)"
                )
        elif skip_tuning:
            applied_freq = self.wavegen_controller.last_applied_freq

        # Confirm other wavegen parameters, passing the applied frequency if any
        if self.wavegen_controller.confirm_changes(applied_freq=applied_freq):
            if not tuning_applied:
                self.status_bar.set_message("Wavegen parameters confirmed.")
            
            # After confirming, we need to re-evaluate the button color
            # Since we just confirmed, there should be no changes pending
            # The button should be green unless we're at a temperature where tuning is available
            # but hasn't been applied yet (which shouldn't happen after we just applied it)
            
            # Force button to green after successful confirmation
            self.confirm_button.config(bg="#43a047", fg="white", activebackground="#43a047")
            
            # Schedule visual update
            self.after(10, self._force_visual_update)
            self._autotune_voltage_prompt_pending = False
            self._last_confirmed_voltage = new_voltage
            return True
        return False

    def _start_autotune_sequence(self):
        self._autotune_voltage_prompt_pending = False
        if getattr(self, "_autotune_running", False):
            return

        tuning_candidate = self._get_tuning_candidate()
        if not tuning_candidate:
            messagebox.showinfo("Autotune", "No tuned frequency is available for the current settings.")
            self._update_autotune_button_state()
            return

        freq_val, source_config, source_temp, _ = tuning_candidate

        if not self._confirm_wavegen_changes(apply_tuned_immediately=False):
            self._update_autotune_button_state()
            return

        self._autotune_running = True
        self._set_autotune_button_style(
            text="Autotuning...",
            state="disabled",
            bg="#1976D2",
            fg="white",
            active_bg="#1976D2",
            active_fg="white",
        )
        self.status_bar.set_message(f"Autotuning to {int(freq_val)} Hz...")

        config_canonical = self._canonical_config(self.config_var.get())
        worker = threading.Thread(
            target=self._run_autotune_ramp,
            args=(freq_val, source_config, source_temp, config_canonical),
            daemon=True,
        )
        worker.start()

    def _run_autotune_ramp(self, target_freq: float, source_config: str, source_temp: float, config_canonical: str):
        try:
            start_freq = self._perform_frequency_ramp(target_freq, config_canonical)
        except Exception as exc:
            self.after(0, lambda: self._on_autotune_failure(exc))
            return

        self.after(
            0,
            lambda: self._on_autotune_success(
                target_freq=target_freq,
                start_freq=start_freq,
                source_config=source_config,
                source_temp=source_temp,
            ),
        )

    def _perform_frequency_ramp(self, target_freq: float, config_canonical: str):
        start_freq = (
            self.wavegen_controller.last_applied_freq
            or self.wavegen_controller.last_confirmed_freq
            or self.frequency_var.get()
        )

        live_freq = self._read_current_wavegen_frequency()
        if live_freq is not None:
            start_freq = live_freq

        start_freq = float(start_freq)
        target_freq = float(target_freq)

        if abs(start_freq - target_freq) <= 1:
            self._apply_wavegen_frequency(target_freq, config_canonical)
            return start_freq

        step = AUTOTUNE_STEP_HZ if target_freq > start_freq else -AUTOTUNE_STEP_HZ
        current_freq = start_freq

        while True:
            next_freq = current_freq + step
            if (step > 0 and next_freq >= target_freq) or (step < 0 and next_freq <= target_freq):
                next_freq = target_freq

            self._apply_wavegen_frequency(next_freq, config_canonical)

            if next_freq == target_freq:
                break

            current_freq = next_freq
            time.sleep(AUTOTUNE_STEP_DELAY)
        return start_freq

    def _apply_wavegen_frequency(self, freq_hz: float, config_canonical: str):
        freq_value = int(round(freq_hz))
        query_scpi("SDG6022X", f"C1:BSWV FRQ,{freq_value}")
        if config_canonical == "Dual Conduction":
            query_scpi("SDG6022X", f"C2:BSWV FRQ,{freq_value}")

    def _read_current_wavegen_frequency(self) -> Optional[float]:
        try:
            raw = get_wavegen_frequency()
            if raw is None:
                return None
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _suggest_voltage_increase(self, start_freq: float, target_freq: float) -> str:
        current_voltage = self.voltage_var.get()
        voltage_values = sorted([value for value, _ in self.param_options.get("voltages", [])])
        next_voltage = next((value for value in voltage_values if value > current_voltage), None)
        if next_voltage is not None:
            return (
                f"The switching frequency increased from {int(start_freq)} Hz to {int(target_freq)} Hz.\n\n"
                f"Please raise the DC supply from {current_voltage} V to at least {next_voltage} V "
                "to maintain peak voltage levels."
            )
        return (
            f"The switching frequency increased from {int(start_freq)} Hz to {int(target_freq)} Hz.\n\n"
            "Please raise the DC supply voltage if possible to compensate for the reduced peak voltage."
        )

    def _on_autotune_success(self, target_freq: float, start_freq: Optional[float], source_config: str, source_temp: float):
        self._autotune_running = False
        self.wavegen_controller.last_applied_freq = target_freq
        message = f"Tuned frequency {int(target_freq)} Hz applied from {source_config} @ {int(source_temp)}°C"
        self.status_bar.set_message(message)
        NotificationManager.show_temporary_popup(
            self,
            f"Tuned frequency {int(target_freq)} Hz applied\n({source_config} @ {int(source_temp)}°C)",
            duration_ms=2000,
        )
        if start_freq is not None and target_freq > start_freq:
            suggestion = self._suggest_voltage_increase(start_freq, target_freq)
            messagebox.showinfo(
                "Increase DC Voltage",
                suggestion,
            )
            self.status_bar.set_message("Please increase the DC voltage before proceeding.")
            self._autotune_voltage_prompt_pending = True
        else:
            self._autotune_voltage_prompt_pending = False
        self._update_confirm_button_color()

    def _on_autotune_failure(self, error: Exception):
        self._autotune_running = False
        self.wavegen_controller.last_applied_freq = None
        messagebox.showerror("Autotune Error", f"Autotune failed: {error}")
        self.status_bar.set_message("Autotune failed.")
        self._update_confirm_button_color()

    def _toggle_auto_sequence(self):
        button = getattr(self, "auto_sequence_button", None)
        if self._auto_sequence_active:
            self._auto_sequence_cancel.set()
            if button:
                button.config(text="Stopping...", state="disabled")
            self.status_bar.set_message("Stopping auto sequence...")
            return

        if self.experiment_runner.state != ExperimentState.IDLE:
            messagebox.showinfo("Auto Sequence", "Please wait for the current experiment to finish before starting the auto sequence.")
            return

        params = self._build_params_from_ui()
        if params is None:
            return

        plan = self._build_auto_sequence_plan()
        if not plan:
            messagebox.showwarning("Auto Sequence", "No parameter combinations are available for the auto sequence.")
            return

        proceed = messagebox.askyesno(
            "Start Auto Sequence",
            f"This will run {len(plan)} tests across configurations, duties, voltages, and temperatures.\n\n"
            "You will be prompted to adjust the DC voltage and temperature between tests.\n\n"
            "Continue?",
        )
        if not proceed:
            return

        self._auto_sequence_duration = params.duration_minutes
        self._auto_sequence_cancel = threading.Event()
        self._auto_sequence_active = True
        if button:
            button.config(text="Stop Auto Sequence", state="normal")
        self.status_bar.set_message("Auto sequence starting...")

        self._auto_sequence_thread = threading.Thread(
            target=self._run_auto_sequence,
            args=(plan,),
            daemon=True,
        )
        self._auto_sequence_thread.start()

    def _build_auto_sequence_plan(self) -> List[Tuple[str, int, int, int]]:
        configs = [value for value, _ in self.param_options.get("configurations", [])]
        duties = [value for value, _ in self.param_options.get("duties", [])]
        voltages = [value for value, _ in self.param_options.get("voltages", [])]
        temperatures = [value for value, _ in self.param_options.get("temperatures", [])]

        plan: List[Tuple[str, int, int, int]] = []
        for temp in temperatures:
            for config in configs:
                for duty in duties:
                    for voltage in voltages:
                        if not self._test_already_completed(config, self.frequency_var.get(), duty, voltage, temp):
                            plan.append((config, duty, voltage, temp))
        return plan

    def _test_already_completed(self, config: str, freq: int, duty: int, voltage: int, temp: int) -> bool:
        device = self.device_name_var.get().strip()
        if not device:
            return False
        safe_device = "".join(c for c in device if c.isalnum() or c in (' ', '_', '-')).rstrip()
        freq_str = freq_label(freq)
        duty_folder = DEVICE_DATA_ROOT / safe_device / freq_str / str(duty)
        filename = f"{safe_device}_{config}_{int(temp)}C_{freq_str}_{int(voltage)}V_{duty}duty.csv"
        file_path = duty_folder / filename
        return file_path.is_file()

    def _run_auto_sequence(self, plan: List[Tuple[str, int, int, int]]):
        previous_temp: Optional[int] = None
        previous_voltage: Optional[int] = None
        total = len(plan)
        completed = 0
        success = True

        try:
            for index, (config, duty, voltage, temp) in enumerate(plan, start=1):
                if self._auto_sequence_cancel.is_set():
                    success = False
                    break

                self._call_on_ui_thread(lambda: self._select_sequence_parameters(config, duty, voltage, temp))
                self._call_on_ui_thread(
                    lambda idx=index, tot=total, cfg=config, d=duty, v=voltage, t=temp: self.status_bar.set_message(
                        f"Auto sequence step {idx}/{tot}: {cfg}, {d}% duty, {v}V, {t}°C"
                    )
                )

                if previous_temp is None or temp != previous_temp:
                    temp_message = (
                        f"Set the chamber temperature to {temp}°C."
                        if previous_temp is None
                        else (
                            f"Raise the chamber temperature from {previous_temp}°C to {temp}°C."
                            if temp > previous_temp
                            else f"Lower the chamber temperature from {previous_temp}°C to {temp}°C."
                        )
                    )
                    if not self._prompt_user_adjustment("Temperature Adjustment", temp_message):
                        success = False
                        break
                    previous_temp = temp
                    if self._auto_sequence_cancel.is_set():
                        success = False
                        break

                need_voltage_prompt = False
                voltage_message = ""
                if previous_voltage is None or voltage != previous_voltage:
                    need_voltage_prompt = True
                    voltage_message = (
                        f"Set the DC supply to {voltage} V."
                        if previous_voltage is None
                        else (
                            f"Raise the DC supply from {previous_voltage} V to {voltage} V."
                            if voltage > previous_voltage
                            else f"Lower the DC supply from {previous_voltage} V to {voltage} V."
                        )
                    )

                candidate = self._call_on_ui_thread(self._get_tuning_candidate)

                if not self._call_on_ui_thread(lambda: self._confirm_wavegen_changes(apply_tuned_immediately=False)):
                    success = False
                    break

                if candidate and not self._auto_sequence_cancel.is_set():
                    target_freq, _, _, _ = candidate
                    config_canonical = self._call_on_ui_thread(lambda: self._canonical_config(self.config_var.get()))
                    self._perform_frequency_ramp(target_freq, config_canonical)
                    self._call_on_ui_thread(
                        lambda tf=target_freq: setattr(self.wavegen_controller, "last_applied_freq", tf)
                    )
                    self._call_on_ui_thread(self._update_confirm_button_color)

                if need_voltage_prompt:
                    if not self._prompt_user_adjustment("Voltage Adjustment", voltage_message):
                        success = False
                        break
                    previous_voltage = voltage
                    if self._auto_sequence_cancel.is_set():
                        success = False
                        break

                params = self._call_on_ui_thread(
                    lambda: ExperimentParams(
                        device_name=self.device_name_var.get().strip(),
                        config=self.config_var.get(),
                        frequency=self.frequency_var.get(),
                        duty=self.duty_var.get(),
                        temperature=self.temperature_var.get(),
                        voltage=self.voltage_var.get(),
                        duration_minutes=self._auto_sequence_duration,
                    )
                )

                self._call_on_ui_thread(lambda p=params: self._launch_experiment(p))

                if not self._wait_for_experiment_completion():
                    success = False
                    break

                completed += 1
                self._call_on_ui_thread(
                    lambda done=completed, tot=total: self.status_bar.set_message(
                        f"Auto sequence progress: {done}/{tot} tests complete."
                    )
                )

                if self._auto_sequence_cancel.is_set():
                    success = False
                    break

        except Exception as exc:
            success = False
            self._call_on_ui_thread(lambda: messagebox.showerror("Auto Sequence Error", str(exc)))
        finally:
            if self._auto_sequence_cancel.is_set():
                self._call_on_ui_thread(lambda: self.status_bar.set_message("Auto sequence cancelled."))
            elif success:
                self._call_on_ui_thread(lambda: self.status_bar.set_message("Auto sequence complete."))
            else:
                self._call_on_ui_thread(lambda: self.status_bar.set_message("Auto sequence stopped."))
            self.after(0, self._finish_auto_sequence_ui)

    def _finish_auto_sequence_ui(self):
        button = getattr(self, "auto_sequence_button", None)
        self._auto_sequence_active = False
        if button:
            button.config(text="Start Auto Sequence", state="normal")
        self._auto_sequence_thread = None
        self._auto_sequence_cancel.clear()

    def _select_sequence_parameters(self, config: str, duty: int, voltage: int, temp: int):
        self.config_var.set(config)
        self.duty_var.set(duty)
        self.voltage_var.set(voltage)
        self.temperature_var.set(temp)
        self._force_visual_update()

    def _prompt_user_adjustment(self, title: str, instructions: str) -> bool:
        def _prompt():
            return messagebox.askokcancel(
                title,
                f"{instructions}\n\nClick OK when ready or Cancel to stop the auto sequence.",
            )

        response = self._call_on_ui_thread(_prompt)
        if not response:
            self._auto_sequence_cancel.set()
        return bool(response)

    def _wait_for_experiment_completion(self) -> bool:
        while True:
            if self._auto_sequence_cancel.is_set():
                self._call_on_ui_thread(self.experiment_runner.cancel_experiment)
            thread = self.experiment_runner.experiment_thread
            state = self.experiment_runner.state
            if state == ExperimentState.IDLE and (thread is None or not thread.is_alive()):
                return not self._auto_sequence_cancel.is_set()
            time.sleep(0.2)

    def _call_on_ui_thread(self, func: Callable[[], Any]):
        if threading.current_thread() is threading.main_thread():
            return func()

        result: Dict[str, Any] = {}
        done = threading.Event()

        def _wrapper():
            try:
                result["value"] = func()
            finally:
                done.set()

        self.after(0, _wrapper)
        done.wait()
        return result.get("value")

    def _force_visual_update(self):
        """Force visual update of all parameter groups"""
        # Re-trigger the highlighting for all groups
        for group in [self.frequency_group, self.temperature_group, 
                      self.duty_group, self.config_group, self.voltage_group]:
            group._highlight_selected()

        # Update the confirm button color
        self._update_confirm_button_color()

        # Force GUI refresh
        self.update_idletasks()

    def _build_params_from_ui(self) -> Optional[ExperimentParams]:
        device_name = self.device_name_var.get().strip()
        if not self.device_name_var.get().strip():
            messagebox.showerror("Input Error", "Please enter a device name.")
            return None

        if not self._ensure_drive_setup(device_name, notify_on_success=False):
            return None

        try:
            duration_minutes = float(self.duration_entry.get())
        except ValueError:
            messagebox.showerror("Input Error", "Please enter a valid duration.")
            return None

        return ExperimentParams(
            device_name=device_name,
            config=self.config_var.get(),
            frequency=self.frequency_var.get(),
            duty=self.duty_var.get(),
            temperature=self.temperature_var.get(),
            voltage=self.voltage_var.get(),
            duration_minutes=duration_minutes,
        )

    def _launch_experiment(self, params: ExperimentParams):
        self._set_experiment_ui_state(running=True)
        self.status_bar.set_message("Experiment running...")
        self.experiment_runner.start_experiment(params)

    def _start_experiment(self):
        """Start experiment"""
        params = self._build_params_from_ui()
        if params is None:
            return

        self._launch_experiment(params)
    
    def _toggle_pause(self):
        """Toggle experiment pause state"""
        self.experiment_runner.pause_experiment()
        
        # Update button text
        if self.experiment_runner.state == ExperimentState.PAUSED:
            self.pause_button.config(text="Resume")
        else:
            self.pause_button.config(text="Pause")
    
    def _cancel_experiment(self):
        """Cancel running experiment"""
        self.experiment_runner.cancel_experiment()
        self._set_experiment_ui_state(running=False)
    
    def _set_experiment_ui_state(self, running: bool):
        """Set UI state for experiment running/stopped"""
        if running:
            # Change background color to indicate running
            self.configure(bg="#FFF176")  # Light yellow
            self.main_frame.configure(bg="#FFF176")
            self.tracker_frame.configure(bg="#FFF176")
            
            # Update button states
            self.start_button.config(state="disabled")
            self.pause_button.config(state="normal", text="Pause")
            self.cancel_button.config(state="normal")
        else:
            # Reset background color
            self.configure(bg="SystemButtonFace")
            self.main_frame.configure(bg="SystemButtonFace")
            self.tracker_frame.configure(bg="SystemButtonFace")
            
            # Update button states
            self.start_button.config(state="normal")
            self.pause_button.config(state="disabled", text="Pause")
            self.cancel_button.config(state="disabled")
    
    def _update_current_display(self, current: float):
        """Update current display label"""
        self.last_current_value = current
        self.last_current_label.config(text=f"Last Current: {current:.6f} A")
    
    def _reset_ui_after_experiment(self):
        """Reset UI after experiment completion"""
        self._set_experiment_ui_state(running=False)
        self.tracker.update_tracker()
        self.status_bar.set_message("Experiment complete!")
    
    def _on_closing(self):
        """Handle window closing"""
        self._persist_device_configuration(self.device_name_var.get(), silent=True)
        self.param_manager.save_params()
        self.experiment_runner.cancel_experiment()
        self.after(500, self.destroy)

    def _load_tuned_frequency_from_csv(self, path: str):
        if not os.path.isfile(path):
            return None
        try:
            with open(path) as f:
                reader = csv.reader(f)
                for row in reader:
                    if row and row[0].strip() == "FINAL_READINGS" and len(row) >= 5:
                        return float(row[4])
        except Exception as e:
            print(f"Error reading '{path}': {e}")
        return None


def main():
    """Main entry point"""
    try:
        app = GaNExperimentGUI()
        app.mainloop()
    except Exception as e:
        print(f"Application error: {e}")
        messagebox.showerror("Application Error", f"An error occurred: {e}")

if __name__ == "__main__":
    main()
