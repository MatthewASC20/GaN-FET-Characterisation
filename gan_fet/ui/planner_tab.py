"""Test Planner tab: multi-parameter selection, test matrix generation,
time estimation, completion tracking, CSV exporting, and automated sequence execution.
"""

from __future__ import annotations

import csv
import logging
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Dict, List, Optional, Tuple

from gan_fet.core.models import MatrixPoint, freq_label, sanitize_device_name
from gan_fet.core.sequence import PlanSummary, build_matrix_plan
from gan_fet.storage.db import Database
from gan_fet.ui.tracker_view import TrackerView
from gan_fet.ui.widgets import parse_positive_duration

log = logging.getLogger(__name__)


class MultiSelectBox(ttk.LabelFrame):
    """Scrollable multi-selection listbox with Select All and Clear buttons."""

    def __init__(
        self,
        master,
        title: str,
        options: List[Tuple[Any, str]],
        on_selection_changed: Optional[Callable[[], None]] = None,
    ):
        super().__init__(master, text=title)
        self.on_selection_changed = on_selection_changed
        self.options: List[Tuple[Any, str]] = []

        self._build_widgets()
        self.set_options(options)

    def _build_widgets(self) -> None:
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=2, pady=(2, 4))
        ttk.Button(toolbar, text="All", width=5, command=self.select_all).pack(side="left", padx=2)
        ttk.Button(toolbar, text="None", width=5, command=self.clear_selection).pack(side="left", padx=2)

        list_frame = ttk.Frame(self)
        list_frame.pack(fill="both", expand=True, padx=2, pady=2)

        self.scrollbar = ttk.Scrollbar(list_frame, orient="vertical")
        self.listbox = tk.Listbox(
            list_frame,
            selectmode=tk.MULTIPLE,
            exportselection=False,
            height=6,
            yscrollcommand=self.scrollbar.set,
        )
        self.scrollbar.config(command=self.listbox.yview)
        self.scrollbar.pack(side="right", fill="y")
        self.listbox.pack(side="left", fill="both", expand=True)

        self.listbox.bind("<<ListboxSelect>>", self._on_select)

    def set_options(self, options: List[Tuple[Any, str]], select_all_by_default: bool = True) -> None:
        self.options = list(options)
        self.listbox.delete(0, tk.END)
        for _val, label in self.options:
            self.listbox.insert(tk.END, label)

        if select_all_by_default:
            self.select_all()

    def select_all(self) -> None:
        self.listbox.select_set(0, tk.END)
        if self.on_selection_changed:
            self.on_selection_changed()

    def clear_selection(self) -> None:
        self.listbox.selection_clear(0, tk.END)
        if self.on_selection_changed:
            self.on_selection_changed()

    def get_selected_values(self) -> List[Any]:
        indices = self.listbox.curselection()
        return [self.options[i][0] for i in indices if i < len(self.options)]

    def _on_select(self, _event) -> None:
        if self.on_selection_changed:
            self.on_selection_changed()


