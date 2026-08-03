"""Qt command log console and run-history table.

Qt counterparts of the Tk command log (``gan_fet.ui.command_log_view``) and
the run-history listing. The rendering policy — one row per event, embedded
line breaks escaped, a bounded number of stored rows — is imported from the
Tk console's pure helpers rather than restated, so the two front-ends cannot
drift apart on what the log shows or how much it retains.

Threading: nothing here subscribes to the event bus directly. The console
connects to :class:`~gan_fet.ui_qt.event_bridge.EventBridge` signals, whose
queued cross-thread delivery guarantees every appender runs on the Qt
thread — the same contract ``UiDispatcher`` provides for Tk.
"""

from __future__ import annotations

import time
from typing import Optional

from PyQt6.QtGui import QColor, QFont, QTextCursor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from gan_fet.core.events import (
    InstrumentCommandEvent,
    PseudoCommandEvent,
    SafetyTripEvent,
)
from gan_fet.core.models import RunRecord, freq_label
from gan_fet.storage.db import Database
from gan_fet.ui.command_log_view import (
    MAX_LOG_ROWS,
    rows_over_limit,
    single_line_text,
)
from gan_fet.ui.telemetry_format import format_frequency, format_volts
from gan_fet.ui_qt.event_bridge import EventBridge

#: Foreground colours on the console's dark background, mirroring the Tk
#: console's palette: alarm red for HIGH, amber for MEDIUM, and a receding
#: grey for LOW so that risky traffic stands out by contrast alone.
_RISK_COLOURS = {
    "HIGH": "#ff5252",
    "MEDIUM": "#ff9800",
    "LOW": "#9e9e9e",
}
_PSEUDO_COLOUR = "#00e5ff"
_TRIP_COLOUR = "#ff1744"
_CONSOLE_STYLE = (
    "QTextEdit { background-color: #1e1e1e; color: #d4d4d4;"
    " font-family: Menlo, Consolas, monospace; }"
)


