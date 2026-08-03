"""Live multi-trace plot with dark theme support and interactive navigation."""

from __future__ import annotations

import tkinter as tk
from collections import deque
from pathlib import Path
from typing import Optional

import matplotlib

matplotlib.use("TkAgg")

import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

PLOT_MAX_POINTS = 200

# Theme Palettes
DARK_BG = "#1e1e1e"
DARK_PANEL = "#2d2d2d"
DARK_TEXT = "#e0e0e0"
DARK_GRID = "#444444"

LIGHT_BG = "#ffffff"
LIGHT_PANEL = "#f8f9fa"
LIGHT_TEXT = "#212529"
LIGHT_GRID = "#dee2e6"


class LivePlot:
    """Enhanced live plot component supporting dual Y-axes, dark mode, and export."""

    def __init__(self, parent_frame: tk.Misc, dark_mode: bool = True):
        self.dark_mode = dark_mode
        self.parent_frame = parent_frame

        self.time_vals: deque[float] = deque(maxlen=PLOT_MAX_POINTS)
        self.current_vals: deque[float] = deque(maxlen=PLOT_MAX_POINTS)
        self.voltage_time_vals: deque[float] = deque(maxlen=PLOT_MAX_POINTS)
        self.voltage_vals: deque[float] = deque(maxlen=PLOT_MAX_POINTS)

        self.fig, self.ax1 = plt.subplots(figsize=(8, 4))
        self.ax2 = self.ax1.twinx()

        self._apply_theme()

        self.line_current, = self.ax1.plot([], [], color="#00e676", linewidth=1.8, label="DC Current (A)")
        self.line_voltage, = self.ax2.plot([], [], color="#29b6f6", linewidth=1.5, linestyle="--", label="SMU Voltage (V)")

        self.canvas = FigureCanvasTkAgg(self.fig, master=parent_frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=1)

        # Matplotlib navigation toolbar
        self.toolbar = NavigationToolbar2Tk(self.canvas, parent_frame, pack_toolbar=False)
        self.toolbar.update()
        self.toolbar.pack(side=tk.BOTTOM, fill=tk.X)

    def _apply_theme(self) -> None:
        bg = DARK_BG if self.dark_mode else LIGHT_BG
        panel = DARK_PANEL if self.dark_mode else LIGHT_PANEL
        text = DARK_TEXT if self.dark_mode else LIGHT_TEXT
        grid = DARK_GRID if self.dark_mode else LIGHT_GRID

        self.fig.patch.set_facecolor(bg)
        for ax in (self.ax1, self.ax2):
            ax.set_facecolor(panel)
            ax.tick_params(colors=text)
            for spine in ax.spines.values():
                spine.set_color(grid)

        self.ax1.set_xlabel("Elapsed Time (s)", color=text)
        self.ax1.set_ylabel("Current (A)", color="#00e676")
        self.ax2.set_ylabel("Voltage (V)", color="#29b6f6")
        self.ax1.grid(True, color=grid, linestyle=":", alpha=0.6)

    def set_dark_mode(self, enabled: bool) -> None:
        """Toggle dark/light theme."""
        self.dark_mode = enabled
        self._apply_theme()
        self.canvas.draw_idle()

    def reset(self, title: str = "Real-Time Characterisation Plot") -> None:
        """Clear data and re-initialize axes."""
        self.time_vals.clear()
        self.current_vals.clear()
        self.voltage_time_vals.clear()
        self.voltage_vals.clear()

        self.ax1.cla()
        self.ax2.cla()

        self._apply_theme()
        self.ax1.set_title(title, color=DARK_TEXT if self.dark_mode else LIGHT_TEXT, pad=10)

        self.line_current, = self.ax1.plot([], [], color="#00e676", linewidth=1.8, label="DC Current (A)")
        self.line_voltage, = self.ax2.plot([], [], color="#29b6f6", linewidth=1.5, linestyle="--", label="SMU Voltage (V)")

        self.canvas.draw()

    def append(self, elapsed_s: float, current_a: float, smu_voltage_v: Optional[float] = None) -> None:
        """Append new sample point and update curves."""
        self.time_vals.append(elapsed_s)
        self.current_vals.append(current_a)
        if smu_voltage_v is not None:
            self.voltage_time_vals.append(elapsed_s)
            self.voltage_vals.append(smu_voltage_v)

        self.line_current.set_data(self.time_vals, self.current_vals)
        if self.voltage_vals:
            self.line_voltage.set_data(
                self.voltage_time_vals, self.voltage_vals
            )

        if self.current_vals:
            # Rescale current Y-axis
            low_i = min(self.current_vals)
            high_i = max(self.current_vals)
            pad_i = max((high_i - low_i) * 0.1, abs(high_i) * 0.05, 1e-4)
            self.ax1.set_ylim(low_i - pad_i, high_i + pad_i)
            self.ax1.set_xlim(self.time_vals[0], max(self.time_vals[-1], 1.0))

        if self.voltage_vals:
            # Rescale voltage Y-axis
            low_v = min(self.voltage_vals)
            high_v = max(self.voltage_vals)
            pad_v = max((high_v - low_v) * 0.1, 1.0)
            self.ax2.set_ylim(low_v - pad_v, high_v + pad_v)

        self.canvas.draw_idle()

    def export_image(self, file_path: Path | str) -> None:
        """Export current figure as an image file."""
        self.fig.savefig(str(file_path), dpi=300, bbox_inches="tight", facecolor=self.fig.get_facecolor())
