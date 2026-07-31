"""Wavegen apply + frequency autotune controls.

Owns the two coloured buttons and the state they encode:
  red    — the instrument does not match the current selection
  yellow — selection applied, but a previously measured (tuned) frequency
           is available and not yet applied
  green  — instrument matches the selection
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import messagebox
from typing import Callable, Optional, Tuple

from gan_fet.core.activity import RigActivity, RigOwner
from gan_fet.core.autotune import WavegenController
from gan_fet.ui.tasks import run_in_background
from gan_fet.ui.widgets import ColorButton, show_temporary_popup

log = logging.getLogger(__name__)

COLOR_PENDING = "#e53935"
COLOR_TUNING = "#FBC02D"
COLOR_OK = "#43a047"
COLOR_DISABLED = "#bdbdbd"
COLOR_ACTIVE = "#1976D2"

#: (frequency_hz, source_config, source_temperature_c)
TuningCandidate = Tuple[float, str, int]


class WavegenControls:
    """Not a Frame: the two buttons are placed into the caller's grid so the
    existing layout is preserved exactly."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        controller: WavegenController,
        activity: RigActivity,
        selection: Callable[[], Tuple[str, int, int]],   # (config, freq_hz, duty)
        tuning_candidate: Callable[[], Optional[TuningCandidate]],
        status: Callable[[str], None],
    ):
        self.master = master
        self.controller = controller
        self.activity = activity
        self.selection = selection
        self.tuning_candidate = tuning_candidate
        self.status = status
        self._tuning = False

        self.apply_button = ColorButton(
            master, text="Apply Wavegen Settings", command=self.apply,
            width=170, height=36, borderless=1, highlightthickness=1,
        )
        self.autotune_button = ColorButton(
            master, text="Autotune Unavailable", command=self.start_autotune,
            width=170, height=36, borderless=1, highlightthickness=1,
        )

    def grid(self, **kwargs) -> None:
        """Place apply at the given cell and autotune in the next column."""
        column = kwargs.pop("column", 0)
        self.apply_button.grid(column=column, **kwargs)
        self.autotune_button.grid(column=column + 1, **kwargs)

    # -- state ------------------------------------------------------------

    def refresh(self) -> None:
        config, freq, duty = self.selection()
        pending = self.controller.has_pending_changes(config, freq, duty)
        candidate = self.tuning_candidate()

        if pending:
            self.apply_button.set_style(bg=COLOR_PENDING, fg="white")
        elif candidate:
            self.apply_button.set_style(bg=COLOR_TUNING, fg="black")
        else:
            self.apply_button.set_style(bg=COLOR_OK, fg="white")

        if self._tuning:
            return
        if candidate:
            self.autotune_button.set_style(
                bg=COLOR_OK, fg="white", active_bg="#388e3c",
                text=f"Autotune: {candidate[0] / 1e6:.2f} MHz", state="normal",
            )
        else:
            self.autotune_button.set_style(
                bg=COLOR_DISABLED, fg="white",
                text="Autotune Unavailable", state="disabled",
            )

    # -- apply --------------------------------------------------------------

    def _confirm_high_risk(self, duty: int) -> bool:
        previous = self.controller.applied_duty
        if previous is None or duty == previous:
            return True
        return messagebox.askyesno(
            "Confirm High-Risk Change",
            "The following high-risk changes were detected:\n\n"
            f"- Duty cycle change from {previous}% to {duty}%.\n\n"
            "These changes may damage the device. Proceed?",
            icon="warning",
        )

    def apply(self) -> bool:
        config, freq, duty = self.selection()
        if not self._confirm_high_risk(duty):
            return False
        try:
            self.controller.apply(config, freq, duty)
        except Exception as exc:
            messagebox.showerror("Wavegen Error", f"Failed to configure wavegen: {exc}")
            return False
        self.status("Wavegen parameters applied.")
        self.refresh()
        return True

    def has_pending_changes(self) -> bool:
        return self.controller.has_pending_changes(*self.selection())

    # -- autotune -------------------------------------------------------------

    def start_autotune(self) -> None:
        if self._tuning:
            return
        candidate = self.tuning_candidate()
        if candidate is None:
            messagebox.showinfo(
                "Autotune", "No tuned frequency is available for the current settings."
            )
            return

        if self.has_pending_changes():
            if not messagebox.askyesno(
                "Autotune", "Wavegen settings have not been applied yet. Apply them first?"
            ):
                return
            if not self.apply():
                return

        if not self.activity.try_acquire(RigOwner.MANUAL):
            messagebox.showinfo(
                "Autotune", f"The rig is busy: {self.activity.owner.value} in progress."
            )
            return

        target, src_config, src_temp = candidate
        config = self.selection()[0]
        self._tuning = True
        self.autotune_button.set_style(
            bg=COLOR_ACTIVE, fg="white", text="Autotuning...", state="disabled"
        )
        cancelled = self.activity.cancel_check()

        run_in_background(
            self.master,
            lambda: self.controller.ramp_to_frequency(
                target, config, cancel_check=cancelled, status=self.status
            ),
            lambda _r, error: self._on_autotune_done(target, src_config, src_temp, error),
            name="autotune",
        )

    def _on_autotune_done(
        self, target: float, src_config: str, src_temp: int,
        error: Optional[BaseException],
    ) -> None:
        self._tuning = False
        self.activity.release(RigOwner.MANUAL)
        if error is not None:
            messagebox.showerror("Autotune Error", f"Autotune failed: {error}")
            self.status("Autotune failed.")
        else:
            show_temporary_popup(
                self.master,
                f"Tuned frequency {int(target)} Hz applied\n({src_config} @ {src_temp}°C)",
                duration_ms=2000,
            )
            self.status(
                f"Tuned frequency {int(target)} Hz applied "
                f"from {src_config} @ {src_temp}°C"
            )
        self.refresh()
