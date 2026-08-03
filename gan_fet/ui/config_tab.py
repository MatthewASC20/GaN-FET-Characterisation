"""Configuration tab: parameter option editors, instrument addresses,
and SMU/safety limits."""

from __future__ import annotations

import math
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from gan_fet.settings import (
    SCOPE_INSTRUMENT_KEY,
    InstrumentAddress,
    Settings,
)
from gan_fet.transport.address import AddressError, parse_address


def options_without_indices(
    options: List[Tuple[Any, str]], selected_indices: Iterable[int]
) -> List[Tuple[Any, str]]:
    """Return ``options`` with every selected listbox row removed."""

    selected = {int(index) for index in selected_indices}
    return [option for index, option in enumerate(options) if index not in selected]


def validate_instrument_target(target_raw: str, port_raw: str) -> tuple[str, int]:
    """Validate the persisted transport-neutral target and port/baud pair."""

    target = target_raw.strip()
    if not target:
        raise ValueError("target/resource is required")
    try:
        port = int(port_raw.strip(), 10)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("port/baud must be an integer") from exc

    is_uri = "://" in target
    is_visa = target.upper().startswith("GPIB") or "::" in target
    is_serial = target.upper().startswith("COM") or target.startswith("/")
    if is_uri or is_visa:
        valid_port = 0 <= port <= 4_000_000
        range_text = "between 0 and 4000000"
    elif is_serial:
        valid_port = 0 < port <= 4_000_000
        range_text = "between 1 and 4000000"
    else:
        valid_port = 0 < port <= 65_535
        range_text = "between 1 and 65535"
    if not valid_port:
        raise ValueError(f"port/baud must be {range_text} for {target!r}")
    try:
        parse_address(target, port)
    except AddressError as exc:
        raise ValueError(str(exc)) from exc
    return target, port



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
        list_frame = ttk.Frame(self)
        list_frame.grid(row=0, column=0, columnspan=2, sticky="nsew", padx=5, pady=(5, 2))

        self.listbox = tk.Listbox(
            list_frame,
            height=4,
            exportselection=False,
            selectmode=tk.EXTENDED,
            font=("Consolas", 10),
        )
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scrollbar.set)

        self.listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.listbox.bind("<<ListboxSelect>>", self._on_select)
        self.listbox.bind("<Delete>", lambda _e: self._delete())

        entry_frame = ttk.Frame(self)
        entry_frame.grid(row=1, column=0, columnspan=2, sticky="ew", padx=5, pady=2)

        self.value_entry = ttk.Entry(entry_frame)
        self.value_entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.value_entry.bind("<Return>", lambda _e: self._add_or_update())

        ttk.Button(entry_frame, text="+ Add", width=7, command=self._add_or_update).pack(side="right")

        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=2, column=0, columnspan=2, sticky="ew", padx=5, pady=(2, 5))

        ttk.Button(btn_frame, text="Delete Selected", command=self._delete).pack(side="left", fill="x", expand=True, padx=(0, 2))
        ttk.Button(btn_frame, text="Reset Defaults", command=self._reset).pack(side="right", fill="x", expand=True, padx=(2, 0))

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

    def set_options(self, options: List[Tuple[Any, str]]) -> None:
        self.options = list(options)
        self.listbox.delete(0, tk.END)
        for value, _label in self.options:
            self.listbox.insert(tk.END, str(value))
        self.value_entry.delete(0, tk.END)

    def _add_or_update(self) -> None:
        raw = self.value_entry.get().strip()
        if not raw:
            messagebox.showerror("Input Error", "Please provide a value.", parent=self)
            return
        try:
            value = self.value_parser(raw)
        except Exception:
            messagebox.showerror("Input Error", f"Invalid value: {raw}", parent=self)
            return
        label = str(self.default_label_factory(value))

        merged = [
            (option_value, option_label)
            for option_value, option_label in self.options
            if option_value != value
        ] + [(value, label)]
        normalized = self.on_change(merged)
        if normalized is not None:
            self.set_options(normalized)

    def _delete(self) -> None:
        selection = self.listbox.curselection()
        if not selection:
            messagebox.showerror("Selection Error", "Please select an entry to delete.", parent=self)
            return
        remaining = options_without_indices(self.options, selection)
        normalized = self.on_change(remaining)
        if normalized is not None:
            self.set_options(normalized)

    def _reset(self) -> None:
        normalized = self.on_change(list(self.default_options))
        if normalized is not None:
            self.set_options(normalized)

    def _on_select(self, _event) -> None:
        selection = self.listbox.curselection()
        if not selection:
            return
        value = self.options[selection[0]][0]
        self.value_entry.delete(0, tk.END)
        self.value_entry.insert(0, str(value))