class CommandLogConsole(QWidget):
    """Real-time log of SCPI traffic, pseudo-commands and safety trips.

    Constructed without any rig object: callers hand it an
    :class:`EventBridge` through :meth:`attach`, so the console can be built
    (and tested) before any hardware composition exists, and it never holds
    a reference that could keep instruments alive.
    """

    def __init__(
        self,
        max_rows: int = MAX_LOG_ROWS,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._max_rows = max_rows
        self._rendered_rows = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        toolbar = QHBoxLayout()
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.clear)
        toolbar.addWidget(self.clear_button)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self._view = QTextEdit()
        self._view.setReadOnly(True)
        self._view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self._view.setStyleSheet(_CONSOLE_STYLE)
        layout.addWidget(self._view)

    def attach(self, bridge: EventBridge) -> None:
        """Connect the bridge's signals to the appenders.

        Detaching is the bridge's job: closing it stops all delivery, so the
        console needs no unsubscribe bookkeeping of its own.
        """
        bridge.instrument_command.connect(self._append_instrument)
        bridge.pseudo_command.connect(self._append_pseudo)
        bridge.safety_trip.connect(self._append_safety_trip)

    def clear(self) -> None:
        self._view.clear()
        self._rendered_rows = 0

    def text(self) -> str:
        """The rendered console content; primarily for test assertions."""
        return self._view.toPlainText()

    def line_count(self) -> int:
        """Number of stored event rows (bounded by the shared row cap)."""
        return self._rendered_rows

    # -- appenders -----------------------------------------------------------

    def _append_instrument(self, event: InstrumentCommandEvent) -> None:
        stamp = time.strftime("%H:%M:%S", time.localtime(event.timestamp))
        millis = int((event.timestamp % 1) * 1000)
        mode = "SIM" if event.is_simulated else "RAW"
        response = (
            f" -> {single_line_text(event.response)}"
            if event.response is not None
            else ""
        )
        line = (
            f"[{stamp}.{millis:03d}] [{mode}] [{event.risk_level}]"
            f" [{single_line_text(event.instrument_name)}]"
            f" {single_line_text(event.command)}{response}"
        )
        colour = _RISK_COLOURS.get(event.risk_level, _RISK_COLOURS["LOW"])
        self._append_line(
            line, colour, bold=event.risk_level in ("HIGH", "MEDIUM")
        )

    def _append_pseudo(self, event: PseudoCommandEvent) -> None:
        stamp = time.strftime("%H:%M:%S", time.localtime(event.timestamp))
        iteration = (
            f" [{event.iteration}/{event.total_iterations}]"
            if event.total_iterations > 0
            else ""
        )
        line = (
            f"[{stamp}] [{single_line_text(event.category)}]"
            f" {single_line_text(event.title)}{iteration}:"
            f" {single_line_text(event.detail)}"
        )
        self._append_line(line, _PSEUDO_COLOUR, bold=True)

    def _append_safety_trip(self, event: SafetyTripEvent) -> None:
        # SafetyTripEvent carries no timestamp; arrival time is the honest
        # substitute, exactly as the Tk console renders it.
        stamp = time.strftime("%H:%M:%S", time.localtime())
        line = (
            f"[{stamp}] SAFETY TRIP: {single_line_text(event.reason)}"
            f" (Value: {single_line_text(event.value)})"
        )
        self._append_line(line, _TRIP_COLOUR, bold=True)

    # -- rendering -----------------------------------------------------------

    def _append_line(self, line: str, colour: str, *, bold: bool) -> None:
        # insertPlainText rather than append(): append() auto-detects HTML,
        # and SCPI traffic can legitimately contain markup-like characters.
        self._view.moveCursor(QTextCursor.MoveOperation.End)
        self._view.setTextColor(QColor(colour))
        self._view.setFontWeight(
            QFont.Weight.Bold if bold else QFont.Weight.Normal
        )
        self._view.insertPlainText(line + "\n")
        self._rendered_rows += 1
        self._prune()
        bar = self._view.verticalScrollBar()
        if bar is not None:
            bar.setValue(bar.maximum())

    def _prune(self) -> None:
        """Drop the oldest rows over the cap — the Tk console's policy."""
        overflow = rows_over_limit(self._rendered_rows, self._max_rows)
        if not overflow:
            return
        document = self._view.document()
        if document is None:
            return
        cursor = QTextCursor(document)
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        cursor.movePosition(
            QTextCursor.MoveOperation.NextBlock,
            QTextCursor.MoveMode.KeepAnchor,
            overflow,
        )
        cursor.removeSelectedText()
        self._rendered_rows -= overflow


_RUN_COLUMNS = (
    "Config",
    "Frequency",
    "Duty",
    "Temp",
    "Voltage",
    "Status",
    "Attempt",
    "Tuned f",
    "Tuned V",
)


def _run_cells(record: RunRecord) -> tuple[str, ...]:
    """One rendered cell per column, reusing the shared unit formatters.

    ``format_frequency``/``format_volts`` render a missing tuned value as an
    em dash rather than a zero — on this rig "did not tune" and "tuned to
    zero" must never look alike.
    """
    point = record.point
    return (
        point.config,
        freq_label(point.frequency_hz),
        f"{point.duty_pct}%",
        f"{point.temperature_c} °C",
        f"{point.voltage_v} V",
        record.status,
        str(record.attempt_no),
        format_frequency(record.tuned_frequency_hz),
        format_volts(record.tuned_voltage_v),
    )


class RunHistoryView(QWidget):
    """Read-only table of the preferred attempt per matrix point.

    Rows come from :meth:`Database.runs_for_device`, which already selects
    the newest successful attempt per point — the view adds no selection
    policy of its own, so it can never disagree with reports about which
    attempt counts.
    """

    def __init__(
        self, db: Database, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self._db = db
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, len(_RUN_COLUMNS))
        self.table.setHorizontalHeaderLabels(list(_RUN_COLUMNS))
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        header = self.table.verticalHeader()
        if header is not None:
            header.setVisible(False)
        layout.addWidget(self.table)

    def refresh(self, device_name: str) -> None:
        """Reload the table for ``device_name``; unknown names render empty."""
        records = self._db.runs_for_device(device_name)
        self.table.setRowCount(len(records))
        for row, record in enumerate(records):
            for column, cell in enumerate(_run_cells(record)):
                self.table.setItem(row, column, QTableWidgetItem(cell))
