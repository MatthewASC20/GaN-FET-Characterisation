"""Configuration tab: parameter option editors, instrument addresses and
SMU/safety limits."""

from __future__ import annotations

import functools
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from gan_fet.core import param_options
from gan_fet.settings import Settings


class ParameterListEditor(ttk.LabelFrame):
    """Edit one option list (values with derived labels)."""

    def __init__(
        self,
        master,
        title: str,
        initial_options: List[Tuple[Any, str]],
        on_change: Callable[[List[Tuple[Any, str]]], Optional[List[Tuple[Any, str]]]],
        *,
        value_parser: Callable[[str], Any],
        default_label_factory: Callable[[Any], str],
        default_options: List[Tuple[Any, str]],
    ):
        super().__init__(master, text=title)
        self.on_change = on_change
        self.value_parser = value_parser
        self.default_label_factory = default_label_factory
        self.default_options = list(default_options)
        self.options: List[Tuple[Any, str]] = []

        self._build_widgets()
        self.set_options(initial_options)

    def _build_widgets(self) -> None:
        self.tree = ttk.Treeview(self, columns=("value",), show="headings", height=6)
        self.tree.heading("value", text="Value")
        self.tree.column("value", anchor="center", width=160)
        self.tree.grid(row=0, column=0, columnspan=3, sticky="nsew", padx=5, pady=5)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        ttk.Label(self, text="Value").grid(row=1, column=0, sticky="w", padx=5)
        self.value_entry = ttk.Entry(self)
        self.value_entry.grid(row=2, column=0, columnspan=2, sticky="ew", padx=5, pady=(0, 5))

        ttk.Button(self, text="Add / Update", command=self._add_or_update).grid(
            row=2, column=2, sticky="ew", padx=5, pady=(0, 5))
        ttk.Button(self, text="Delete Selected", command=self._delete).grid(
            row=3, column=0, sticky="ew", padx=5, pady=(0, 5))
        ttk.Button(self, text="Reset to Defaults", command=self._reset).grid(
            row=3, column=1, sticky="ew", padx=5, pady=(0, 5))
        ttk.Button(self, text="Clear Field",
                   command=lambda: self.value_entry.delete(0, tk.END)).grid(
            row=3, column=2, sticky="ew", padx=5, pady=(0, 5))

        for col in range(3):
            self.grid_columnconfigure(col, weight=1 if col < 2 else 0)
        self.grid_rowconfigure(0, weight=1)

    def set_options(self, options: List[Tuple[Any, str]]) -> None:
        self.options = list(options)
        self.tree.delete(*self.tree.get_children())
        for value, _label in self.options:
            self.tree.insert("", "end", values=(value,))
        self.value_entry.delete(0, tk.END)

    def _add_or_update(self) -> None:
        raw = self.value_entry.get().strip()
        if not raw:
            messagebox.showerror("Input Error", "Please provide a value.")
            return
        try:
            value = self.value_parser(raw)
        except Exception:
            messagebox.showerror("Input Error", f"Invalid value: {raw}")
            return
        label = str(self.default_label_factory(value))

        merged = [(v, l) for v, l in self.options if v != value] + [(value, label)]
        normalized = self.on_change(merged)
        if normalized is not None:
            self.set_options(normalized)

    def _delete(self) -> None:
        selected = self.tree.selection()
        if not selected:
            messagebox.showerror("Selection Error", "Please select an entry to delete.")
            return
        selected_values = {self.tree.item(item, "values")[0] for item in selected}
        remaining = [(v, l) for v, l in self.options if str(v) not in selected_values]
        normalized = self.on_change(remaining)
        if normalized is not None:
            self.set_options(normalized)

    def _reset(self) -> None:
        normalized = self.on_change(list(self.default_options))
        if normalized is not None:
            self.set_options(normalized)

    def _on_select(self, _event) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        (value,) = self.tree.item(selection[0], "values")
        self.value_entry.delete(0, tk.END)
        self.value_entry.insert(0, value)


class InstrumentConfigEditor(ttk.LabelFrame):
    """IP/port per instrument, applied to Settings (and saved)."""

    def __init__(self, master, settings: Settings, on_applied: Callable[[], None]):
        super().__init__(master, text="Instrument Connections")
        self.settings = settings
        self.on_applied = on_applied
        self.entries: Dict[str, Dict[str, ttk.Entry]] = {}
        self._build()

    def _build(self) -> None:
        ttk.Label(self, text="Instrument").grid(row=0, column=0, padx=5, pady=(5, 2), sticky="w")
        ttk.Label(self, text="IP Address").grid(row=0, column=1, padx=5, pady=(5, 2), sticky="w")
        ttk.Label(self, text="Port").grid(row=0, column=2, padx=5, pady=(5, 2), sticky="w")

        for row, (name, addr) in enumerate(self.settings.instruments.items(), start=1):
            ttk.Label(self, text=name).grid(row=row, column=0, padx=5, pady=2, sticky="w")
            ip_entry = ttk.Entry(self)
            ip_entry.insert(0, addr.ip)
            ip_entry.grid(row=row, column=1, padx=5, pady=2, sticky="ew")
            port_entry = ttk.Entry(self, width=8)
            port_entry.insert(0, str(addr.port))
            port_entry.grid(row=row, column=2, padx=5, pady=2, sticky="ew")
            self.entries[name] = {"ip": ip_entry, "port": port_entry}

        ttk.Button(self, text="Apply Changes", command=self._apply).grid(
            row=len(self.entries) + 1, column=0, columnspan=3,
            padx=5, pady=(10, 5), sticky="ew",
        )
        self.grid_columnconfigure(1, weight=1)

    def _apply(self) -> None:
        for name, fields in self.entries.items():
            ip = fields["ip"].get().strip()
            if not ip:
                messagebox.showerror("Validation Error", f"IP address is required for {name}.")
                return
            try:
                port = int(fields["port"].get().strip(), 10)
            except ValueError:
                messagebox.showerror("Validation Error", f"Port must be a number for {name}.")
                return
            if not (0 < port <= 65535):
                messagebox.showerror("Validation Error", f"Port out of range for {name}.")
                return
            self.settings.instruments[name].ip = ip
            self.settings.instruments[name].port = port
        self.settings.save()
        self.on_applied()