class InstrumentConfigEditor(ttk.Frame):
    """Transport-neutral instrument targets, one per instrument.

    A Prologix bridge is part of the target itself
    (``prologix+tcp://host:port?addr=N``), never a separate field.
    """

    def __init__(self, master, settings: Settings, on_applied: Callable[[], None]):
        super().__init__(master)
        self.settings = settings
        self.on_applied = on_applied
        self.instrument_entries: Dict[str, Dict[str, ttk.Entry]] = {}
        self._build()

    def _build(self) -> None:
        connections = ttk.LabelFrame(self, text="Instrument Connections")
        connections.pack(fill="x", expand=True, padx=2, pady=(0, 6))

        ttk.Label(
            connections, text="Instrument", font=("Helvetica", 9, "bold")
        ).grid(
            row=0, column=0, padx=5, pady=(5, 2), sticky="w"
        )
        ttk.Label(
            connections, text="Target / Resource", font=("Helvetica", 9, "bold")
        ).grid(
            row=0, column=1, padx=5, pady=(5, 2), sticky="w"
        )
        ttk.Label(
            connections, text="Port / Baud", font=("Helvetica", 9, "bold")
        ).grid(
            row=0, column=2, padx=5, pady=(5, 2), sticky="w"
        )

        scope_address = self.settings.instruments.get(SCOPE_INSTRUMENT_KEY)
        if scope_address is None:
            raise ValueError(
                "No address is configured for the Teledyne LeCroy HDO4054"
            )
        rows = [
            (
                SCOPE_INSTRUMENT_KEY,
                "Teledyne LeCroy HDO4054",
                scope_address,
            ),
            *(
                (name, name, address)
                for name, address in self.settings.instruments.items()
                if name != SCOPE_INSTRUMENT_KEY
            ),
        ]
        for row, (name, label, addr) in enumerate(rows, start=1):
            ttk.Label(connections, text=label).grid(
                row=row, column=0, padx=5, pady=2, sticky="w"
            )
            target_entry = ttk.Entry(connections)
            target_entry.insert(0, addr.ip)
            target_entry.grid(row=row, column=1, padx=5, pady=2, sticky="ew")
            port_entry = ttk.Entry(connections, width=10)
            port_entry.insert(0, str(addr.port))
            port_entry.grid(row=row, column=2, padx=5, pady=2, sticky="ew")
            self.instrument_entries[name] = {
                "target": target_entry,
                "port": port_entry,
            }

        help_row = len(self.instrument_entries) + 1
        ttk.Label(
            connections,
            text=(
                "Targets may be hostnames/IPs, VISA resources, serial devices, or "
                "tcp://, visa://, serial:// and prologix+ transport URIs. The "
                "HDO4054 requires an LXI/VXI-11 VISA resource with port 0."
            ),
            wraplength=540,
            justify="left",
        ).grid(
            row=help_row,
            column=0,
            columnspan=3,
            padx=5,
            pady=(4, 6),
            sticky="w",
        )
        connections.grid_columnconfigure(1, weight=1)


        ttk.Button(self, text="Apply Instrument Changes", command=self._apply).pack(
            fill="x", padx=2, pady=5
        )

    def _apply(self) -> None:
        edited_addresses: Dict[str, InstrumentAddress] = {}
        for name, fields in self.instrument_entries.items():
            try:
                target, port = validate_instrument_target(
                    fields["target"].get(), fields["port"].get()
                )
            except ValueError as exc:
                messagebox.showerror(
                    "Validation Error", f"{name}: {exc}", parent=self
                )
                return
            edited_addresses[name] = InstrumentAddress(target, port)

        pending = edited_addresses


        previous_instruments = self.settings.instruments
        self.settings.instruments = pending
        try:
            self.settings.save()
        except Exception as exc:
            self.settings.instruments = previous_instruments
            messagebox.showerror(
                "Save Error", f"Could not save instrument settings: {exc}", parent=self
            )
            return

        self.on_applied()
        messagebox.showinfo(
            "Instrument Settings",
            "Connection changes were saved and will take effect after restarting the application.",
            parent=self,
        )


