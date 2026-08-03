"""Live run plot on the Qt canvas.

Same contract as the Tk ``LivePlot``: ``reset`` starts a run's trace,
``append`` adds one sample. Dual axes because current and bus voltage share
a time base but not a scale.
"""

from __future__ import annotations

from typing import Optional

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from matplotlib.lines import Line2D


class QtLivePlot(FigureCanvasQTAgg):
    def __init__(self) -> None:
        figure = Figure(figsize=(5.2, 4.0), tight_layout=True)
        super().__init__(figure)
        self._ax_current = figure.add_subplot(111)
        self._ax_volts = self._ax_current.twinx()
        self._times: list[float] = []
        self._currents: list[float] = []
        self._volts: list[Optional[float]] = []
        self._line_current: Line2D
        self._line_volts: Line2D
        self.reset("No run yet")

    def reset(self, title: str) -> None:
        self._times.clear()
        self._currents.clear()
        self._volts.clear()
        self._ax_current.clear()
        self._ax_volts.clear()
        self._ax_current.set_title(title, fontsize=9)
        self._ax_current.set_xlabel("Elapsed (s)")
        self._ax_current.set_ylabel("DC input current (A)", color="tab:blue")
        self._ax_volts.set_ylabel("SMU voltage (V)", color="tab:red")
        # One artist per axis, updated in place by append(). Plotting a new
        # line per sample stacks artists for the run's whole duration.
        (self._line_current,) = self._ax_current.plot(
            [], [], color="tab:blue", linewidth=1.2
        )
        (self._line_volts,) = self._ax_volts.plot(
            [], [], color="tab:red", linewidth=1.0
        )
        self.draw_idle()

    def append(
        self,
        elapsed_s: float,
        current_a: float,
        smu_voltage_v: Optional[float],
    ) -> None:
        self._times.append(elapsed_s)
        self._currents.append(current_a)
        self._volts.append(smu_voltage_v)
        self._line_current.set_data(self._times, self._currents)
        known = [
            (t, v)
            for t, v in zip(self._times, self._volts)
            if v is not None
        ]
        if known:
            self._line_volts.set_data(
                [t for t, _ in known],
                [v for _, v in known],
            )
        for ax in (self._ax_current, self._ax_volts):
            ax.relim()
            ax.autoscale_view()
        self.draw_idle()

    @property
    def sample_count(self) -> int:
        return len(self._times)

    @property
    def title(self) -> str:
        return self._ax_current.get_title()