class SmuLimitsEditor(ttk.LabelFrame):
    """SMU envelope + safety limits. Numeric fields mapped onto Settings."""

    FIELDS = [
        ("SMU max voltage (V)", "smu", "max_voltage_v", float),
        ("Current compliance (A)", "smu", "current_compliance_a", float),
        ("NPLC", "smu", "nplc", float),
        ("Sample interval (s)", "smu", "sample_interval_s", float),
        ("Ramp step (V)", "smu", "ramp_step_v", float),
        ("Ramp delay (s)", "smu", "ramp_delay_s", float),
        ("Prologix GPIB addr (blank = transparent bridge)", "smu", "prologix_gpib_addr", "int_or_none"),
        ("ZVS search window (± V)", "zvs", "window_v", float),
        ("ZVS step (V)", "zvs", "step_v", float),
        ("Max DC current (A)", "safety", "max_dc_current_a", float),
        ("Max Vds peak (V)", "safety", "max_vds_peak_v", float),
        ("Peak tolerance (V)", "peak_control", "tolerance_v", float),
    ]

    def __init__(self, master, settings: Settings, on_applied: Callable[[], None]):
        super().__init__(master, text="SMU / Safety Limits")
        self.settings = settings
        self.on_applied = on_applied
        self.entries: list[tuple[ttk.Entry, str, str, Any]] = []

        for row, (label, section, attr, cast) in enumerate(self.FIELDS):
            ttk.Label(self, text=label).grid(row=row, column=0, padx=5, pady=2, sticky="w")
            entry = ttk.Entry(self, width=12)
            value = getattr(getattr(settings, section), attr)
            entry.insert(0, "" if value is None else str(value))
            entry.grid(row=row, column=1, padx=5, pady=2, sticky="ew")
            self.entries.append((entry, section, attr, cast))

        ttk.Button(self, text="Apply Changes", command=self._apply).grid(
            row=len(self.FIELDS), column=0, columnspan=2, padx=5, pady=(10, 5), sticky="ew",
        )
        self.grid_columnconfigure(1, weight=1)

    def _apply(self) -> None:
        for entry, section, attr, cast in self.entries:
            raw = entry.get().strip()
            try:
                if cast == "int_or_none":
                    value = int(raw) if raw else None
                else:
                    value = cast(raw)
            except ValueError:
                messagebox.showerror("Validation Error", f"Invalid value for {attr}: {raw!r}")
                return
            setattr(getattr(self.settings, section), attr, value)
        self.settings.save()
        self.on_applied()


EDITOR_TITLES = {
    "configurations": "Configurations",
    "frequencies": "Frequencies (Hz)",
    "duties": "Duty Cycles (%)",
    "temperatures": "Temperatures (°C)",
    "voltages": "Voltages (V)",
}


class ConfigurationTab(ttk.Frame):
    """The whole Configuration tab: one option editor per parameter key plus
    the global instrument/limit editors."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        settings: Settings,
        options: Mapping[str, Sequence[param_options.Option]],
        defaults: Mapping[str, Sequence[param_options.Option]],
        on_options_changed: Callable[
            [str, List[param_options.Option]], Optional[List[param_options.Option]]
        ],
        on_settings_applied: Callable[[], None],
    ):
        super().__init__(master)
        self.editors: Dict[str, ParameterListEditor] = {}

        ttk.Label(
            self,
            text=(
                "Per-device parameter options (stored in the database) and global "
                "instrument/safety settings (stored in settings.json)."
            ),
            wraplength=800,
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(10, 5))

        editors_frame = ttk.Frame(self)
        editors_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        for col in range(2):
            editors_frame.grid_columnconfigure(col, weight=1, uniform="cfg")

        for idx, key in enumerate(param_options.OPTION_KEYS):
            editor = ParameterListEditor(
                editors_frame,
                EDITOR_TITLES[key],
                list(options[key]),
                on_change=functools.partial(self._changed, key, on_options_changed),
                value_parser=functools.partial(param_options.parse_value, key),
                default_label_factory=functools.partial(param_options.default_label, key),
                default_options=list(defaults[key]),
            )
            editor.grid(row=idx // 2, column=idx % 2, sticky="nsew", padx=10, pady=10)
            self.editors[key] = editor

        side = ttk.Frame(self)
        side.grid(row=1, column=1, sticky="nsew", padx=10, pady=(0, 10))
        InstrumentConfigEditor(side, settings, on_settings_applied).pack(
            fill="x", pady=(0, 10)
        )
        SmuLimitsEditor(side, settings, on_settings_applied).pack(fill="x")

        self.grid_columnconfigure(0, weight=3)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

    @staticmethod
    def _changed(key, handler, options):
        return handler(key, list(options))

    def set_options(self, options: Mapping[str, Sequence[param_options.Option]]) -> None:
        for key, editor in self.editors.items():
            if key in options:
                editor.set_options(list(options[key]))
