"""Completion tracker: done/not-done matrix per temperature, driven by the
database (v1 scanned the CSV file tree)."""

from __future__ import annotations

import logging
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, List, Optional, Tuple

from gan_fet.core.models import MatrixPoint, RunRecord, freq_label
from gan_fet.storage.db import Database
from gan_fet.ui.plan_store import NO_PLAN_HEADING, applied_queue
from gan_fet.ui.plan_table import (
    PLAN_COLUMN_IDS,
    configure_plan_tree,
    fill_plan_tree,
)

log = logging.getLogger(__name__)


def _sample_plot_axis(
    samples: list[tuple],
) -> tuple[list[float] | list[str], str]:
    """Prefer lossless active elapsed time, falling back for legacy samples."""
    elapsed_values: list[float] = []
    for row in samples:
        if len(row) <= 6 or row[6] is None:
            break
        elapsed_values.append(float(row[6]))
    else:
        if elapsed_values:
            return elapsed_values, "Elapsed time (s)"

    return [str(row[0]) for row in samples], "Timestamp"


def format_run_details(run: RunRecord, sample_count: int) -> str:
    """Format the stored procedure outcome for operator review."""
    readings = run.readings

    def value(number, unit: str, digits: int = 4) -> str:
        return "—" if number is None else f"{number:.{digits}g} {unit}".strip()

    return "\n".join(
        (
            run.point.describe(),
            "",
            f"Status: {run.status} (attempt {run.attempt_no})",
            f"Samples: {sample_count}",
            f"Started: {run.started_at or '—'}",
            f"Completed: {run.completed_at or '—'}",
            f"Bus voltage: {value(run.bus_voltage_v, 'V')}",
            f"Tuned DC voltage: {value(run.tuned_voltage_v, 'V')}",
            f"Vin: {value(readings.vin, 'V')}",
            f"Iin: {value(readings.iin, 'A', 6)}",
            f"Switching frequency: {value(readings.fsw_hz, 'Hz')}",
            f"Irms: {value(readings.irms, 'A')}",
            f"Vds peak: {value(readings.vds_pk, 'V')}",
            f"Isw RMS: {value(readings.isw_rms, 'A')}",
            f"Scope capture: {run.screenshot_path or '—'}",
        )
    )


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
        can_delete_run: Optional[Callable[[], bool]] = None,
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
        self.can_delete_run = can_delete_run or (lambda: True)

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

    def follow_frequency(self, frequency_hz: Optional[int]) -> None:
        """Show the matrix for the frequency that was just measured.

        The completed grid reads this widget's own frequency selector, so a
        sequence working through a multi-frequency plan would tick off points
        the operator could not see. Following the run keeps "already
        completed" describing what just happened rather than whichever
        frequency was last chosen by hand.
        """
        if not frequency_hz:
            self.refresh()
            return
        try:
            if int(self.selected_freq.get()) != int(frequency_hz):
                self.selected_freq.set(int(frequency_hz))
        except (ValueError, tk.TclError):  # pragma: no cover - defensive
            pass
        self.refresh()

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
            if values:
                if self.selected_freq.get() not in values:
                    self.selected_freq.set(values[0])
                    self.freq_dropdown.current(0)
            else:
                self.selected_freq.set(0)
                self.freq_dropdown.set("")
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
            config_val = next(
                value for value, label in self.configs if label == config_label
            )
            duty_val = next(
                value for value, label in self.duties if label == duty_label
            )
            volt_val = next(
                value for value, label in self.voltages if label == volt_label
            )
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
            menu.add_command(
                label="View Run Details",
                command=lambda: self._show_run_details(run.id),
            )
            menu.add_command(label="Plot Experiment", command=lambda: self._plot_run(run.id))
            if run.screenshot_path:
                menu.add_command(
                    label="View Scope Screenshot",
                    command=lambda: self._open_screenshot(run.id),
                )
            menu.add_separator()
            menu.add_command(label="Delete Test", command=lambda: self._delete_run(point))
        else:
            menu.add_command(label="(no data)", state="disabled")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _show_run_details(self, run_id: int) -> None:
        run = self.db.get_run(run_id)
        if run is None:
            messagebox.showwarning(
                "Run Details", "The selected run no longer exists.", parent=self
            )
            return
        sample_count = len(self.db.samples_for_run(run_id))
        messagebox.showinfo(
            "Run Details",
            format_run_details(run, sample_count),
            parent=self,
        )

    def _open_screenshot(self, run_id: int) -> None:
        run = self.db.get_run(run_id)
        if run is None or not run.screenshot_path:
            messagebox.showinfo(
                "Scope Screenshot",
                "This run has no stored scope screenshot.",
                parent=self,
            )
            return
        screenshot = Path(run.screenshot_path).expanduser()
        if not screenshot.is_absolute():
            screenshot = self.db.path.parent / screenshot
        screenshot = screenshot.resolve()
        if not screenshot.is_file():
            messagebox.showwarning(
                "Scope Screenshot",
                f"The stored screenshot could not be found:\n{screenshot}",
                parent=self,
            )
            return
        if not webbrowser.open(screenshot.as_uri()):
            messagebox.showinfo(
                "Scope Screenshot",
                f"Open this image manually:\n{screenshot}",
                parent=self,
            )

    def _delete_run(self, point: MatrixPoint) -> None:
        if not self.can_delete_run():
            messagebox.showwarning(
                "Rig Busy",
                "Stored runs cannot be deleted while a rig operation is active.",
                parent=self,
            )
            return
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

        x_values, x_label = _sample_plot_axis(samples)
        figure, current_axis = plt.subplots(figsize=(9, 5.5))
        voltage_axis = current_axis.twinx()
        plotted = []
        for axis, label, index, colour in (
            (current_axis, "DC current", 1, "#1565c0"),
            (current_axis, "RMS current", 4, "#00897b"),
            (current_axis, "Switch RMS", 5, "#8e24aa"),
            (voltage_axis, "SMU voltage", 2, "#ef6c00"),
            (voltage_axis, "DC voltage", 3, "#c62828"),
        ):
            values = [row[index] for row in samples]
            if any(value is not None for value in values):
                (line,) = axis.plot(
                    x_values,
                    values,
                    label=label,
                    color=colour,
                    linewidth=1.6,
                )
                plotted.append(line)
        current_axis.set_title(run.point.describe() if run else f"Run {run_id}")
        current_axis.set_xlabel(x_label)
        current_axis.set_ylabel("Current (A)")
        voltage_axis.set_ylabel("Voltage (V)")
        current_axis.grid(True, alpha=0.3)
        if plotted:
            current_axis.legend(
                plotted,
                [str(line.get_label()) for line in plotted],
                loc="best",
            )
        if x_label == "Timestamp":
            step = max(1, len(x_values) // 8)
            positions = range(0, len(x_values), step)
            current_axis.set_xticks(
                positions,
                [str(x_values[i]) for i in positions],
                rotation=45,
            )
        figure.tight_layout()
        plt.show()


class UpNextView(ttk.LabelFrame):
    """The applied test plan, in the order it will run.

    Every point, scrollable, rendered exactly as the planner's own "Sequence
    Execution Plan Listing" renders it — same columns, same formatting, same
    greyed-out completed rows — because they are two views of one stored plan
    and any visible difference between them is a bug. See
    :mod:`gan_fet.ui.plan_table`.

    This is where the operator confirms that what they applied is what they
    meant, so a truncated five would answer the wrong question.
    """

    def __init__(
        self,
        master,
        db: Database,
        get_device_name: Callable[[], str],
        plan_store=None,
        **kwargs,
    ):
        super().__init__(master, text="Planned Tests", **kwargs)
        self.db = db
        self.get_device_name = get_device_name
        # When a plan has been applied, it is what runs, so it is what this
        # shows. Without one the live matrix drives the queue as before.
        self.plan_store = plan_store

        self._build_ui()

    def _build_ui(self) -> None:
        self.status_lbl = ttk.Label(
            self,
            text=NO_PLAN_HEADING,
            font=("TkDefaultFont", 9, "italic"),
            wraplength=900,
            justify="left",
        )
        self.status_lbl.pack(anchor="w", padx=10, pady=(5, 2))

        table_frame = ttk.Frame(self)
        table_frame.pack(fill="both", expand=True, padx=10, pady=(0, 5))

        self.tree = ttk.Treeview(
            table_frame, columns=PLAN_COLUMN_IDS, show="headings", height=10
        )
        scrollbar = ttk.Scrollbar(
            table_frame, orient="vertical", command=self.tree.yview
        )
        self.tree.configure(yscrollcommand=scrollbar.set)
        configure_plan_tree(self.tree)

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def refresh(self) -> None:
        """Show the applied plan, or say that there is none.

        There is no computed fallback. The queue used to build a plan from the
        cross-product of every configured option, which nobody had asked for
        and which looked enough like a real plan to make "Clear Plan" seem
        broken. Now it shows what was applied, or nothing.
        """
        self.tree.delete(*self.tree.get_children())
        applied = getattr(self.plan_store, "applied", None)
        if applied is None:
            self.status_lbl.config(text=NO_PLAN_HEADING)
            return
        self._show_applied(applied)


    def _show_applied(self, applied) -> None:
        """Render the outstanding points of the applied plan.

        No run-table lookup: the stored plan records its own progress.
        Subtracting the run table here as well would hide the one case the
        planner's re-test option exists for — points deliberately queued again
        despite already having runs.

        The selected device is passed so a plan belonging to another part says
        so instead of quietly listing work that will be refused.
        """
        try:
            contents = applied_queue(applied, self.get_device_name().strip())
        except Exception:
            # A blank table and an unchanged heading is what this used to look
            # like, which reads as "the plan is empty" rather than "something
            # went wrong". Say which it is.
            log.exception("Could not render the applied test plan")
            self.status_lbl.config(
                text=(
                    f"Applied plan ({applied.source}, {len(applied)} points) "
                    "could not be displayed — see the log. The plan is still "
                    "applied; Clear Plan returns to the parameter selections."
                )
            )
            return
        self.status_lbl.config(text=contents.heading)
        fill_plan_tree(self.tree, contents.rows)
