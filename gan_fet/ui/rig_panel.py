"""Manual rig control: SMU readout, ZVS search, bus ramp-down, EMERGENCY STOP.

Every action here goes through the shared `RigActivity` guard, so a manual
operation can never run concurrently with an experiment, a sequence, or
another manual operation — two threads driving the SMU at once could leave
the bus at an arbitrary voltage.
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, Optional

from gan_fet.core.activity import RigActivity, RigOwner
from gan_fet.core.experiment import ExperimentEngine
from gan_fet.core.safety import SafetyMonitor
from gan_fet.core.voltage_control import PeakControlError
from gan_fet.instruments.smu import Keithley2400
from gan_fet.ui.tasks import run_in_background
from gan_fet.ui.widgets import ColorButton

log = logging.getLogger(__name__)


class RigPanel(ttk.LabelFrame):
    def __init__(
        self,
        master: tk.Misc,
        *,
        smu: Keithley2400,
        engine: ExperimentEngine,
        safety: SafetyMonitor,
        activity: RigActivity,
        target_voltage: Callable[[], int],
        status: Callable[[str], None],
    ):
        super().__init__(master, text="SMU (Keithley 2400-series)")
        self.smu = smu
        self.engine = engine
        self.safety = safety
        self.activity = activity
        self.target_voltage = target_voltage
        self.status = status
        self._zvs_running = False

        self.status_label = ttk.Label(self, text="Bus: — | I: — | Output: OFF")
        self.status_label.grid(row=0, column=0, sticky="w", padx=8, pady=4)

        self.zvs_button = ttk.Button(self, text="Find ZVS Now", command=self._toggle_zvs)
        self.zvs_button.grid(row=0, column=1, padx=6, pady=4)

        self.bus_off_button = ttk.Button(self, text="Bus Off", command=self._bus_off)
        self.bus_off_button.grid(row=0, column=2, padx=6, pady=4)

        self.estop_button = ColorButton(
            self, text="EMERGENCY STOP", command=self.emergency_stop,
            width=170, height=36, borderless=1, highlightthickness=1,
        )
        self.estop_button.set_style(
            bg="#b71c1c", fg="white", active_bg="#7f0000", active_fg="white"
        )
        self.estop_button.grid(row=0, column=3, padx=10, pady=4)
        self.grid_columnconfigure(0, weight=1)

        self.refresh()

    # -- readout ----------------------------------------------------------

    def refresh(self) -> None:
        state = "ON" if self.smu.output_is_on else "OFF"
        current = self.engine.last_current
        current_text = f"{current * 1000:.2f} mA" if current is not None else "—"
        self.status_label.config(
            text=f"Bus setpoint: {self.smu.setpoint_v:.1f} V | "
                 f"I: {current_text} | Output: {state}"
        )

    # -- ZVS --------------------------------------------------------------

    def _toggle_zvs(self) -> None:
        if self._zvs_running:
            # A search in flight: ask the control loops to stop at the next
            # step rather than leaving the operator only the E-STOP.
            self.activity.request_cancel()
            self.zvs_button.config(text="Stopping...", state="disabled")
            self.status("Stopping ZVS search...")
            return

        if not self.activity.try_acquire(RigOwner.MANUAL):
            messagebox.showinfo(
                "Find ZVS", f"The rig is busy: {self.activity.owner.value} in progress."
            )
            return

        target_v = float(self.target_voltage())
        self._zvs_running = True
        self.zvs_button.config(text="Stop ZVS", state="normal")
        self.status("Bringing the bus up for ZVS search...")

        cancelled = self.activity.cancel_check()

        def work():
            if not self.smu.output_on():
                raise ConnectionError("Could not enable the SMU output")
            self.engine.peak_controller.achieve_peak(
                target_v, cancel_check=cancelled, status=self.status
            )
            return self.engine.zvs_tuner.find_minimum(
                cancel_check=cancelled, status=self.status
            )

        run_in_background(self, work, self._on_zvs_done, name="zvs")

    def _on_zvs_done(self, result, error: Optional[BaseException]) -> None:
        self._zvs_running = False
        self.activity.release(RigOwner.MANUAL)
        self.zvs_button.config(text="Find ZVS Now", state="normal")
        self.refresh()

        if error is not None:
            if isinstance(error, PeakControlError) and str(error) == "cancelled":
                self.status("ZVS search cancelled. Bus is still live — use Bus Off.")
            else:
                messagebox.showerror("Find ZVS", f"ZVS search failed: {error}")
                self.status("ZVS search failed.")
        elif result is None:
            self.status("ZVS search found no improvement.")
        else:
            self.status(
                f"ZVS point: {result.v_zvs:.1f} V ({result.i_min * 1000:.2f} mA). "
                "Bus is live at the ZVS point."
            )

    # -- bus off ------------------------------------------------------------

    def _bus_off(self) -> None:
        if not self.activity.try_acquire(RigOwner.MANUAL):
            messagebox.showinfo(
                "Bus Off",
                f"The rig is busy: {self.activity.owner.value} in progress.\n\n"
                "Stop it first, or use EMERGENCY STOP.",
            )
            return

        def work():
            try:
                self.smu.ramp_to(0.0)
                self.smu.output_off()
            except Exception:
                self.smu.emergency_off()
                raise

        def done(_result, error: Optional[BaseException]) -> None:
            self.activity.release(RigOwner.MANUAL)
            self.refresh()
            self.status("Bus off." if error is None else f"Bus off (forced): {error}")

        run_in_background(self, work, done, name="bus-off")
        self.status("Ramping bus to 0 V...")

    # -- emergency stop -------------------------------------------------------

    def emergency_stop(self) -> None:
        """Deliberately bypasses the activity guard: it must cut the output
        while another owner still holds the rig."""
        self.activity.request_cancel()
        self.engine.cancel()
        run_in_background(
            self,
            self.safety.emergency_stop,
            lambda _r, _e: self.refresh(),
            name="estop",
        )
