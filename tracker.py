import os
import tkinter as tk
from tkinter import ttk, messagebox
import pandas as pd
from data_utils import freq_label
import matplotlib.pyplot as plt
from sheet_utils import clear_test_in_sheet
from typing import List, Tuple, Optional

class ExperimentTracker(tk.Frame):
    def __init__(self, master, get_device_name, frequencies, duties, temperatures, voltages, configs, **kwargs):
        super().__init__(master, **kwargs)
        self.get_device_name = get_device_name
        self.frequencies = list(frequencies)
        self.duties = list(duties)
        self.temperatures = list(temperatures)
        self.voltages = list(voltages)
        self.configs = list(configs)

        # Frequency selector variable and dropdown
        initial_freq = self.frequencies[0][0] if self.frequencies else 0
        self.selected_freq = tk.IntVar(value=initial_freq)
        self.tables = []  # Will hold a table for each temperature
        self.title_labels = []  # Track temperature headers so we can refresh text

        self._build_frequency_selector()
        self.build_tables()
        self.update_tracker()

    def _build_frequency_selector(self):
        """Create a frequency dropdown at the top."""
        self.freq_dropdown = ttk.Combobox(
            self,
            values=[label for _, label in self.frequencies],
            state="readonly",
            width=10
        )
        if self.frequencies:
            self.freq_dropdown.current(0)
        self.freq_dropdown.grid(row=0, column=0, sticky="w", padx=10, pady=(8, 4))
        self.freq_dropdown.bind("<<ComboboxSelected>>", self._on_freq_selected)
        self._refresh_frequency_dropdown()

    def _frequency_label_from_value(self, freq_val: int) -> str | None:
        for value, label in self.frequencies:
            if value == freq_val:
                return label
        return freq_label(freq_val) if freq_val else None

    def _refresh_frequency_dropdown(self):
        labels = [label for _, label in self.frequencies]
        self.freq_dropdown["values"] = labels
        if not labels:
            self.freq_dropdown.set("")
            self.selected_freq.set(0)
            return

        current_val = self.selected_freq.get()
        available_values = [value for value, _ in self.frequencies]
        if current_val not in available_values:
            self.selected_freq.set(available_values[0])
            current_val = available_values[0]

        current_label = self._frequency_label_from_value(current_val)
        if current_label:
            self.freq_dropdown.set(current_label)

    def _on_freq_selected(self, event=None):
        """Change all tables to display for the selected frequency."""
        # Update selected_freq from dropdown
        selected_label = self.freq_dropdown.get()
        for freq_val, label in self.frequencies:
            if label == selected_label:
                self.selected_freq.set(freq_val)
                break
        self.update_tracker()

    def build_tables(self):
        """Builds three horizontal tables, one for each temperature."""
        if hasattr(self, "table_frames"):
            for frame in self.table_frames:
                frame.destroy()
        self.tables = []
        self.title_labels = []
        self.table_frames = []

        for i, (temp_val, temp_label) in enumerate(self.temperatures):
            frame = ttk.Frame(self)
            frame.grid(row=1, column=i, sticky="nsew", padx=12, pady=6)  # Row fixed at 1, vary columns
            self.table_frames.append(frame)

            title = ttk.Label(
                frame,
                text=f"{self.get_device_name()} - {temp_label} @ {self._frequency_label_from_value(self.selected_freq.get())}",
                font=("Arial", 11, "bold")
            )
            title.pack()
            self.title_labels.append(title)
            tree = ttk.Treeview(
                frame,
                columns=("config", "duty", "volt", "done"),
                show="headings"
            )
            tree.heading("config", text="Configuration")
            tree.heading("duty", text="Duty Cycle")
            tree.heading("volt", text="Voltage (V)")
            tree.heading("done", text="Done")
            tree.column("config", width=120, anchor="center")
            tree.column("duty", width=80, anchor="center")
            tree.column("volt", width=90, anchor="center")
            tree.column("done", width=54, anchor="center")
            tree.pack(fill="both", expand=True)
            tree.bind("<Button-3>", lambda event, t=tree, n=temp_val: self.show_context_menu(event, t, self.selected_freq.get(), n))
            tree.bind("<Button-2>", lambda event, t=tree, n=temp_val: self.show_context_menu(event, t, self.selected_freq.get(), n))
            self.tables.append(tree)

        # Make columns expand evenly
        for i in range(len(self.temperatures)):
            self.grid_columnconfigure(i, weight=1)
        self.grid_rowconfigure(1, weight=1)

    def show_context_menu(self, event, tree, freq_val, temp_val):
        item = tree.identify_row(event.y)
        if not item:
            return
        tree.selection_set(item)
        menu = tk.Menu(tree, tearoff=0)
        menu.add_command(label="Delete Test", command=lambda: self.delete_test(tree, item, freq_val, temp_val))
        done = tree.item(item, "values")[3]  # now 4 columns, 'done' is 4th
        if done == "✅":
            menu.add_command(label="Plot Experiment", command=lambda: self.plot_experiment(tree, item, freq_val, temp_val))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def delete_test(self, tree, item, freq_val, temp_val):
        values = tree.item(item, "values")
        config_label, duty_label, volt_label, _ = values
        config_val = next(val for val, label in self.configs if label == config_label)
        duty_val = next(val for val, label in self.duties if label == duty_label)
        temp = int(temp_val)
        volt_val = int(volt_label.replace("V", ""))
        device = self.get_device_name().strip()
        safe_device = "".join(c for c in device if c.isalnum() or c in (' ', '_', '-')).rstrip()
        freq_str = freq_label(freq_val)
        duty_folder = os.path.join("Device Data", safe_device, freq_str, str(duty_val))
        filename = f"{safe_device}_{config_val}_{temp}C_{freq_str}_{volt_val}V_{duty_val}duty.csv"
        file_path = os.path.join(duty_folder, filename)
        if os.path.isfile(file_path):
            try:
                os.remove(file_path)
                messagebox.showinfo("Deleted", f"Deleted: {file_path}")
            except Exception as e:
                messagebox.showerror("Error", f"Could not delete file: {e}")
        else:
            messagebox.showwarning("Not found", f"File not found: {file_path}")

        # --- Clear Google Sheet values ---
        try:
            cleared = clear_test_in_sheet(
                device_name=device,
                freq=freq_val,
                temp=temp,
                config=config_val,
                duty=duty_val,
                voltage=volt_val
            )
            if cleared:
                print("Cleared test values from Google Sheet.")
        except Exception as e:
            print(f"Error clearing Google Sheet values: {e}")

        self.update_tracker()

    def plot_experiment(self, tree, item, freq_val, temp_val):
        values = tree.item(item, "values")
        config_label, duty_label, volt_label, _ = values
        config_val = next(val for val, label in self.configs if label == config_label)
        duty_val = next(val for val, label in self.duties if label == duty_label)
        temp = int(temp_val)
        volt_val = int(volt_label.replace("V", ""))
        device = self.get_device_name().strip()
        safe_device = "".join(c for c in device if c.isalnum() or c in (' ', '_', '-')).rstrip()
        freq_str = freq_label(freq_val)
        duty_folder = os.path.join("Device Data", safe_device, freq_str, str(duty_val))
        filename = f"{safe_device}_{config_val}_{temp}C_{freq_str}_{volt_val}V_{duty_val}duty.csv"
        file_path = os.path.join(duty_folder, filename)

        if not os.path.isfile(file_path):
            messagebox.showerror("Error", f"File not found: {file_path}")
            return
        try:
            df = pd.read_csv(file_path)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to read CSV: {e}")
            return

        # Plot Current (A) vs. Timestamp
        plt.figure(figsize=(8, 5))
        plt.plot(df['Timestamp'], df['Current (A)'])
        plt.title(f"Experiment Plot: {os.path.basename(file_path)}")
        plt.xlabel("Timestamp")
        plt.ylabel("Current (A)")
        plt.xticks(rotation=45)
        plt.tight_layout()
        plt.show()

    def update_tracker(self):
        if not hasattr(self, "table_frames") or not self.table_frames:
            self.build_tables()
        device = self.get_device_name().strip()
        if not device:
            return
        safe_device = "".join(c for c in device if c.isalnum() or c in (' ', '_', '-')).rstrip()
        freq_val = self.selected_freq.get()
        freq_str = freq_label(freq_val)
        freq_display = self._frequency_label_from_value(freq_val) or freq_str
        for idx, (temp_val, temp_label) in enumerate(self.temperatures):
            tree = self.tables[idx]
            if idx < len(self.title_labels):
                self.title_labels[idx].config(
                    text=f"{device} - {temp_label} @ {freq_display}"
                )
            for row in tree.get_children():
                tree.delete(row)
            freq_folder = os.path.join("Device Data", safe_device, freq_str)
            rows = []
            for config_val, config_label in self.configs:
                for duty_val, duty_label in self.duties:
                    for volt_val, volt_label in self.voltages:
                        duty_folder = os.path.join(freq_folder, str(duty_val))
                        filename = f"{safe_device}_{config_val}_{int(temp_val)}C_{freq_str}_{int(volt_val)}V_{duty_val}duty.csv"
                        file_path = os.path.join(duty_folder, filename)
                        done = "✅" if os.path.isfile(file_path) else "❌"
                        rows.append((config_label, duty_label, volt_label, done))
            sorted_rows = sorted(rows, key=lambda x: (x[0], x[1], x[2]))
            for row in sorted_rows:
                tree.insert("", "end", values=row)

    def update_parameter_space(
        self,
        *,
        frequencies: Optional[List[Tuple[int, str]]] = None,
        duties: Optional[List[Tuple[int, str]]] = None,
        temperatures: Optional[List[Tuple[int, str]]] = None,
        voltages: Optional[List[Tuple[int, str]]] = None,
        configs: Optional[List[Tuple[str, str]]] = None,
    ):
        if frequencies is not None:
            self.frequencies = list(frequencies)
            if not self.frequencies:
                self.selected_freq.set(0)
            elif self.selected_freq.get() not in [value for value, _ in self.frequencies]:
                self.selected_freq.set(self.frequencies[0][0])
            self._refresh_frequency_dropdown()

        if duties is not None:
            self.duties = list(duties)
        if temperatures is not None:
            self.temperatures = list(temperatures)
        if voltages is not None:
            self.voltages = list(voltages)
        if configs is not None:
            self.configs = list(configs)

        self.build_tables()
        self.update_tracker()