class SmuLimitsEditor(ttk.LabelFrame):
    """SMU envelope + safety limits. Numeric fields mapped onto Settings."""

    FIELDS: list[tuple[str, str, str, Callable[[str], float]]] = [
        ("SMU max voltage (V)", "smu", "max_voltage_v", float),
        ("Current compliance (A)", "smu", "current_compliance_a", float),
        ("NPLC", "smu", "nplc", float),
        ("Sample interval (s)", "smu", "sample_interval_s", float),
        ("Ramp step (V)", "smu", "ramp_step_v", float),
        ("DC voltage tune window (± V)", "voltage_tune", "window_v", float),
        ("DC voltage tune step (V)", "voltage_tune", "step_v", float),
        ("Max DC current (A)", "safety", "max_dc_current_a", float),
        ("Max Vds peak (V)", "safety", "max_vds_peak_v", float),
        ("Peak tolerance (V)", "peak_control", "tolerance_v", float),
    ]

    def __init__(self, master, settings: Settings, on_applied: Callable[[], None]):
        super().__init__(master, text="SMU & Safety Limits")
        self.settings = settings
        self.on_applied = on_applied
        self.entries: list[
            tuple[ttk.Entry, str, str, Callable[[str], float]]
        ] = []

        for row, (label, section, attr, cast) in enumerate(self.FIELDS):
            ttk.Label(self, text=label).grid(row=row, column=0, padx=5, pady=2, sticky="w")
            entry = ttk.Entry(self, width=12)
            value = getattr(getattr(settings, section), attr)
            entry.insert(0, "" if value is None else str(value))
            entry.grid(row=row, column=1, padx=5, pady=2, sticky="ew")
            self.entries.append((entry, section, attr, cast))

        ttk.Button(self, text="Apply Limit Changes", command=self._apply).grid(
            row=len(self.FIELDS), column=0, columnspan=2, padx=5, pady=(8, 5), sticky="ew",
        )
        self.grid_columnconfigure(1, weight=1)

    def _apply(self) -> None:
        pending: list[tuple[str, str, Any]] = []
        for entry, section, attr, cast in self.entries:
            raw = entry.get().strip()
            try:
                value = cast(raw)
            except (TypeError, ValueError):
                messagebox.showerror("Validation Error", f"Invalid value for {attr}: {raw!r}", parent=self)
                return
            if not math.isfinite(value) or value <= 0:
                messagebox.showerror(
                    "Validation Error",
                    f"{attr} must be a finite, positive number.",
                    parent=self,
                )
                return
            pending.append((section, attr, value))

        for section, attr, value in pending:
            setattr(getattr(self.settings, section), attr, value)
        try:
            self.settings.save()
        except Exception as exc:
            messagebox.showerror(
                "Save Error", f"Could not save limit settings: {exc}", parent=self
            )
            return
        self.on_applied()


