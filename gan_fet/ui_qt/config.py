"""Qt configuration pane: instrument connections and SMU limits.

Reuses the toolkit-free validators from the Tk configuration tab —
``validate_instrument_target`` is the single authority on what a persisted
instrument target may look like — so the two front-ends can never drift
apart on what they accept.

Both Apply paths follow the Tk tab's validate-swap-save-rollback shape:
``Settings.save()`` can raise (disk full, scope-configuration rejection),
and a half-applied settings object would misreport the rig to every later
reader, so the previous values are restored before the failure is reported.
"""

from __future__ import annotations

import math
from typing import Callable

from PyQt6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gan_fet.settings import (
    SCOPE_INSTRUMENT_KEY,
    InstrumentAddress,
    Settings,
)
from gan_fet.ui.config_tab import validate_instrument_target

#: SMU limits exposed for editing. Deliberately the safety-relevant head of
#: the Tk ``SmuLimitsEditor.FIELDS`` list; every attribute lives on
#: ``settings.smu``.
_SMU_LIMIT_FIELDS: tuple[tuple[str, str], ...] = (
    ("SMU max voltage (V)", "max_voltage_v"),
    ("Current compliance (A)", "current_compliance_a"),
    ("NPLC", "nplc"),
)

_TARGET_HELP = (
    "Targets may be hostnames/IPs, VISA resources, serial devices, or "
    "tcp://, visa://, serial:// and prologix+ transport URIs. The "
    "HDO4054 requires an LXI/VXI-11 VISA resource with port 0."
)


class ConfigPane(QWidget):
    """Instrument targets and SMU limits with explicit Apply semantics.

    Nothing is persisted on keystrokes: each section stages edits in its
    line edits and commits them atomically on its Apply button, exactly
    like the Tk editors, so a half-typed address can never reach disk.
    """

    def __init__(
        self, settings: Settings, on_applied: Callable[[], None]
    ) -> None:
        super().__init__()
        self.settings = settings
        self.on_applied = on_applied
        self._instrument_edits: dict[str, dict[str, QLineEdit]] = {}
        self._limit_edits: dict[str, QLineEdit] = {}

        outer = QVBoxLayout(self)
        outer.addWidget(self._build_instruments_group())
        self.apply_instruments_button = QPushButton(
            "Apply Instrument Changes"
        )
        self.apply_instruments_button.clicked.connect(self._apply_instruments)
        outer.addWidget(self.apply_instruments_button)

        outer.addWidget(self._build_limits_group())
        self.apply_limits_button = QPushButton("Apply Limit Changes")
        self.apply_limits_button.clicked.connect(self._apply_limits)
        outer.addWidget(self.apply_limits_button)
        outer.addStretch(1)

    # -- construction --------------------------------------------------------

    def _build_instruments_group(self) -> QGroupBox:
        group = QGroupBox("Instrument Connections")
        grid = QGridLayout(group)
        for column, heading in enumerate(
            ("Instrument", "Target / Resource", "Port / Baud")
        ):
            label = QLabel(heading)
            label.setStyleSheet("font-weight: bold;")
            grid.addWidget(label, 0, column)

        scope_address = self.settings.instruments.get(SCOPE_INSTRUMENT_KEY)
        if scope_address is None:
            # Same refusal as the Tk editor: rendering an editable rig
            # without its only accepted oscilloscope would invite a save
            # that the scope-identity gate then rejects.
            raise ValueError(
                "No address is configured for the Teledyne LeCroy HDO4054"
            )
        rows = [
            (SCOPE_INSTRUMENT_KEY, "Teledyne LeCroy HDO4054", scope_address),
            *(
                (name, name, address)
                for name, address in self.settings.instruments.items()
                if name != SCOPE_INSTRUMENT_KEY
            ),
        ]
        for row, (name, label_text, address) in enumerate(rows, start=1):
            grid.addWidget(QLabel(label_text), row, 0)
            target_edit = QLineEdit(address.ip)
            grid.addWidget(target_edit, row, 1)
            port_edit = QLineEdit(str(address.port))
            port_edit.setMaximumWidth(90)
            grid.addWidget(port_edit, row, 2)
            self._instrument_edits[name] = {
                "target": target_edit,
                "port": port_edit,
            }

        help_label = QLabel(_TARGET_HELP)
        help_label.setWordWrap(True)
        grid.addWidget(help_label, len(rows) + 1, 0, 1, 3)
        grid.setColumnStretch(1, 1)
        return group

    def _build_limits_group(self) -> QGroupBox:
        group = QGroupBox("SMU & Safety Limits")
        grid = QGridLayout(group)
        for row, (label_text, attr) in enumerate(_SMU_LIMIT_FIELDS):
            grid.addWidget(QLabel(label_text), row, 0)
            edit = QLineEdit(str(getattr(self.settings.smu, attr)))
            edit.setMaximumWidth(120)
            grid.addWidget(edit, row, 1)
            self._limit_edits[attr] = edit
        grid.setColumnStretch(1, 1)
        return group

    # -- apply ---------------------------------------------------------------

    def _apply_instruments(self) -> None:
        """Validate every row, then swap-save-rollback the whole map.

        Unlike the Tk editor, which stops at the first bad row, every
        row's error is collected into one dialog: an operator retyping a
        bench of addresses should learn about all of them at once.
        """
        edited: dict[str, InstrumentAddress] = {}
        errors: list[str] = []
        for name, fields in self._instrument_edits.items():
            try:
                target, port = validate_instrument_target(
                    fields["target"].text(), fields["port"].text()
                )
            except ValueError as exc:
                errors.append(f"{name}: {exc}")
                continue
            edited[name] = InstrumentAddress(target, port)
        if errors:
            QMessageBox.critical(self, "Validation Error", "\n".join(errors))
            return

        previous = self.settings.instruments
        self.settings.instruments = edited
        try:
            self.settings.save()
        except Exception as exc:
            self.settings.instruments = previous
            QMessageBox.critical(
                self,
                "Save Error",
                f"Could not save instrument settings: {exc}",
            )
            return

        self.on_applied()
        QMessageBox.information(
            self,
            "Instrument Settings",
            "Connection changes were saved and will take effect after "
            "restarting the application.",
        )

    def _apply_limits(self) -> None:
        """The same shape for the SMU envelope: nothing sticks unless saved."""
        pending: list[tuple[str, float]] = []
        errors: list[str] = []
        for attr, edit in self._limit_edits.items():
            raw = edit.text().strip()
            try:
                value = float(raw)
            except (TypeError, ValueError):
                errors.append(f"Invalid value for {attr}: {raw!r}")
                continue
            if not math.isfinite(value) or value <= 0:
                errors.append(f"{attr} must be a finite, positive number.")
                continue
            pending.append((attr, value))
        if errors:
            QMessageBox.critical(self, "Validation Error", "\n".join(errors))
            return

        previous = [
            (attr, getattr(self.settings.smu, attr)) for attr, _ in pending
        ]
        for attr, value in pending:
            setattr(self.settings.smu, attr, value)
        try:
            self.settings.save()
        except Exception as exc:
            for attr, value in previous:
                setattr(self.settings.smu, attr, value)
            QMessageBox.critical(
                self,
                "Save Error",
                f"Could not save limit settings: {exc}",
            )
            return
        self.on_applied()
