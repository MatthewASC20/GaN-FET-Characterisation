"""Analytics pane for the Qt front-end: loss breakdown per configuration.

Presentation only. Every derived power figure comes from
``compute_loss_breakdown`` in ``gan_fet.ui.analytics_tab`` — that function is
toolkit-free despite living beside the Tk widgets, and reusing it rather than
porting it keeps the two front-ends incapable of disagreeing about a loss
number. Importing that module does pull ``tkinter`` in at import time, but
only as a module import: no Tk widget or interpreter is ever created here.

Missing configurations render as an em dash, never zero. On this rig a zero
input power is a measurement and an absent one is not; the shared formatters
in ``gan_fet.ui.telemetry_format`` encode that rule once for both front-ends.
"""

from __future__ import annotations

from typing import Optional, cast

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gan_fet.core.models import RunRecord, freq_label
from gan_fet.settings import DEFAULT_CONFIGURATIONS
from gan_fet.storage.db import Database
from gan_fet.ui.analytics_tab import LossBreakdown, compute_loss_breakdown
from gan_fet.ui.param_options import default_label
from gan_fet.ui.telemetry_format import (
    NO_READING,
    format_milliamps,
    format_volts,
)

#: Filter axes, keyed by the option-set names ``default_label`` understands.
_FILTER_KEYS = ("frequencies", "duties", "temperatures")
_FILTER_TITLES = {
    "frequencies": "Frequency:",
    "duties": "Duty Cycle:",
    "temperatures": "Temperature:",
}
_FILTER_POINT_ATTRS = {
    "frequencies": "frequency_hz",
    "duties": "duty_pct",
    "temperatures": "temperature_c",
}

#: Same series colours as the Tk chart: colour follows the loss component,
#: and an operator moving between the two front-ends must see conduction,
#: switching and parasitic loss painted identically in both. Order matches
#: ``_loss_components``.
_BAR_SERIES = (
    ("Conduction (P_cond)", "#2196F3"),
    ("Switching (P_sw)", "#FF9800"),
    ("Parasitic (P_other)", "#9E9E9E"),
)

#: Per-configuration table measures, each rendered by a shared formatter so
#: the em-dash rule cannot drift from the telemetry cards.
_TABLE_MEASURES = ("Vin", "Iin", "P_in")


def _format_watts(power_w: Optional[float]) -> str:
    """Watts with the shared no-reading rule; no such formatter exists yet."""
    return f"{NO_READING} W" if power_w is None else f"{power_w:.2f} W"


def _config_powers(breakdown: LossBreakdown) -> dict[str, Optional[float]]:
    """Map configuration names onto the breakdown's per-configuration P_in.

    Spelled out with literal keys so mypy checks each against the TypedDict,
    rather than indexing it with a runtime string.
    """
    return {
        "Dual Conduction": breakdown["p_dual"],
        "Single Conduction": breakdown["p_s_cond"],
        "Single Device": breakdown["p_s_dev"],
    }