class RampRatesSliderEditor(ttk.LabelFrame):
    """Stage ramp-rate changes and persist them with one explicit Apply."""

    VOLTAGE_RANGE = (1.0, 200.0)
    FREQUENCY_RANGE = (10.0, 2000.0)
    DUTY_RANGE = (1.0, 100.0)

    def __init__(self, master, settings: Settings, on_applied: Callable[[], None]):
        super().__init__(master, text="Ramp Rate Sliders")
        self.settings = settings
        self.on_applied = on_applied
        self._dirty = False

        # Voltage Ramp Rate (V/s)
        ttk.Label(self, text="DC Voltage Ramp Rate (V/s):").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        current_v_rate = getattr(self.settings.smu, "ramp_rate_v_s", 25.0)
        self.v_rate_var = tk.DoubleVar(value=current_v_rate)
        self.v_label = ttk.Label(self, text=f"{current_v_rate:.1f} V/s", width=12)
        self.v_label.grid(row=0, column=2, padx=5, pady=5)

        self.v_slider = ttk.Scale(
            self,
            from_=self.VOLTAGE_RANGE[0],
            to=self.VOLTAGE_RANGE[1],
            variable=self.v_rate_var,
            orient="horizontal",
            command=self._on_v_slider,
        )
        self.v_slider.grid(row=0, column=1, sticky="ew", padx=5, pady=5)

        # Frequency Autotune Ramp Rate (kHz/s)
        ttk.Label(self, text="Frequency Ramp Rate (kHz/s):").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        current_freq_rate = getattr(self.settings.voltage_tune, "autotune_freq_rate_khz_s", 200.0)
        self.freq_rate_var = tk.DoubleVar(value=current_freq_rate)
        self.freq_label = ttk.Label(self, text=f"{current_freq_rate:.0f} kHz/s", width=12)
        self.freq_label.grid(row=1, column=2, padx=5, pady=5)

        self.freq_slider = ttk.Scale(
            self,
            from_=self.FREQUENCY_RANGE[0],
            to=self.FREQUENCY_RANGE[1],
            variable=self.freq_rate_var,
            orient="horizontal",
            command=self._on_freq_slider,
        )
        self.freq_slider.grid(row=1, column=1, sticky="ew", padx=5, pady=5)

        # Duty Cycle Ramp Rate (%/s)
        ttk.Label(self, text="Duty Cycle Ramp Rate (%/s):").grid(row=2, column=0, sticky="w", padx=5, pady=5)
        current_duty_rate = getattr(
            self.settings.wavegen, "duty_ramp_rate_pct_s", 10.0
        )
        self.duty_rate_var = tk.DoubleVar(value=current_duty_rate)
        self.duty_label = ttk.Label(self, text=f"{current_duty_rate:.1f} %/s", width=12)
        self.duty_label.grid(row=2, column=2, padx=5, pady=5)

        self.duty_slider = ttk.Scale(
            self,
            from_=self.DUTY_RANGE[0],
            to=self.DUTY_RANGE[1],
            variable=self.duty_rate_var,
            orient="horizontal",
            command=self._on_duty_slider,
        )
        self.duty_slider.grid(row=2, column=1, sticky="ew", padx=5, pady=5)

        self.apply_button = ttk.Button(
            self,
            text="Apply Ramp Rates",
            command=self._apply,
            state="disabled",
        )
        self.apply_button.grid(
            row=3, column=0, columnspan=3, sticky="ew", padx=5, pady=(6, 5)
        )

        self.grid_columnconfigure(1, weight=1)

    @classmethod
    def normalize_rates(
        cls, voltage_v_s: Any, frequency_khz_s: Any, duty_pct_s: Any
    ) -> tuple[float, float, float]:
        """Validate and round one staged slider selection."""

        values = (
            (float(voltage_v_s), cls.VOLTAGE_RANGE, 1, "voltage ramp rate"),
            (float(frequency_khz_s), cls.FREQUENCY_RANGE, 0, "frequency ramp rate"),
            (float(duty_pct_s), cls.DUTY_RANGE, 1, "duty ramp rate"),
        )
        normalized: list[float] = []
        for value, (minimum, maximum), digits, label in values:
            if not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValueError(
                    f"{label} must be between {minimum:g} and {maximum:g}"
                )
            normalized.append(float(round(value, digits)))
        return normalized[0], normalized[1], normalized[2]

    def _mark_dirty(self) -> None:
        self._dirty = True
        if hasattr(self, "apply_button"):
            self.apply_button.config(state="normal")

    def _on_v_slider(self, val: str) -> None:
        try:
            v = float(val)
            self.v_label.config(text=f"{v:.1f} V/s")
        except ValueError:
            return
        self._mark_dirty()

    def _on_freq_slider(self, val: str) -> None:
        try:
            f = float(val)
            self.freq_label.config(text=f"{f:.0f} kHz/s")
        except ValueError:
            return
        self._mark_dirty()

    def _on_duty_slider(self, val: str) -> None:
        try:
            d = float(val)
            self.duty_label.config(text=f"{d:.1f} %/s")
        except ValueError:
            return
        self._mark_dirty()

    def _apply(self) -> None:
        try:
            voltage, frequency, duty = self.normalize_rates(
                self.v_rate_var.get(),
                self.freq_rate_var.get(),
                self.duty_rate_var.get(),
            )
        except (TypeError, ValueError, tk.TclError) as exc:
            messagebox.showerror(
                "Validation Error", f"Could not apply ramp rates: {exc}", parent=self
            )
            return

        updates = [
            (self.settings.smu, "ramp_rate_v_s", voltage),
            (self.settings.voltage_tune, "autotune_freq_rate_khz_s", frequency),
            (self.settings.wavegen, "freq_ramp_rate_khz_s", frequency),
            (self.settings.wavegen, "duty_ramp_rate_pct_s", duty),
        ]
        if hasattr(self.settings.voltage_tune, "duty_ramp_rate_pct_s"):
            updates.append((self.settings.voltage_tune, "duty_ramp_rate_pct_s", duty))

        previous = [(section, attr, getattr(section, attr)) for section, attr, _ in updates]
        for section, attr, value in updates:
            setattr(section, attr, value)
        try:
            self.settings.save()
        except Exception as exc:
            for section, attr, value in previous:
                setattr(section, attr, value)
            messagebox.showerror(
                "Save Error", f"Could not save ramp-rate settings: {exc}", parent=self
            )
            return

        self._dirty = False
        self.apply_button.config(state="disabled")
        self.on_applied()
