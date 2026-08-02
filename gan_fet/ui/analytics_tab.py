"""Analytics & Power Loss Breakdown UI Tab.

Calculates and visualizes power losses (P_in = Vin * Iin) and loss decomposition:
- Conduction Loss = P_in(Dual Conduction) - P_in(Single Conduction)
- Switching Loss = P_in(Single Conduction) - P_in(Single Device)
- Parasitic / Baseline Loss = P_in(Single Device)
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Callable, Optional, TypedDict, cast

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from gan_fet.core.models import RunRecord, freq_label
from gan_fet.storage.db import Database
from gan_fet.storage import reports as reports_mod

log = logging.getLogger(__name__)


class LossBreakdown(TypedDict):
    p_dual: Optional[float]
    p_s_cond: Optional[float]
    p_s_dev: Optional[float]
    p_cond: Optional[float]
    p_sw: Optional[float]
    p_other: Optional[float]
    r_dson: Optional[float]


def compute_loss_breakdown(
    runs: list[RunRecord],
    frequency_hz: int,
    duty_pct: int,
    temperature_c: int,
) -> dict[int, LossBreakdown]:
    """Compute P_in, P_cond, P_sw, and P_other for each voltage at (freq, duty, temp)."""
    # Group runs by (voltage, config)
    by_volt_config: dict[int, dict[str, RunRecord]] = {}
    for r in runs:
        if r.status != "completed":
            continue
        p = r.point
        if (
            p.frequency_hz == frequency_hz
            and p.duty_pct == duty_pct
            and p.temperature_c == temperature_c
        ):
            by_volt_config.setdefault(p.voltage_v, {})[p.config] = r

    def input_power(record: Optional[RunRecord]):
        if record is None or record.readings.iin is None:
            return None
        voltage = record.readings.vin
        if voltage is None:
            voltage = record.bus_voltage_v
        if voltage is None:
            return None
        return voltage * record.readings.iin

    results: dict[int, LossBreakdown] = {}
    for volt, configs in sorted(by_volt_config.items()):
        dual = configs.get("Dual Conduction")
        s_cond = configs.get("Single Conduction")
        s_dev = configs.get("Single Device")

        p_dual = input_power(dual)
        p_s_cond = input_power(s_cond)
        p_s_dev = input_power(s_dev)

        # The subtraction model is meaningful only when all three measured
        # powers have the expected ordering. Clamping each negative delta
        # independently hides inconsistent data and can make the displayed
        # stacked losses exceed the measured total input power.
        decomposition_valid = (
            p_dual is not None
            and p_s_cond is not None
            and p_s_dev is not None
            and p_dual >= p_s_cond >= p_s_dev >= 0.0
        )
        if decomposition_valid:
            p_cond = p_dual - p_s_cond
            p_sw = p_s_cond - p_s_dev
            p_other = p_s_dev
        else:
            p_cond = p_sw = p_other = None

        # Estimate R_ds(on) = P_cond / (I_rms^2)
        r_dson = None
        if p_cond is not None and dual and dual.readings.irms and dual.readings.irms > 0:
            r_dson = p_cond / (dual.readings.irms ** 2)

        results[volt] = {
            "p_dual": p_dual,
            "p_s_cond": p_s_cond,
            "p_s_dev": p_s_dev,
            "p_cond": p_cond,
            "p_sw": p_sw,
            "p_other": p_other,
            "r_dson": r_dson,
        }
    return results


class AnalyticsTab(ttk.Frame):
    def __init__(
        self,
        master: tk.Widget,
        db: Database,
        get_device_name: Callable[[], str],
        on_generate_report: Optional[Callable[[], None]] = None,
    ):
        super().__init__(master, padding=10)
        self.db = db
        self.get_device_name = get_device_name
        self.on_generate_report = on_generate_report

        self._frequency_options: list[tuple[int, str]] = []
        self._duty_options: list[tuple[int, str]] = []
        self._temperature_options: list[tuple[int, str]] = []
        self._configuration_options: list[tuple[str, str]] = []
        self._voltage_options: list[tuple[int, str]] = []
        self.card_r_dson_title: ttk.Label

        self._build_ui()

    def _build_ui(self) -> None:
        # Filter Header Frame
        hdr = ttk.LabelFrame(self, text="Filter & Analytics Controls", padding=8)
        hdr.pack(fill="x", pady=(0, 10))

        ttk.Label(hdr, text="Frequency:").pack(side="left", padx=(0, 5))
        self.freq_combo = ttk.Combobox(
            hdr, width=12, state="readonly"
        )
        self.freq_combo.pack(side="left", padx=(0, 15))
        self.freq_combo.bind("<<ComboboxSelected>>", self._on_filter_changed)

        ttk.Label(hdr, text="Duty Cycle:").pack(side="left", padx=(0, 5))
        self.duty_combo = ttk.Combobox(
            hdr, width=8, state="readonly"
        )
        self.duty_combo.pack(side="left", padx=(0, 15))
        self.duty_combo.bind("<<ComboboxSelected>>", self._on_filter_changed)

        ttk.Label(hdr, text="Temperature:").pack(side="left", padx=(0, 5))
        self.temp_combo = ttk.Combobox(
            hdr, width=8, state="readonly"
        )
        self.temp_combo.pack(side="left", padx=(0, 15))
        self.temp_combo.bind("<<ComboboxSelected>>", self._on_filter_changed)

        ttk.Button(hdr, text="Refresh Analytics", command=self.refresh).pack(
            side="left", padx=(10, 5)
        )
        ttk.Button(hdr, text="Generate Full Report", command=self._export_report).pack(
            side="left", padx=5
        )

        # Executive KPI Cards Frame
        kpi_frame = ttk.Frame(self)
        kpi_frame.pack(fill="x", pady=(0, 10))

        self.card_completed = self._make_kpi_card(
            kpi_frame, "Completed Matrix Points", "0 / 0 (0%)", 0
        )
        self.card_p_max = self._make_kpi_card(
            kpi_frame, "Max Input Power (P_in)", "— W", 1
        )
        self.card_p_loss = self._make_kpi_card(
            kpi_frame, "Max P_loss (400V)", "— W", 2
        )
        self.card_r_dson = self._make_kpi_card(
            kpi_frame,
            "Est. R_ds(on) @ 25°C",
            "— mΩ",
            3,
            title_attr="card_r_dson_title",
        )

        for col in range(4):
            kpi_frame.grid_columnconfigure(col, weight=1)

        # Paned Window for Graphs and Table
        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True)

        # Top Charts Frame
        charts_frame = ttk.Frame(paned)
        paned.add(charts_frame, weight=3)

        self.fig = Figure(figsize=(10, 4), dpi=100)
        self.ax_bar = self.fig.add_subplot(121)
        self.ax_curve = self.fig.add_subplot(122)
        self.fig.tight_layout(pad=3.0)

        self.canvas = FigureCanvasTkAgg(self.fig, master=charts_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        # Bottom Table Frame
        table_frame = ttk.LabelFrame(paned, text="Side-by-Side 3-Configuration Loss Table", padding=6)
        paned.add(table_frame, weight=2)

        cols = (
            "voltage",
            "p_dual",
            "p_s_cond",
            "p_s_dev",
            "p_cond",
            "p_sw",
            "p_other",
            "r_dson",
        )
        self.table = ttk.Treeview(
            table_frame, columns=cols, show="headings", height=6
        )
        self.table.heading("voltage", text="Voltage (V)")
        self.table.heading("p_dual", text="Dual P_in (W)")
        self.table.heading("p_s_cond", text="Single Cond P_in (W)")
        self.table.heading("p_s_dev", text="Single Dev P_in (W)")
        self.table.heading("p_cond", text="Conduction Loss (W)")
        self.table.heading("p_sw", text="Switching Loss (W)")
        self.table.heading("p_other", text="Parasitic Loss (W)")
        self.table.heading("r_dson", text="Est. R_ds(on) (mΩ)")

        for column_name in cols:
            self.table.column(column_name, anchor="center", width=120)

        sb = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=sb.set)
        self.table.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def _make_kpi_card(
        self,
        parent: ttk.Frame,
        title: str,
        default_val: str,
        col: int,
        *,
        title_attr: Optional[str] = None,
    ) -> ttk.Label:
        card = ttk.Frame(parent, padding=8, relief="ridge")
        card.grid(row=0, column=col, padx=5, pady=2, sticky="nsew")
        title_label = ttk.Label(
            card, text=title, font=("Helvetica", 8, "bold")
        )
        title_label.pack(anchor="w")
        if title_attr is not None:
            setattr(self, title_attr, title_label)
        lbl = ttk.Label(card, text=default_val, font=("Consolas", 12, "bold"))
        lbl.pack(anchor="e", pady=(4, 0))
        return lbl

    def _on_filter_changed(self, _event=None) -> None:
        self.refresh()

    def update_filter_options(
        self,
        frequencies: list[tuple[int, str]],
        duties: list[tuple[int, str]],
        temperatures: list[tuple[int, str]],
        configurations: Optional[list[tuple[str, str]]] = None,
        voltages: Optional[list[tuple[int, str]]] = None,
    ) -> None:
        previous = (
            self._selected_value(self.freq_combo, self._frequency_options),
            self._selected_value(self.duty_combo, self._duty_options),
            self._selected_value(self.temp_combo, self._temperature_options),
        )
        self._frequency_options = list(frequencies)
        self._duty_options = list(duties)
        self._temperature_options = list(temperatures)
        self._configuration_options = list(configurations or [])
        self._voltage_options = list(voltages or [])

        for combo, options, prior in (
            (self.freq_combo, self._frequency_options, previous[0]),
            (self.duty_combo, self._duty_options, previous[1]),
            (self.temp_combo, self._temperature_options, previous[2]),
        ):
            combo["values"] = [label for _value, label in options]
            values = [value for value, _label in options]
            if prior in values:
                combo.current(values.index(prior))
            elif options:
                combo.current(0)
            else:
                combo.set("")
        self.refresh()

    @staticmethod
    def _selected_value(combo: ttk.Combobox, options):
        index = combo.current()
        if 0 <= index < len(options):
            return options[index][0]
        return None

    @staticmethod
    def _format_optional(value: Optional[float], *, scale: float = 1.0) -> str:
        return "—" if value is None else f"{value * scale:.2f}"

    def _clear_view(self) -> None:
        self.card_completed.config(text="0 / 0 (0%)")
        self.card_p_max.config(text="— W")
        self.card_p_loss.config(text="— W")
        self.card_r_dson.config(text="— mΩ")
        for row in self.table.get_children():
            self.table.delete(row)
        self.ax_bar.clear()
        self.ax_curve.clear()
        self.canvas.draw_idle()

    def refresh(self) -> None:
        device_name = self.get_device_name().strip()
        if not device_name:
            self._clear_view()
            return

        runs = self.db.runs_for_device(device_name)
        completed = [r for r in runs if r.status == "completed"]

        frequencies = {value for value, _label in self._frequency_options}
        configurations = {
            value for value, _label in self._configuration_options
        }
        duties = {value for value, _label in self._duty_options}
        temperatures = {
            value for value, _label in self._temperature_options
        }
        voltages = {value for value, _label in self._voltage_options}
        total_runs = (
            len(frequencies)
            * len(configurations)
            * len(duties)
            * len(temperatures)
            * len(voltages)
        )
        completed_keys = {
            (
                run.point.frequency_hz,
                run.point.config,
                run.point.duty_pct,
                run.point.temperature_c,
                run.point.voltage_v,
            )
            for run in completed
            if (
                run.point.frequency_hz in frequencies
                and run.point.config in configurations
                and run.point.duty_pct in duties
                and run.point.temperature_c in temperatures
                and run.point.voltage_v in voltages
            )
        }
        self.card_completed.config(
            text=(
                f"{len(completed_keys)} / {total_runs} "
                f"({len(completed_keys) / total_runs * 100:.0f}%)"
                if total_runs
                else "0 / 0 (0%)"
            )
        )

        freq_hz = self._selected_value(
            self.freq_combo, self._frequency_options
        )
        duty_pct = self._selected_value(self.duty_combo, self._duty_options)
        temp_c = self._selected_value(self.temp_combo, self._temperature_options)
        if freq_hz is None or duty_pct is None or temp_c is None:
            self._clear_view()
            return
        self.card_r_dson_title.config(
            text=f"Est. R_ds(on) @ {temp_c}°C"
        )

        loss_data = compute_loss_breakdown(completed, freq_hz, duty_pct, temp_c)

        # Clear existing table rows
        for row in self.table.get_children():
            self.table.delete(row)

        input_powers: list[float] = []
        losses_400: list[float] = []
        r_dson_vals: list[float] = []

        for volt, data in sorted(loss_data.items()):
            input_powers.extend(
                value
                for value in (
                    data["p_dual"],
                    data["p_s_cond"],
                    data["p_s_dev"],
                )
                if value is not None
            )
            if volt == 400 and data["p_dual"] is not None:
                losses_400.append(data["p_dual"])
            if data["r_dson"] is not None:
                r_dson_vals.append(data["r_dson"])

            self.table.insert(
                "",
                "end",
                values=(
                    f"{volt}V",
                    self._format_optional(data["p_dual"]),
                    self._format_optional(data["p_s_cond"]),
                    self._format_optional(data["p_s_dev"]),
                    self._format_optional(data["p_cond"]),
                    self._format_optional(data["p_sw"]),
                    self._format_optional(data["p_other"]),
                    (
                        f"{data['r_dson'] * 1000:.1f}"
                        if data["r_dson"] is not None
                        else "—"
                    ),
                ),
            )

        self.card_p_max.config(
            text=f"{max(input_powers):.2f} W" if input_powers else "— W"
        )
        self.card_p_loss.config(
            text=f"{max(losses_400):.2f} W" if losses_400 else "— W"
        )
        if r_dson_vals:
            avg_r = (sum(r_dson_vals) / len(r_dson_vals)) * 1000.0
            self.card_r_dson.config(text=f"{avg_r:.1f} mΩ")
        else:
            self.card_r_dson.config(text="— mΩ")

        # Update Matplotlib Visualizations
        self.ax_bar.clear()
        self.ax_curve.clear()

        volts = [
            voltage
            for voltage, values in sorted(loss_data.items())
            if values["p_cond"] is not None
            and values["p_sw"] is not None
            and values["p_other"] is not None
        ]
        if volts:
            p_conds = [cast(float, loss_data[v]["p_cond"]) for v in volts]
            p_sws = [cast(float, loss_data[v]["p_sw"]) for v in volts]
            p_others = [cast(float, loss_data[v]["p_other"]) for v in volts]

            # Stacked Bar Chart
            x = [f"{v}V" for v in volts]
            self.ax_bar.bar(x, p_conds, label="Conduction (P_cond)", color="#2196F3")
            self.ax_bar.bar(
                x, p_sws, bottom=p_conds, label="Switching (P_sw)", color="#FF9800"
            )
            bottom_2 = [c + s for c, s in zip(p_conds, p_sws)]
            self.ax_bar.bar(
                x, p_others, bottom=bottom_2, label="Parasitic (P_other)", color="#9E9E9E"
            )

            self.ax_bar.set_ylabel("Power Loss (W)")
            self.ax_bar.set_title(f"Loss Decomposition ({freq_label(freq_hz)}, {duty_pct}%)")
            self.ax_bar.legend(fontsize=7, loc="upper left")
            self.ax_bar.grid(True, alpha=0.3)

        # Curve comparison
        grouped_curves: dict[str, list[tuple[int, float]]] = {}
        for r in completed:
            p = r.point
            if (
                p.frequency_hz == freq_hz
                and p.duty_pct == duty_pct
                and p.temperature_c == temp_c
                and r.readings.iin is not None
            ):
                grouped_curves.setdefault(p.config, []).append(
                    (p.voltage_v, r.readings.iin * 1000.0)
                )

        for cfg, pts in sorted(grouped_curves.items()):
            pts.sort()
            self.ax_curve.plot(
                [v for v, _ in pts],
                [i for _, i in pts],
                marker="o",
                label=cfg,
            )

        self.ax_curve.set_xlabel("Test Voltage (V)")
        self.ax_curve.set_ylabel("DC Input Current (mA)")
        self.ax_curve.set_title(f"3-Configuration Iin Overlay ({temp_c}°C)")
        if grouped_curves:
            self.ax_curve.legend(fontsize=7, loc="upper left")
        self.ax_curve.grid(True, alpha=0.3)

        self.fig.tight_layout(pad=2.0)
        self.canvas.draw()

    def _export_report(self) -> None:
        if self.on_generate_report is not None:
            self.on_generate_report()
            return
        device_name = self.get_device_name().strip()
        if not device_name:
            messagebox.showwarning("Warning", "Please select a valid device.")
            return
        out_dir = self.db.path.parent / "reports"
        report_path = reports_mod.generate_device_report(self.db, device_name, out_dir)
        messagebox.showinfo(
            "Report Generated", f"Executive report generated at:\n{report_path}"
        )