class PlannerTab(ttk.Frame):
    """Tab widget for planning multi-parameter automated test runs."""

    def __init__(
        self,
        master,
        db: Database,
        get_device_name: Callable[[], str],
        on_start_sequence: Callable[[PlanSummary, float, bool], None],
        on_apply_plan: Callable[[Optional[PlanSummary]], bool],
        on_delete_run: Optional[Callable[[MatrixPoint], None]] = None,
        can_delete_run: Optional[Callable[[], bool]] = None,
    ):
        super().__init__(master)
        self.db = db
        self.get_device_name = get_device_name
        self.on_start_sequence = on_start_sequence
        self.on_apply_plan = on_apply_plan
        self.on_delete_run = on_delete_run
        self.can_delete_run = can_delete_run

        self.param_options: Dict[str, List[Tuple[Any, str]]] = {
            "frequencies": [],
            "configurations": [],
            "duties": [],
            "voltages": [],
            "temperatures": [],
        }
        self.select_boxes: Dict[str, MultiSelectBox] = {}
        self.current_plan: Optional[PlanSummary] = None

        self._building_ui = True
        self._build_ui()
        self._building_ui = False

    def _build_ui(self) -> None:
        # Top banner & description
        top_frame = ttk.Frame(self)
        top_frame.pack(fill="x", padx=10, pady=(10, 5))

        ttk.Label(
            top_frame,
            text="Automated Test Matrix Planner",
            font=("TkDefaultFont", 12, "bold"),
        ).pack(side="left")

        ttk.Label(
            top_frame,
            text="— Select target parameters to generate and inspect the test execution plan.",
            font=("TkDefaultFont", 10, "italic"),
        ).pack(side="left", padx=(10, 0))

        # Parameter selection area
        selection_frame = ttk.LabelFrame(self, text="Parameter Matrix Selection")
        selection_frame.pack(fill="x", padx=10, pady=5)

        box_specs = [
            ("Frequencies", "frequencies"),
            ("Configurations", "configurations"),
            ("Duty Cycles", "duties"),
            ("Voltages", "voltages"),
            ("Temperatures", "temperatures"),
        ]

        grid_frame = ttk.Frame(selection_frame)
        grid_frame.pack(fill="x", padx=5, pady=5)
        for col, (title, key) in enumerate(box_specs):
            grid_frame.grid_columnconfigure(col, weight=1, uniform="param_col")
            box = MultiSelectBox(
                grid_frame,
                title,
                self.param_options[key],
                on_selection_changed=self._on_param_selection_changed,
            )
            box.grid(row=0, column=col, sticky="nsew", padx=4, pady=4)
            self.select_boxes[key] = box

        # Options & Control bar
        ctrl_frame = ttk.Frame(selection_frame)
        ctrl_frame.pack(fill="x", padx=5, pady=(0, 5))

        self.include_completed_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            ctrl_frame,
            text="Include Completed Runs (Re-test)",
            variable=self.include_completed_var,
            command=self._on_param_selection_changed,
        ).pack(side="left", padx=10)

        ttk.Label(ctrl_frame, text="Duration / test (min):").pack(side="left", padx=(15, 2))
        self.duration_entry = ttk.Entry(ctrl_frame, width=8)
        self.duration_entry.insert(0, "1.0")
        self.duration_entry.pack(side="left", padx=2)
        self.duration_entry.bind(
            "<FocusOut>", lambda _event: self.generate_plan(show_errors=False)
        )

        self.find_zvs_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            ctrl_frame,
            text="Find ZVS before each run",
            variable=self.find_zvs_var,
        ).pack(side="left", padx=15)

        # The plan is already rebuilt live on every selection change, so
        # there is nothing for a "generate" button to do. Applying is the
        # action that matters: it makes this plan the one the Experiment tab
        # queue shows and the one the sequence runs.
        self.generate_btn = ttk.Button(
            ctrl_frame,
            text="Apply Test Plan",
            command=self._apply_plan,
        )
        self.generate_btn.pack(side="right", padx=10)

        # Plan Summary Dashboard
        summary_frame = ttk.LabelFrame(self, text="Plan Summary & Estimates")
        summary_frame.pack(fill="x", padx=10, pady=5)

        self.lbl_total = ttk.Label(summary_frame, text="Total Matrix Points: —", font=("TkDefaultFont", 9, "bold"))
        self.lbl_total.pack(side="left", padx=15, pady=5)

        self.lbl_completed = ttk.Label(summary_frame, text="Already Completed: —", foreground="green")
        self.lbl_completed.pack(side="left", padx=15, pady=5)

        self.lbl_pending = ttk.Label(summary_frame, text="Pending Execution: —", foreground="#1565C0")
        self.lbl_pending.pack(side="left", padx=15, pady=5)

        self.lbl_duration = ttk.Label(summary_frame, text="Est. Runtime: —")
        self.lbl_duration.pack(side="left", padx=15, pady=5)

        self.lbl_temp_prompts = ttk.Label(summary_frame, text="Thermal Chamber Prompts: —")
        self.lbl_temp_prompts.pack(side="left", padx=15, pady=5)

        # Tabbed view for Completion Matrix Grid and Scheduled Sequence Plan
        planner_notebook = ttk.Notebook(self)
        planner_notebook.pack(fill="both", expand=True, padx=10, pady=5)

        # Tab 1: Completion Matrix Grid (TrackerView)
        matrix_tab = ttk.Frame(planner_notebook)
        planner_notebook.add(matrix_tab, text="Test Matrix Completion Grid")

        self.tracker = TrackerView(
            matrix_tab,
            self.db,
            get_device_name=self.get_device_name,
            frequencies=self.param_options["frequencies"],
            duties=self.param_options["duties"],
            temperatures=self.param_options["temperatures"],
            voltages=self.param_options["voltages"],
            configs=self.param_options["configurations"],
            on_delete_run=self.on_delete_run,
            can_delete_run=self.can_delete_run,
        )
        self.tracker.pack(fill="both", expand=True)

        # Tab 2: Detailed Test Execution Sequence Listing
        plan_tab = ttk.Frame(planner_notebook)
        planner_notebook.add(plan_tab, text="Sequence Execution Plan Listing")

        table_frame = ttk.LabelFrame(plan_tab, text="Test Execution Point Listing")
        table_frame.pack(fill="both", expand=True, padx=5, pady=5)

        columns = ("step", "temp", "config", "freq", "duty", "voltage", "status", "duration")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=10)

        self.tree.heading("step", text="#")
        self.tree.heading("temp", text="Temp (°C)")
        self.tree.heading("config", text="Configuration")
        self.tree.heading("freq", text="Frequency")
        self.tree.heading("duty", text="Duty (%)")
        self.tree.heading("voltage", text="Voltage (V)")
        self.tree.heading("status", text="Status")
        self.tree.heading("duration", text="Est. Time (min)")

        self.tree.column("step", width=50, anchor="center")
        self.tree.column("temp", width=90, anchor="center")
        self.tree.column("config", width=140, anchor="w")
        self.tree.column("freq", width=110, anchor="center")
        self.tree.column("duty", width=80, anchor="center")
        self.tree.column("voltage", width=90, anchor="center")
        self.tree.column("status", width=110, anchor="center")
        self.tree.column("duration", width=110, anchor="center")

        tree_scroll_y = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        tree_scroll_x = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=tree_scroll_y.set, xscrollcommand=tree_scroll_x.set)

        tree_scroll_y.pack(side="right", fill="y")
        tree_scroll_x.pack(side="bottom", fill="x")
        self.tree.pack(side="left", fill="both", expand=True)

        self.tree.tag_configure("completed", foreground="#666666", background="#f0f0f0")
        self.tree.tag_configure("pending", foreground="#0d47a1")

        # Action Buttons
        actions_frame = ttk.Frame(plan_tab)
        actions_frame.pack(fill="x", padx=5, pady=(5, 10))

        self.run_btn = ttk.Button(
            actions_frame,
            text="Run Planned Sequence",
            command=self._start_sequence,
            state="disabled",
        )
        self.run_btn.pack(side="left", padx=(0, 10))

        self.export_btn = ttk.Button(
            actions_frame,
            text="Export Plan to CSV",
            command=self._export_csv,
            state="disabled",
        )
        self.export_btn.pack(side="left")

    def update_parameter_options(
        self,
        frequencies: List[Tuple[Any, str]],
        configurations: List[Tuple[Any, str]],
        duties: List[Tuple[Any, str]],
        voltages: List[Tuple[Any, str]],
        temperatures: List[Tuple[Any, str]],
    ) -> None:
        """Update available options in the multi-select boxes."""
        self.param_options["frequencies"] = list(frequencies)
        self.param_options["configurations"] = list(configurations)
        self.param_options["duties"] = list(duties)
        self.param_options["voltages"] = list(voltages)
        self.param_options["temperatures"] = list(temperatures)

        self._building_ui = True
        try:
            for key, box in self.select_boxes.items():
                box.set_options(self.param_options[key], select_all_by_default=True)
        finally:
            self._building_ui = False

        self.tracker.update_parameter_space(
            frequencies=frequencies,
            duties=duties,
            temperatures=temperatures,
            voltages=voltages,
            configs=configurations,
        )

        self.generate_plan(show_errors=False)

    def _on_param_selection_changed(self) -> None:
        if getattr(self, "_building_ui", False):
            return
        self.generate_plan(show_errors=False)

    def generate_plan(self, *, show_errors: bool = True) -> Optional[PlanSummary]:
        """Compute the custom matrix test plan based on user selections."""
        device_name = self.get_device_name().strip()
        canonical_device = sanitize_device_name(device_name)
        if not device_name or canonical_device != device_name:
            self._reset_summary("No Device Selected")
            return None

        freqs = [int(v) for v in self.select_boxes["frequencies"].get_selected_values()]
        configs = [str(v) for v in self.select_boxes["configurations"].get_selected_values()]
        duties = [int(v) for v in self.select_boxes["duties"].get_selected_values()]
        voltages = [int(v) for v in self.select_boxes["voltages"].get_selected_values()]
        temps = [int(v) for v in self.select_boxes["temperatures"].get_selected_values()]

        if not (freqs and configs and duties and voltages and temps):
            self._reset_summary("Select at least one option per parameter")
            return None

        try:
            duration = parse_positive_duration(self.duration_entry.get())
        except (TypeError, ValueError) as exc:
            self._reset_summary("Invalid duration")
            if show_errors:
                messagebox.showerror(
                    "Input Error",
                    f"Please enter a finite, positive duration per test point.\n\n{exc}",
                    parent=self,
                )
            return None

        include_completed = self.include_completed_var.get()

        plan = build_matrix_plan(
            db=self.db,
            device_name=canonical_device,
            frequencies_hz=freqs,
            configs=configs,
            duties=duties,
            voltages=voltages,
            temperatures=temps,
            include_completed=include_completed,
            duration_minutes_per_point=duration,
        )

        self.current_plan = plan
        self._display_plan(plan, duration)
        return plan

    def _reset_summary(self, status_msg: str) -> None:
        self.current_plan = None
        self.lbl_total.config(text=f"Total Matrix Points: — ({status_msg})")
        self.lbl_completed.config(text="Already Completed: —")
        self.lbl_pending.config(text="Pending Execution: —")
        self.lbl_duration.config(text="Est. Runtime: —")
        self.lbl_temp_prompts.config(text="Thermal Chamber Prompts: —")

        self.tree.delete(*self.tree.get_children())
        self.run_btn.config(state="disabled")
        self.export_btn.config(state="disabled")

    def _display_plan(self, plan: PlanSummary, duration: float) -> None:
        self.lbl_total.config(text=f"Total Matrix Points: {plan.total_count}")
        self.lbl_completed.config(text=f"Already Completed: {plan.completed_count}")
        self.lbl_pending.config(text=f"Pending Execution: {plan.pending_count}")

        # Format runtime (mins or hrs)
        total_mins = plan.estimated_duration_minutes
        if total_mins >= 60:
            hrs = total_mins / 60.0
            runtime_str = f"{total_mins:.1f} min ({hrs:.1f} hrs)"
        else:
            runtime_str = f"{total_mins:.1f} min"

        self.lbl_duration.config(text=f"Est. Runtime: {runtime_str}")
        self.lbl_temp_prompts.config(
            text=f"Thermal Chamber Prompts: {plan.temperature_changes} transition(s)"
        )

        # Clear and populate table
        self.tree.delete(*self.tree.get_children())
        for idx, item in enumerate(plan.points, start=1):
            pt = item.point
            status_str = "Completed" if item.is_completed else "Pending"
            tag = "completed" if item.is_completed else "pending"

            self.tree.insert(
                "",
                "end",
                values=(
                    idx,
                    f"{pt.temperature_c} °C",
                    pt.config,
                    freq_label(pt.frequency_hz),
                    f"{pt.duty_pct} %",
                    f"{pt.voltage_v} V",
                    status_str,
                    f"{duration:.1f}",
                ),
                tags=(tag,),
            )

        has_runnable_points = len(plan.points) > 0
        self.run_btn.config(state="normal" if has_runnable_points else "disabled")
        self.export_btn.config(state="normal" if has_runnable_points else "disabled")

    def _apply_plan(self) -> None:
        """Make the current plan the one the rig is working from."""
        plan = self.generate_plan(show_errors=True)
        self.on_apply_plan(plan)

    def _start_sequence(self) -> None:
        # Rebuild at click time so edits made after the last explicit
        # "Generate" action can never launch a stale matrix.
        plan = self.generate_plan(show_errors=True)
        if not plan or not plan.points:
            messagebox.showwarning("No Test Plan", "Please generate a test plan first.")
            return

        try:
            duration = parse_positive_duration(self.duration_entry.get())
        except (TypeError, ValueError) as exc:
            messagebox.showerror(
                "Input Error",
                f"Please enter a finite, positive duration per test point.\n\n{exc}",
                parent=self,
            )
            return

        find_zvs = self.find_zvs_var.get()
        # Apply first: starting switches to the Experiment tab, and its queue
        # has to describe the sequence that is about to run rather than a
        # matrix rebuilt from that tab's own selectors.
        self.on_apply_plan(plan)
        self.on_start_sequence(plan, duration, find_zvs)

    def _export_csv(self) -> None:
        if not self.current_plan or not self.current_plan.points:
            messagebox.showwarning("No Plan", "No generated test plan to export.")
            return

        device = self.get_device_name().strip() or "Device"
        default_filename = f"{sanitize_device_name(device)}_test_plan.csv"

        filepath = filedialog.asksaveasfilename(
            title="Export Test Plan CSV",
            initialfile=default_filename,
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not filepath:
            return

        try:
            with open(filepath, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "Step", "Device", "Temperature_C", "Config",
                    "Frequency_Hz", "Frequency_Label", "Duty_Pct",
                    "Voltage_V", "Status"
                ])
                for idx, item in enumerate(self.current_plan.points, start=1):
                    pt = item.point
                    writer.writerow([
                        idx,
                        pt.device_name,
                        pt.temperature_c,
                        pt.config,
                        pt.frequency_hz,
                        freq_label(pt.frequency_hz),
                        pt.duty_pct,
                        pt.voltage_v,
                        "Completed" if item.is_completed else "Pending",
                    ])
            messagebox.showinfo("Export Successful", f"Test plan successfully exported to:\n{filepath}")
        except Exception as exc:
            log.exception("failed to export test plan")
            messagebox.showerror("Export Error", f"Failed to export test plan: {exc}")
