"""The run-setup row: duration, the ZVS sweep toggle, and the two rig buttons.

Owns construction, layout and the one piece of layout logic that is genuinely
its own — whether the voltage-only ZVS checkbox is on screen at all. Everything
these buttons do belongs to the window.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from gan_fet.ui.telemetry_format import last_current_text
from gan_fet.ui.widgets import ColorButton

#: Shared geometry for the two rig buttons, which are sized to hold their
#: longest label ("Apply Wavegen Settings", "Autotune Unavailable").
_RIG_BUTTON = {
    "width": 170,
    "height": 36,
    "borderless": 1,
    "highlightthickness": 1,
}


class RunControls:
    """Duration, ZVS toggle, last-current readout and the two rig buttons.

    Not a widget subclass: these span two rows of the experiment grid and have
    to land in the parent's geometry rather than in a frame of their own.
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        options_row: int,
        buttons_row: int,
        default_duration: str,
        find_zvs_var: tk.BooleanVar,
        # Typed as tkinter types ``command``: the handlers take optional
        # arguments the button never supplies, and return values it ignores.
        on_apply_wavegen: Callable[..., object],
        on_autotune: Callable[..., object],
    ) -> None:
        self._options_row = options_row
        self._find_zvs_var = find_zvs_var

        ttk.Label(parent, text="Duration (min):").grid(
            row=options_row, column=0, sticky="e", padx=5, pady=5
        )
        self.duration_entry = ttk.Entry(parent, width=10)
        self.duration_entry.insert(0, default_duration)
        self.duration_entry.grid(row=options_row, column=1, sticky="w")

        # Frequency tuning is on by default and lives in Config > Advanced:
        # it is part of establishing the operating point, not a per-run choice.
        # The voltage-only ZVS sweep is kept mainly to exercise the frequency
        # search independently, so it stays hidden unless revealed in Config.
        self.find_zvs_checkbox = ttk.Checkbutton(
            parent, text="Find ZVS before run", variable=find_zvs_var
        )

        self.last_current_label = ttk.Label(parent, text=last_current_text(None))
        self.last_current_label.grid(
            row=buttons_row, column=0, sticky="w", padx=5, pady=5
        )

        self.confirm_button = ColorButton(
            parent,
            text="Apply Wavegen Settings",
            command=on_apply_wavegen,
            **_RIG_BUTTON,
        )
        self.confirm_button.grid(
            row=buttons_row, column=1, sticky="w", padx=5, pady=5
        )

        self.autotune_button = ColorButton(
            parent,
            text="Autotune Unavailable",
            command=on_autotune,
            **_RIG_BUTTON,
        )
        self.autotune_button.grid(
            row=buttons_row, column=2, sticky="w", padx=5, pady=5
        )

    def set_zvs_sweep_visible(self, visible: bool) -> None:
        """Show or hide the voltage-only ZVS control.

        Hiding it clears it. A control the operator cannot see must not keep
        silently steering the run.
        """
        if visible:
            self.find_zvs_checkbox.grid(
                row=self._options_row, column=2, sticky="w", padx=5
            )
        else:
            self.find_zvs_checkbox.grid_remove()
            self._find_zvs_var.set(False)

    def set_duration(self, minutes: str) -> None:
        """Replace the duration field, as the voltage default changes."""
        self.duration_entry.delete(0, tk.END)
        self.duration_entry.insert(0, minutes)

    def set_last_current(self, amps: float | None) -> None:
        self.last_current_label.config(text=last_current_text(amps))
