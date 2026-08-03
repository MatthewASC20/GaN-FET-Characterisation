"""Offscreen tests: Qt command log console and run-history view.

Skipped wholesale when PyQt6 is not installed. Events travel the production
path — global bus, real :class:`EventBridge`, queued signal delivery — so a
pass means the console renders what a worker thread actually published, not
what a stubbed appender was handed.
"""

from __future__ import annotations

import os
import time

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from gan_fet.core.events import (  # noqa: E402
    InstrumentCommandEvent,
    PseudoCommandEvent,
    SafetyTripEvent,
    bus,
)
from gan_fet.core.models import (  # noqa: E402
    FinalReadings,
    MatrixPoint,
    freq_label,
)
from gan_fet.storage.db import Database  # noqa: E402
from gan_fet.ui_qt.command_log import (  # noqa: E402
    CommandLogConsole,
    RunHistoryView,
)
from gan_fet.ui_qt.event_bridge import EventBridge  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def pump(qapp, predicate, timeout_s: float = 5.0) -> bool:
    """Process Qt events until ``predicate()`` holds or the timeout runs out."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture()
def console(qapp):
    """A console attached to a live bridge, detached again after the test."""
    bridge = EventBridge()
    widget = CommandLogConsole()
    widget.attach(bridge)
    yield widget
    bridge.close()


def _command_event(command: str, **overrides) -> InstrumentCommandEvent:
    fields = dict(
        timestamp=time.time(),
        instrument_name="K2410",
        command=command,
        response=None,
        risk_level="LOW",
        is_simulated=False,
    )
    fields.update(overrides)
    return InstrumentCommandEvent(**fields)


def test_instrument_events_render_one_marked_line_each(qapp, console) -> None:
    bus.publish(
        _command_event(
            "SOUR:VOLT 400.0",
            response="OK",
            risk_level="HIGH",
            is_simulated=True,
        )
    )
    assert pump(qapp, lambda: "SOUR:VOLT 400.0" in console.text())
    text = console.text()
    assert "[K2410]" in text
    assert "[SIM]" in text
    assert "[HIGH]" in text
    assert "-> OK" in text
    assert console.line_count() == 1


def test_pseudo_commands_render_with_category_and_iteration(
    qapp, console
) -> None:
    bus.publish(
        PseudoCommandEvent(
            timestamp=time.time(),
            category="RAMP",
            title="Ramping bus",
            detail="to 400 V",
            iteration=2,
            total_iterations=5,
        )
    )
    assert pump(qapp, lambda: "Ramping bus" in console.text())
    assert "[RAMP]" in console.text()
    assert "[2/5]" in console.text()


def test_safety_trips_render_and_multiline_reasons_stay_one_row(
    qapp, console
) -> None:
    bus.publish(SafetyTripEvent(reason="Overcurrent\nlimit", value=1.23))
    assert pump(qapp, lambda: "SAFETY TRIP" in console.text())
    # The embedded newline must be escaped, or one event would occupy two
    # rows and break the row-cap accounting shared with the Tk console.
    assert r"Overcurrent\nlimit" in console.text()
    assert "1.23" in console.text()
    assert console.line_count() == 1


def test_the_row_cap_drops_the_oldest_lines(qapp) -> None:
    bridge = EventBridge()
    widget = CommandLogConsole(max_rows=3)
    widget.attach(bridge)
    try:
        for index in range(5):
            bus.publish(_command_event(f"CMD {index}"))
        assert pump(qapp, lambda: "CMD 4" in widget.text())
        assert widget.line_count() == 3
        assert "CMD 0" not in widget.text()
        assert "CMD 1" not in widget.text()
        assert "CMD 2" in widget.text()
    finally:
        bridge.close()


def test_the_clear_button_empties_the_console(qapp, console) -> None:
    bus.publish(_command_event("OUTP ON"))
    assert pump(qapp, lambda: "OUTP ON" in console.text())
    console.clear_button.click()
    assert console.text() == ""
    assert console.line_count() == 0
    # The console keeps logging after a clear.
    bus.publish(_command_event("OUTP OFF"))
    assert pump(qapp, lambda: "OUTP OFF" in console.text())
    assert console.line_count() == 1


def test_run_history_refresh_shows_the_completed_run(qapp, tmp_path) -> None:
    db = Database(tmp_path / "history.db")
    try:
        point = MatrixPoint(
            device_name="QT-DUT",
            config="config-A",
            frequency_hz=2_000_000,
            duty_pct=30,
            temperature_c=25,
            voltage_v=200,
        )
        run_id = db.create_run(point, 1.0)
        db.complete_run(
            run_id,
            FinalReadings(),
            tuned_voltage_v=201.5,
            tuned_frequency_hz=1_950_000.0,
        )

        view = RunHistoryView(db)
        view.refresh("QT-DUT")
        assert view.table.rowCount() == 1
        cells = [
            view.table.item(0, column).text()
            for column in range(view.table.columnCount())
        ]
        assert cells[0] == "config-A"
        assert cells[1] == freq_label(2_000_000)
        assert cells[2] == "30%"
        assert cells[5] == "completed"
        assert cells[6] == "1"
        assert "1.95" in cells[7]
        assert "201.5" in cells[8]

        # Unknown devices render empty rather than stale rows.
        view.refresh("nobody")
        assert view.table.rowCount() == 0
    finally:
        db.close()
