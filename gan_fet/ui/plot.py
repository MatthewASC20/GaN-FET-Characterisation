"""Live DC-current plot embedded in the main window."""

from __future__ import annotations

import tkinter as tk
from collections import deque

import matplotlib

matplotlib.use("TkAgg")

import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

PLOT_MAX_POINTS = 100


class LivePlot:
    def __init__(self, parent_frame: tk.Misc):
        self.time_vals: deque[float] = deque(maxlen=PLOT_MAX_POINTS)
        self.current_vals: deque[float] = deque(maxlen=PLOT_MAX_POINTS)

        self.fig, self.ax = plt.subplots(figsize=(7, 4))
        self._style_axes("Real-Time DC Current")
        self.line, = self.ax.plot([], [], label="DC Current (A)")
        self.ax.legend()

        self.canvas = FigureCanvasTkAgg(self.fig, master=parent_frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=1)

    def _style_axes(self, title: str) -> None:
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("Current (A)")
        self.ax.set_title(title)
        self.ax.grid(True)

    def reset(self, title: str) -> None:
        self.time_vals.clear()
        self.current_vals.clear()
        self.ax.cla()
        self._style_axes(title)
        self.line, = self.ax.plot([], [], label="DC Current (A)")
        self.ax.legend()
        self.canvas.draw()

    def append(self, elapsed_s: float, current_a: float) -> None:
        self.time_vals.append(elapsed_s)
        self.current_vals.append(current_a)
        self.line.set_data(self.time_vals, self.current_vals)
        if self.current_vals:
            low = min(self.current_vals)
            high = max(self.current_vals)
            pad = max((high - low) * 0.1, abs(high) * 0.05, 1e-6)
            self.ax.set_ylim(low - pad, high + pad)
            self.ax.set_xlim(self.time_vals[0], max(self.time_vals[-1], 1))
        self.canvas.draw_idle()