def _loss_components(
    breakdown: LossBreakdown,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """The stacked-bar series in ``_BAR_SERIES`` order, with literal keys."""
    return breakdown["p_cond"], breakdown["p_sw"], breakdown["p_other"]


class AnalyticsPane(QWidget):
    """Filters, per-configuration KPIs, a loss chart and a comparison table."""

    def __init__(self, db: Database) -> None:
        super().__init__()
        self._db = db
        self._device_name = ""
        self._completed: list[RunRecord] = []
        #: Values behind each combo, index-aligned with its labels. The combo
        #: shows labels only, so selection must be resolved through this.
        self._filter_values: dict[str, list[int]] = {
            key: [] for key in _FILTER_KEYS
        }
        self._repopulating = False
        self._build_ui()

    # -- construction --------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        filter_row = QHBoxLayout()
        self.filters: dict[str, QComboBox] = {}
        for key in _FILTER_KEYS:
            filter_row.addWidget(QLabel(_FILTER_TITLES[key]))
            combo = QComboBox()
            combo.currentIndexChanged.connect(self._on_filter_changed)
            self.filters[key] = combo
            filter_row.addWidget(combo)
        filter_row.addStretch(1)
        layout.addLayout(filter_row)

        # One KPI card per configuration, laid out like the main window's
        # telemetry cards: bold title above the value.
        kpi_grid = QGridLayout()
        self.kpi_values: dict[str, QLabel] = {}
        for column, config in enumerate(DEFAULT_CONFIGURATIONS):
            title = QLabel(f"{config} P_in")
            title.setStyleSheet("font-weight: bold;")
            value = QLabel(_format_watts(None))
            kpi_grid.addWidget(
                title, 0, column, Qt.AlignmentFlag.AlignCenter
            )
            kpi_grid.addWidget(
                value, 1, column, Qt.AlignmentFlag.AlignCenter
            )
            self.kpi_values[config] = value
        layout.addLayout(kpi_grid)

        figure = Figure(figsize=(6.0, 3.2), tight_layout=True)
        self.canvas = FigureCanvasQTAgg(figure)
        self.ax = figure.add_subplot(111)
        layout.addWidget(self.canvas, stretch=3)

        headers = ["Voltage"] + [
            f"{config} {measure}"
            for config in DEFAULT_CONFIGURATIONS
            for measure in _TABLE_MEASURES
        ]
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        row_header = self.table.verticalHeader()
        if row_header is not None:
            row_header.setVisible(False)
        layout.addWidget(self.table, stretch=2)

    # -- data flow -----------------------------------------------------------

    def refresh(self, device_name: str) -> None:
        """Reload the device's completed runs and re-render everything."""
        self._device_name = device_name.strip()
        if self._device_name:
            runs = self._db.runs_for_device(self._device_name)
            self._completed = [r for r in runs if r.status == "completed"]
        else:
            self._completed = []
        self._repopulate_filters()
        self._render()

    def _on_filter_changed(self, _index: int) -> None:
        # Repopulation rewrites every combo; rendering once at the end is
        # both cheaper and immune to half-updated filter state.
        if not self._repopulating:
            self._render()

    def _repopulate_filters(self) -> None:
        """Offer exactly the values the device has completed runs for.

        A prior selection survives when its value still exists, so refreshing
        after a new run does not silently jump the operator to another
        operating point.
        """
        self._repopulating = True
        try:
            for key in _FILTER_KEYS:
                prior = self._selected_value(key)
                attr = _FILTER_POINT_ATTRS[key]
                values = sorted(
                    {getattr(run.point, attr) for run in self._completed}
                )
                self._filter_values[key] = values
                combo = self.filters[key]
                combo.clear()
                for value in values:
                    combo.addItem(default_label(key, value))
                if prior in values:
                    combo.setCurrentIndex(values.index(prior))
        finally:
            self._repopulating = False

    def _selected_value(self, key: str) -> Optional[int]:
        index = self.filters[key].currentIndex()
        values = self._filter_values[key]
        return values[index] if 0 <= index < len(values) else None

    # -- rendering -----------------------------------------------------------

    def _clear_view(self) -> None:
        for value in self.kpi_values.values():
            value.setText(_format_watts(None))
        self.table.setRowCount(0)
        self.ax.clear()
        self.canvas.draw_idle()

    def _render(self) -> None:
        freq_hz = self._selected_value("frequencies")
        duty_pct = self._selected_value("duties")
        temp_c = self._selected_value("temperatures")
        if freq_hz is None or duty_pct is None or temp_c is None:
            self._clear_view()
            return

        breakdowns = compute_loss_breakdown(
            self._completed, freq_hz, duty_pct, temp_c
        )
        # Every derived power above came from compute_loss_breakdown; this
        # lookup only recovers the raw Vin/Iin readings the table shows
        # beside them, which the policy function does not expose.
        by_volt_config: dict[int, dict[str, RunRecord]] = {}
        for record in self._completed:
            point = record.point
            if (
                point.frequency_hz == freq_hz
                and point.duty_pct == duty_pct
                and point.temperature_c == temp_c
            ):
                by_volt_config.setdefault(point.voltage_v, {})[
                    point.config
                ] = record

        self._render_kpis(breakdowns)
        self._render_table(breakdowns, by_volt_config)
        self._render_chart(breakdowns, freq_hz, duty_pct)

    def _render_kpis(self, breakdowns: dict[int, LossBreakdown]) -> None:
        """Headline P_in per configuration: the worst case over voltage.

        The maximum is the number that matters on a stress rig — it is what
        the device actually had to survive at this operating point.
        """
        per_voltage = [
            _config_powers(breakdown) for breakdown in breakdowns.values()
        ]
        for config in DEFAULT_CONFIGURATIONS:
            powers = [
                power
                for powers_at_voltage in per_voltage
                if (power := powers_at_voltage[config]) is not None
            ]
            self.kpi_values[config].setText(
                _format_watts(max(powers) if powers else None)
            )

    def _render_table(
        self,
        breakdowns: dict[int, LossBreakdown],
        by_volt_config: dict[int, dict[str, RunRecord]],
    ) -> None:
        rows = sorted(breakdowns.items())
        self.table.setRowCount(len(rows))
        for row, (volt, breakdown) in enumerate(rows):
            powers = _config_powers(breakdown)
            cells = [f"{volt}V"]
            for config in DEFAULT_CONFIGURATIONS:
                record = by_volt_config.get(volt, {}).get(config)
                readings = record.readings if record is not None else None
                cells.append(
                    format_volts(readings.vin if readings else None)
                )
                cells.append(
                    format_milliamps(readings.iin if readings else None)
                )
                cells.append(_format_watts(powers.get(config)))
            for column, text in enumerate(cells):
                self.table.setItem(row, column, QTableWidgetItem(text))

    def _render_chart(
        self,
        breakdowns: dict[int, LossBreakdown],
        freq_hz: int,
        duty_pct: int,
    ) -> None:
        self.ax.clear()
        # Only voltages with a valid decomposition get bars; the policy
        # function already withheld the split where the ordering failed.
        split = {
            volt: _loss_components(breakdown)
            for volt, breakdown in sorted(breakdowns.items())
        }
        volts = [
            volt
            for volt, components in split.items()
            if all(component is not None for component in components)
        ]
        if volts:
            labels = [f"{v}V" for v in volts]
            bottoms = [0.0 for _ in volts]
            for series, (label, colour) in enumerate(_BAR_SERIES):
                heights = [cast(float, split[v][series]) for v in volts]
                self.ax.bar(
                    labels, heights, bottom=bottoms, label=label, color=colour
                )
                bottoms = [b + h for b, h in zip(bottoms, heights)]
            self.ax.set_ylabel("Power Loss (W)")
            self.ax.set_title(
                f"Loss Decomposition ({freq_label(freq_hz)}, {duty_pct}%)"
            )
            self.ax.legend(fontsize=7, loc="upper left")
            self.ax.grid(True, alpha=0.3)
        self.canvas.draw_idle()
