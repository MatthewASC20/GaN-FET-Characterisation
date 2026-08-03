"""The SMU row: bus status and the four manual controls.

Owns construction and layout. Which controls are enabled when is decided by
``resolve_rig_control_state`` and applied by the window, so this panel exposes
its widgets rather than hiding them — moving that seam is a separate change.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from gan_fet.ui.smu_status import smu_status_line
from gan_fet.ui.widgets import ColorButton, RigControlState, set_widget_enabled

#: Emergency stop is styled apart from everything else in the window. It is
#: the one control that must be findable without reading any labels.
ESTOP_STYLE = {
    "bg": "#b71c1c",
    "fg": "white",
    "active_bg": "#7f0000",
    "active_fg": "white",
}


class SmuPanel(ttk.LabelFrame):
    """Bus status plus Tune DC Voltage, Bus Off, Reset Safety and Emergency Stop."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        on_tune_voltage: Callable[[], None],
        on_bus_off: Callable[[], None],
        on_reset_safety: Callable[[], None],
        on_emergency_stop: Callable[[], None],
    ) -> None:
        super().__init__(parent, text="SMU (Keithley 2400-series)")

        self.status_label = ttk.Label(
            self,
            text=smu_status_line(setpoint_v=None, current_a=None, output_on=False),
        )
        self.status_label.grid(row=0, column=0, sticky="w", padx=8, pady=4)

        self.voltage_tune_button = ttk.Button(
            self, text="Tune DC Voltage Now", command=on_tune_voltage
        )
        self.voltage_tune_button.grid(row=0, column=1, padx=6, pady=4)

        self.bus_off_button = ttk.Button(self, text="Bus Off", command=on_bus_off)
        self.bus_off_button.grid(row=0, column=2, padx=6, pady=4)

        self.reset_safety_button = ttk.Button(
            self, text="Reset Safety", command=on_reset_safety
        )
        self.reset_safety_button.grid(row=0, column=3, padx=6, pady=4)

        self.estop_button = ColorButton(
            self,
            text="EMERGENCY STOP",
            command=on_emergency_stop,
            width=170,
            height=36,
            borderless=1,
            highlightthickness=1,
        )
        self.estop_button.set_style(**ESTOP_STYLE)
        self.estop_button.grid(row=0, column=4, padx=10, pady=4)

        # The status text takes the slack so the buttons keep their positions
        # as the window is resized.
        self.grid_columnconfigure(0, weight=1)

    def set_status(self, text: str) -> None:
        self.status_label.config(text=text)

    def apply_control_state(self, controls: RigControlState) -> None:
        """Enable or disable the manual controls for one snapshot of rig state.

        **Emergency stop is deliberately absent.** It is never disabled, by any
        state, including while an operation is running, while the safety latch
        is set, and while the instruments are offline — those are exactly the
        situations in which it is needed. Adding it here would be a mistake
        even if every current state happened to leave it enabled.
        """
        if controls.stop_voltage_tune:
            self.voltage_tune_button.config(state="normal", text="Stop Voltage Tune")
        else:
            self.voltage_tune_button.config(
                state="normal" if controls.hardware_actions else "disabled",
                text="Tune DC Voltage Now",
            )
        set_widget_enabled(self.bus_off_button, controls.shutdown_actions)
        set_widget_enabled(self.reset_safety_button, controls.reset_safety)

    def set_voltage_tune_stopping(self) -> None:
        """Show that a cooperative stop has been requested but not completed.

        Disabled so the request cannot be repeated while it is in flight; the
        next control-state refresh restores whichever label is then correct.
        """
        self.voltage_tune_button.config(text="Stopping...", state="disabled")
