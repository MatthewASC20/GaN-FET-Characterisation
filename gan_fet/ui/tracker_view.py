"""Completion tracker: done/not-done matrix per temperature, driven by the
database (v1 scanned the CSV file tree)."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, List, Optional, Tuple

from gan_fet.core.models import MatrixPoint, freq_label
from gan_fet.storage.db import Database


class TrackerView(tk.Frame):
    def __init__(
        self,
        master,
        db: Database,
        get_device_name: Callable[[], str],
        frequencies: List[Tuple[int, str]],
        duties: List[Tuple[int, str]],
        temperatures: List[Tuple[int, str]],
        voltages: List[Tuple[int, str]],
        configs: List[Tuple[str, str]],
        on_delete_run: Optional[Callable[[MatrixPoint], None]] = None,
        **kwargs,
    ):
        super().__init__(master, **kwargs)
        self.db = db
        self.get_device_name = get_device_name
        self.frequencies = list(frequencies)
        self.duties = list(duties)
        self.temperatures = list(temperatures)
        self.voltages = list(voltages)
        self.configs = list(configs)
        self.on_delete_run = on_delete_run

        self.selected_freq = tk.IntVar(
            value=self.frequencies[0][0] if self.frequencies else 0
        )
        self.tables: list[ttk.Treeview] = []
        self.title_labels: list[ttk.Label] = []
        self.table_frames: list[ttk.Frame] = []

        self._build_frequency_selector()
        self.build_tables()
        self.refresh()

    # -- layout -----------------------------------------------------------

    def _build_frequency_selector(self) -> None:
        self.freq_dropdown = ttk.Combobox(
            self,
            values=[label for _, label in self.frequencies],
            state="readonly",
            width=10,
        )
        if self.frequencies:
            self.freq_dropdown.current(0)
        self.freq_dropdown.grid(row=0, column=0, sticky="w", padx=10, pady=(8, 4))
        self.freq_dropdown.bind("<<ComboboxSelected>>", self._on_freq_selected)

    def _on_freq_selected(self, _event=None) -> None:
        selected_label = self.freq_dropdown.get()
        for value, label in self.frequencies:
            if label == selected_label:
                self.selected_freq.set(value)
                break
        self.refresh()

    def build_tables(self) -> None:
        for frame in self.table_frames:
            frame.destroy()
        self.tables = []
        self.title_labels = []
        self.table_frames = []

        for i, (temp_val, temp_label) in enumerate(self.temperatures):
            frame = ttk.Frame(self)
            frame.grid(row=1, column=i, sticky="nsew", padx=12, pady=6)
            self.table_frames.append(frame)

            title = ttk.Label(frame, text=temp_label, font=("TkDefaultFont", 11, "bold"))
            title.pack()
            self.title_labels.append(title)

            tree = ttk.Treeview(
                frame, columns=("config", "duty", "volt", "done"), show="headings",
                height=12,
            )
            for col, text, width in (
                ("config", "Configuration", 120),
                ("duty", "Duty", 70),
                ("volt", "Voltage", 80),
                ("done", "Done", 50),
            ):
                tree.heading(col, text=text)
                tree.column(col, width=width, anchor="center")
            tree.pack(fill="both", expand=True)
            for button in ("<Button-2>", "<Button-3>"):
                tree.bind(
                    button,
                    lambda event, t=tree, temp=temp_val: self._context_menu(event, t, temp),
                )
            self.tables.append(tree)

        for i in range(max(1, len(self.temperatures))):
            self.grid_columnconfigure(i, weight=1)
        self.grid_rowconfigure(1, weight=1)

    # -- data -------------------------------------------------------------

    def refresh(self) -> None:
        device = self.get_device_name().strip()
        freq_val = self.selected_freq.get()
        done = (
            self.db.completed_points(device, freq_val) if device else set()
        )
        freq_display = freq_label(freq_val) if freq_val else ""

        for idx, (temp_val, temp_label) in enumerate(self.temperatures):
            if idx >= len(self.tables):
                break
            tree = self.tables[idx]
            self.title_labels[idx].config(
                text=f"{device or '—'} · {temp_label} @ {freq_display}"
            )
            tree.delete(*tree.get_children())
            rows = []
            for config_val, config_label in self.configs:
                for duty_val, duty_label in self.duties:
                    for volt_val, volt_label in self.voltages:
                        is_done = (config_val, duty_val, volt_val, temp_val) in done
                        rows.append(
                            (config_label, duty_label, volt_label,
                             "✅" if is_done else "❌")
                        )
            for row in sorted(rows):
                tree.insert("", "end", values=row)

    def update_parameter_space(
        self,
        *,
        frequencies=None, duties=None, temperatures=None,
        voltages=None, configs=None,
    ) -> None:
        if frequencies is not None:
            self.frequencies = list(frequencies)
            values = [v for v, _ in self.frequencies]
            self.freq_dropdown["values"] = [label for _, label in self.frequencies]
            if values and self.selected_freq.get() not in values:
                self.selected_freq.set(values[0])
                self.freq_dropdown.current(0)
        if duties is not None:
            self.duties = list(duties)
        if temperatures is not None:
            self.temperatures = list(temperatures)
        if voltages is not None:
            self.voltages = list(voltages)
        if configs is not None:
            self.configs = list(configs)
        self.build_tables()
        self.refresh()

    # -- context menu -------------------------------------------------------

    def _point_from_row(self, tree: ttk.Treeview, item: str, temp: int) -> Optional[MatrixPoint]:
        values = tree.item(item, "values")
        if not values:
            return None
        config_label, duty_label, volt_label, _ = values
        try:
            config_val = next(v for v, l in self.configs if l == config_label)
            duty_val = next(v for v, l in self.duties if l == duty_label)
            volt_val = next(v for v, l in self.voltages if l == volt_label)
        except StopIteration:
            return None
        return MatrixPoint(
            device_name=self.get_device_name().strip(),
            config=config_val,
            frequency_hz=self.selected_freq.get(),
            duty_pct=int(duty_val),
            temperature_c=int(temp),
            voltage_v=int(volt_val),
        )

    def _context_menu(self, event, tree: ttk.Treeview, temp: int) -> None:
        item = tree.identify_row(event.y)
        if not item:
            return
        tree.selection_set(item)
        point = self._point_from_row(tree, item, temp)
        if point is None:
            return
        run = self.db.find_run(point)

        menu = tk.Menu(tree, tearoff=0)
        if run is not None:
            menu.add_command(label="Plot Experiment", command=lambda: self._plot_run(run.id))
            menu.add_command(label="Delete Test", command=lambda: self._delete_run(point))
        else:
            menu.add_command(label="(no data)", state="disabled")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _delete_run(self, point: MatrixPoint) -> None:
        run = self.db.find_run(point)
        if run is None:
            return
        if not messagebox.askyesno(
            "Delete Test",
            f"Delete the stored result for:\n\n{point.describe()}\n\n"
            "This removes the run and all its samples from the database.",
        ):
            return
        self.db.delete_run(run.id)
        if self.on_delete_run is not None:
            self.on_delete_run(point)
        self.refresh()

    def _plot_run(self, run_id: int) -> None:
        samples = self.db.samples_for_run(run_id)
        if not samples:
            messagebox.showinfo("Plot Experiment", "This run has no samples recorded.")
            return
        run = self.db.get_run(run_id)

        import matplotlib.pyplot as plt

        timestamps = [row[0] for row in samples]
        currents = [row[1] for row in samples]
        plt.figure(figsize=(8, 5))
        plt.plot(timestamps, currents)
        plt.title(run.point.describe() if run else f"Run {run_id}")
        plt.xlabel("Timestamp")
        plt.ylabel("Current (A)")
        step = max(1, len(timestamps) // 8)
        plt.xticks(range(0, len(timestamps), step),
                   [timestamps[i] for i in range(0, len(timestamps), step)],
                   rotation=45)
        plt.tight_layout()
        plt.show()
