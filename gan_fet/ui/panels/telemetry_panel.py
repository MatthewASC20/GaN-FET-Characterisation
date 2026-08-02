"""Live telemetry cards.

Owns its labels and their updates. Callers pass values; resolving where a value
comes from when none is supplied stays with the window, which is what knows the
instruments.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Optional

from gan_fet.ui.telemetry_format import (
    format_amps,
    format_frequency,
    format_milliamps,
    format_volts,
)

#: (attribute, heading) for each card, in display order. Ordered so the two
#: controlled quantities — frequency and Vds peak — read first.
TELEMETRY_CARDS: tuple[tuple[str, str], ...] = (
    ("frequency", "Frequency (f_sw)"),
    ("vds_peak", "Peak Voltage (V_ds,pk)"),
    ("dc_voltage", "DC Voltage (V_dc)"),
    ("dc_current", "DC Current (I_dc)"),
    ("rms_current", "Load Current (I_rms)"),
    ("isw_rms", "Switch Current (I_sw,rms)"),
)


class TelemetryPanel(ttk.LabelFrame):
    """A row of readouts showing the rig's current operating point."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, text="Live Telemetry & Meters")
        self._labels: dict[str, ttk.Label] = {}
        for column, (attr, heading) in enumerate(TELEMETRY_CARDS):
            card = ttk.Frame(self, padding=6, relief="ridge")
            card.grid(row=0, column=column, padx=6, pady=6, sticky="nsew")
            ttk.Label(card, text=heading, font=("Helvetica", 8, "bold")).pack(
                anchor="w"
            )
            value = ttk.Label(card, text="", font=("Consolas", 13, "bold"))
            value.pack(anchor="e", pady=(4, 0))
            self._labels[attr] = value
            self.grid_columnconfigure(column, weight=1)
        self.clear()

    def clear(self) -> None:
        """Blank every card."""
        for attr, formatter in (
            ("frequency", format_frequency),
            ("vds_peak", format_volts),
            ("dc_voltage", format_volts),
            ("dc_current", format_milliamps),
            ("rms_current", format_amps),
            ("isw_rms", format_amps),
        ):
            self._labels[attr].config(text=formatter(None))

    def update_values(
        self,
        *,
        freq_hz: Optional[float] = None,
        vds_peak: Optional[float] = None,
        dc_volts: Optional[float] = None,
        dc_current_a: Optional[float] = None,
        rms_current_a: Optional[float] = None,
        isw_rms_a: Optional[float] = None,
    ) -> None:
        """Render the supplied readings, leaving the rest as they are.

        ``None`` means "no new reading", not "no reading". Several of these are
        captured once per run rather than polled — Vds peak and the RMS
        currents among them — so blanking a card because this particular
        refresh had nothing new would erase a measurement that is still the
        latest one taken. ``clear()`` is the way to blank deliberately.
        """
        for attr, value, formatter in (
            ("frequency", freq_hz, format_frequency),
            ("vds_peak", vds_peak, format_volts),
            ("dc_voltage", dc_volts, format_volts),
            ("dc_current", dc_current_a, format_milliamps),
            ("rms_current", rms_current_a, format_amps),
            ("isw_rms", isw_rms_a, format_amps),
        ):
            if value is not None:
                self._labels[attr].config(text=formatter(value))
