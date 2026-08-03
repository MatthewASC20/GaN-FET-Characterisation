"""The device selector row: name entry, dropdown, add and remove.

Owns the three widgets and their enabled state. What happens when a device is
committed, added or removed stays with the window — those touch the database,
the tracker, the planner and the Sheets mirror — so this panel takes them as
callbacks and does no work of its own.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Sequence

from gan_fet.ui.widgets import RigControlState, set_widget_enabled


class DeviceBar:
    """The device row, gridded into a caller-owned frame.

    Not a widget subclass: the row shares its grid with the rest of the
    experiment controls, so its cells have to land in the parent's geometry
    rather than in a frame of its own.
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        row: int,
        textvariable: tk.StringVar,
        devices: Sequence[str],
        on_commit: Callable[..., None],
        on_add: Callable[[], None],
        on_remove: Callable[[], None],
    ) -> None:
        tk.Label(parent, text="Device Name:").grid(
            row=row, column=0, sticky="e", padx=5, pady=5
        )
        self.dropdown = ttk.Combobox(
            parent,
            textvariable=textvariable,
            values=list(devices),
            width=20,
            state="normal",
        )
        self.dropdown.grid(row=row, column=1, sticky="w", padx=5, pady=5)
        # Committing on focus-out and Return as well as selection: the box is
        # editable, so a typed name that is never "selected" still has to take.
        for event in ("<<ComboboxSelected>>", "<FocusOut>", "<Return>"):
            self.dropdown.bind(event, on_commit)

        buttons = ttk.Frame(parent)
        buttons.grid(row=row, column=2, sticky="w", padx=(2, 5), pady=5)

        self.add_button = ttk.Button(
            buttons, text="+ Add Device", command=on_add, width=12
        )
        self.add_button.pack(side="left", padx=(0, 2))

        self.remove_button = ttk.Button(
            buttons, text="- Remove Device", command=on_remove, width=14
        )
        self.remove_button.pack(side="left", padx=(2, 0))

    def set_devices(self, devices: Sequence[str]) -> None:
        """Replace the dropdown's list without disturbing the current text."""
        self.dropdown["values"] = list(devices)

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable every control in the row together."""
        for widget in (self.dropdown, self.add_button, self.remove_button):
            set_widget_enabled(widget, enabled)

    def apply_control_state(self, controls: RigControlState) -> None:
        """Enable or disable the row for one snapshot of rig state.

        The whole row moves together: choosing a different device mid-run would
        change which device the samples are being recorded against, so there is
        no state in which selecting is safe but adding or removing is not.
        """
        self.set_enabled(controls.edit_inputs)
