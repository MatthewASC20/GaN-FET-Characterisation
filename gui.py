import tkinter as tk
from tkinter import ttk, messagebox
from tkmacosx import Button as MacButton
from tracker import ExperimentTracker
from config import *
from instrument_utils import *
from data_utils import generate_filename, log_data_to_csv
from experiment import *
import matplotlib
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.pyplot as plt
from collections import deque
import threading
import os
import json
from sheet_utils import log_final_results_to_sheet
from typing import Callable, Optional, Dict, Any
from dataclasses import dataclass
from enum import Enum
import csv
from api import setup_device_in_drive 

matplotlib.use('TkAgg')

# Constants
PARAMS_FILE = "last_params.json"
PLOT_MAX_POINTS = 100
VOLTAGE_TOLERANCE = 2.0
DEFAULT_GEOMETRY = "1700x950"

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
        self.buttons = {}
        self.options = options  # Store options for reference

        label = ttk.Label(self, text=label_text)
        label.pack(side="left", padx=(0, 10))

        for value, text in options:
            button = MacButton(
                self, text=text, width=100, height=30, borderless=1,
                command=lambda v=value: self._on_button_click(v)
            )
            button.pack(side="left", padx=5)
            self.buttons[value] = button

        # Initial highlight
        self._highlight_selected()
        
        # Trace variable changes
        self.variable.trace_add("write", lambda *args: self.after(1, self._highlight_selected))

    def _on_button_click(self, value):
        self.variable.set(value)
        # Immediate visual feedback
        self._highlight_selected()

    def _highlight_selected(self):
        """Update button highlighting based on current variable value"""
        try:
            # Get the current value
            selected = self.variable.get()
            
            # Update each button's appearance
            for value, button in self.buttons.items():
                if value == selected:
                    # Selected button - blue
                    button.config(
                        bg="#2196F3", 
                        fg="white", 
                        activebackground="#1976D2",
                        activeforeground="white"
                    )
                else:
                    # Unselected button - gray
                    button.config(
                        bg="#E0E0E0", 
                        fg="black", 
                        activebackground="#D0D0D0",
                        activeforeground="black"
                    )
            
            # Force the frame to update
            self.update_idletasks()
            
        except Exception as e:
            print(f"Error in _highlight_selected: {e}")

    def refresh(self):
        """Public method to force refresh the visual state"""
        self._highlight_selected()



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
            # Apply changes based on what changed
            if new_config != self.last_confirmed_config:
                configure_wavegen(new_config, new_freq, new_duty)
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
                timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
                
                # Log data
                log_data_to_csv(filename, [
                    timestamp, params.device_name, params.temperature,
                    params.frequency, params.voltage, params.config,
                    params.duty, current
                ], header=header)
                
                # Update UI
                self.gui.after(0, lambda c=current: self.gui._update_current_display(c))
                self.gui.after(0, lambda c=current: self.gui.plot_manager.update_plot(c))
        
        # Capture final readings
        if self.state != ExperimentState.CANCELLED:
            self._capture_final_readings(filename, params)
        
        return True
    
    def _check_file_overwrite(self, filename: str) -> bool:
        """Check if file exists and confirm overwrite"""
        if os.path.isfile(filename):
            result = messagebox.askyesno(
                "Overwrite Confirmation",
                f"A result file for this test already exists:\n\n"
                f"{os.path.basename(filename)}\n\nDo you want to overwrite it?"
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
        try:
            peak_voltage = float(get_oscilloscope_peak_voltage())
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
            dc_voltage = float(get_multimeter_voltage())
            rms_current = float(get_oscilloscope_rms_current())
            isw_rms = float(get_oscilloscope_isw_rms())
            dc_current = self.gui.last_current_value
            measured_freq = float(get_wavegen_frequency())

            # Log to CSV
            log_data_to_csv(filename, [
                "FINAL_READINGS", dc_voltage, rms_current, dc_current,
                measured_freq, isw_rms
            ])

            # Log to Google Sheet
            success = log_final_results_to_sheet(
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
                isw=isw_rms
            )

            print("\n=== Final Instrument Readings ===")
            print(f"DC Voltage: {dc_voltage} V")
            print(f"RMS Current: {rms_current} A")
            print(f"DC Current: {dc_current} A")
            print(f"Frequency: {measured_freq} Hz")

            if params.config == "Dual Conduction":
                print(f"Isw RMS Current: {isw_rms} A")

            if success:
                print("Logged final readings to Google Sheet.")
            else:
                print("Failed to log final readings to Google Sheet.")

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
    
    def load_params(self):
        """Load parameters from file"""
        try:
            if os.path.isfile(PARAMS_FILE):
                with open(PARAMS_FILE, "r") as f:
                    params = json.load(f)
                
                self.gui.device_name_var.set(params.get("device_name", ""))
                self.gui.config_var.set(params.get("config", self.gui.config_var.get()))
                self.gui.frequency_var.set(int(params.get("frequency", self.gui.frequency_var.get())))
                self.gui.duty_var.set(int(params.get("duty", self.gui.duty_var.get())))
                self.gui.temperature_var.set(int(params.get("temperature", self.gui.temperature_var.get())))
                self.gui.voltage_var.set(int(params.get("voltage", self.gui.voltage_var.get())))
                
                duration = params.get("duration", VOLTAGE_DEFAULT_MINUTES.get(self.gui.voltage_var.get(), 1))
                self.gui._cached_duration = duration
                
        except Exception as e:
            print(f"Error loading parameters: {e}")
            self.gui._cached_duration = VOLTAGE_DEFAULT_MINUTES.get(self.gui.voltage_var.get(), 1)

class GaNExperimentGUI(tk.Tk):
    """Main GUI application class"""
    
    def __init__(self):
        super().__init__()
        self.title("GaN Device Test Runner")
        self.geometry(DEFAULT_GEOMETRY)
        self.protocol("WM_DELETE_WINDOW", self._on_closing)
        
        # Initialize variables
        self._init_variables()
        
        # Initialize managers
        self.param_manager = ParameterManager(self)
        self.wavegen_controller = WavegenController(self)
        self.experiment_runner = ExperimentRunner(self)
        
        # Load parameters and build UI
        self.param_manager.load_params()
        self._build_ui()
        self._setup_callbacks()
        
        # Initialize state
        self.last_current_value = None
        self._cached_duration = VOLTAGE_DEFAULT_MINUTES.get(self.voltage_var.get(), 1)
        
        # Update UI after loading    
    def _init_variables(self):
        """Initialize tkinter variables"""
        self.device_name_var = tk.StringVar(value="")
        self.config_var = tk.StringVar(value=CONFIGURATIONS[0][0])
        self.frequency_var = tk.IntVar(value=FREQUENCIES[0][0])
        self.duty_var = tk.IntVar(value=DUTIES[0][0])
        self.temperature_var = tk.IntVar(value=TEMPERATURES[0][0])
        self.voltage_var = tk.IntVar(value=VOLTAGES[0][0])
    
    def _build_ui(self):
        """Build the main UI"""
        # Main frame
        self.main_frame = tk.Frame(self)
        self.main_frame.grid(row=0, column=0, sticky="nsew")
        
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
        
        # Status bar
        self.status_bar = StatusBar(self)
        
        # Configure grid weights
        self._configure_grid_weights()
    
    def _get_known_device_names(self):
        base_path = "Device Data"
        if not os.path.isdir(base_path):
            return []
        return [f for f in os.listdir(base_path) if os.path.isdir(os.path.join(base_path, f))]


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
        self.device_dropdown.bind("<KeyRelease>", self._on_device_change)
        self.device_dropdown.bind("<<ComboboxSelected>>", self._on_device_change)

    def _on_device_change(self, *args):
        name = self.device_name_var.get().strip()
        if not name:
            return

        # Sync tracker UI
        self.tracker.update_tracker()

        # Check if it's a new device
        known_devices = self._get_known_device_names()
        if name not in known_devices:
            try:
                setup_device_in_drive(name)  # ⬅️ API call here!
                # Update dropdown with new entry
                self.device_dropdown["values"] = known_devices + [name]
                messagebox.showinfo("Folder Created", f"Drive setup created for: {name}")
            except Exception as e:
                messagebox.showerror("Drive Setup Failed", f"Google Drive setup failed:\n{e}")
    
    def _build_parameter_groups(self):
        """Build parameter selection groups"""
        self.frequency_group = ParamButtonGroup(
            self.main_frame, FREQUENCIES, self.frequency_var, "Frequency (Hz)"
        )
        self.frequency_group.grid(row=1, column=0, columnspan=3, pady=10, sticky="w")

        self.temperature_group = ParamButtonGroup(
            self.main_frame, TEMPERATURES, self.temperature_var, "Temperature (°C)"
        )
        self.temperature_group.grid(row=2, column=0, columnspan=3, pady=10, sticky="w")

        self.duty_group = ParamButtonGroup(
            self.main_frame, DUTIES, self.duty_var, "Duty Cycle"
        )
        self.duty_group.grid(row=3, column=0, columnspan=3, pady=10, sticky="w")

        self.config_group = ParamButtonGroup(
            self.main_frame, CONFIGURATIONS, self.config_var, "Configuration"
        )
        self.config_group.grid(row=4, column=0, columnspan=3, pady=10, sticky="w")

        self.voltage_group = ParamButtonGroup(
            self.main_frame, VOLTAGES, self.voltage_var, "Voltage (V)"
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
        self.last_current_label.grid(row=7, column=0, columnspan=2, sticky="w", padx=5, pady=5)
        
        self.confirm_button = MacButton(
            self.main_frame, text="Confirm Changes", 
            command=self._confirm_wavegen_changes,
            width=120, height=40, borderless=1, highlightthickness=1
        )
        self.confirm_button.grid(row=7, column=2, sticky="w", padx=5, pady=5)
    
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
        self.tracker_frame = tk.Frame(self)
        self.tracker_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=10)
        
        self.tracker = ExperimentTracker(
            self.tracker_frame,
            get_device_name=lambda: self.device_name_var.get(),
            frequencies=FREQUENCIES,
            duties=DUTIES,
            temperatures=TEMPERATURES,
            voltages=VOLTAGES,
            configs=CONFIGURATIONS
        )
        self.tracker.grid(row=0, column=0, columnspan=3, sticky="nsew")
    
    def _configure_grid_weights(self):
        """Configure grid weights for proper resizing"""
        # Main frame columns
        self.main_frame.grid_columnconfigure(0, weight=0)
        self.main_frame.grid_columnconfigure(1, weight=0)
        self.main_frame.grid_columnconfigure(2, weight=0)
        self.main_frame.grid_columnconfigure(3, weight=1)
        
        # Main frame rows
        for r in range(9):
            self.main_frame.grid_rowconfigure(r, weight=0)
        
        # Tracker frame
        self.tracker_frame.grid_rowconfigure(0, weight=1)
        for i in range(3):
            self.tracker_frame.grid_columnconfigure(i, weight=1)
        
        # Root window
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)
    
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
    
    def _tuning_available(self):
        device = "".join(c for c in self.device_name_var.get().strip()
                        if c.isalnum() or c in (' ', '_', '-'))
        freq = self.frequency_var.get()
        duty = self.duty_var.get()
        volt = self.voltage_var.get()
        config = self.config_var.get()
        temp = self.temperature_var.get()

        if temp == 25:
            return False

        def _get_canonical(val):
            for v, lbl in CONFIGURATIONS:
                if val == v or val == lbl:
                    return v
            return val

        config_canonical = _get_canonical(config)

        # Check same config at 25C
        path_primary = generate_filename(device, 25, freq, volt, config_canonical, duty)
        if os.path.isfile(path_primary):
            with open(path_primary, newline='') as f:
                reader = csv.reader(f)
                for row in reader:
                    if row and row[0].strip() == "FINAL_READINGS" and len(row) >= 5 and row[4]:
                        return True

        # Fallback: Dual Conduction at 25C if needed
        if config_canonical != "Dual Conduction":
            path_dual = generate_filename(device, 25, freq, volt, "Dual Conduction", duty)
            if os.path.isfile(path_dual):
                with open(path_dual, newline='') as f:
                    reader = csv.reader(f)
                    for row in reader:
                        if row and row[0].strip() == "FINAL_READINGS" and len(row) >= 5 and row[4]:
                            return True
        return False

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
            # Fixed _confirm_wavegen_changes method

    def _confirm_wavegen_changes(self):
        """Confirm wavegen changes and apply prior tuning if available"""
        tuning_applied = False
        applied_freq = None
        
        device = "".join(c for c in self.device_name_var.get().strip()
                        if c.isalnum() or c in (' ', '_', '-'))
        freq = self.frequency_var.get()
        duty = self.duty_var.get()
        volt = self.voltage_var.get()
        config = self.config_var.get()
        temp = self.temperature_var.get()

        # Check if tuning should be applied
        should_apply_tuning = temp != 25 and self._tuning_available()
        
        if should_apply_tuning:
            def _get_canonical(val):
                for v, lbl in CONFIGURATIONS:
                    if val == v or val == lbl:
                        return v
                return val

            config_canonical = _get_canonical(config)

            # Try same config at 25°C
            path_primary = generate_filename(device, 25, freq, volt, config_canonical, duty)
            freq_val = self._load_tuned_frequency_from_csv(path_primary)
            used_config = config_canonical
            used_file = path_primary

            # Fallback: Dual Conduction at 25°C
            if freq_val is None and config_canonical != "Dual Conduction":
                path_fallback = generate_filename(device, 25, freq, volt, "Dual Conduction", duty)
                freq_val = self._load_tuned_frequency_from_csv(path_fallback)
                used_config = "Dual Conduction"
                used_file = path_fallback

            # Apply frequency if found
            if freq_val:
                try:
                    query_scpi("SDG6022X", f"C1:BSWV FRQ,{freq_val}")
                    if config_canonical == "Dual Conduction":
                        query_scpi("SDG6022X", f"C2:BSWV FRQ,{freq_val}")
                    NotificationManager.show_temporary_popup(
                        self,
                        f"Tuned frequency {int(freq_val)} Hz applied\n({used_config} @ 25°C)",
                        duration_ms=2000
                    )
                    self.status_bar.set_message(f"Tuned frequency {int(freq_val)} Hz applied from '{os.path.basename(used_file)}'")
                    tuning_applied = True
                    applied_freq = freq_val
                except Exception as e:
                    messagebox.showerror("Wavegen Error", f"Failed to set tuned frequency: {e}")

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

    def _start_experiment(self):
        """Start experiment"""
        if not self.device_name_var.get().strip():
            messagebox.showerror("Input Error", "Please enter a device name.")
            return
        
        try:
            duration_minutes = float(self.duration_entry.get())
        except ValueError:
            messagebox.showerror("Input Error", "Please enter a valid duration.")
            return
        
        # Create experiment parameters
        params = ExperimentParams(
            device_name=self.device_name_var.get().strip(),
            config=self.config_var.get(),
            frequency=self.frequency_var.get(),
            duty=self.duty_var.get(),
            temperature=self.temperature_var.get(),
            voltage=self.voltage_var.get(),
            duration_minutes=duration_minutes
        )
        
        # Update UI state
        self._set_experiment_ui_state(running=True)
        self.status_bar.set_message("Experiment running...")
        
        # Start experiment
        self.experiment_runner.start_experiment(params)
    
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