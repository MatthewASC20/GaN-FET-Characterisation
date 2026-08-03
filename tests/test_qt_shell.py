"""The phase-1 Qt shell: mode identity, the E-stop contract, the bridge.

Skipped wholesale when PyQt6 is not installed, so environments without the
``[qt]`` extra stay green. Runs on the offscreen platform plugin — unlike the
tkstub, these are real widgets, so a pass means the GUI code executed.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from gan_fet.core.events import StatusUpdatedEvent, bus  # noqa: E402
from gan_fet.ui_qt.event_bridge import EventBridge  # noqa: E402
from gan_fet.ui_qt.main_window import QtMainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def test_the_banner_states_the_mode_unmistakably(qapp) -> None:
    simulated = QtMainWindow(
        is_simulated=True, on_emergency_stop=lambda: None
    )
    live = QtMainWindow(is_simulated=False, on_emergency_stop=lambda: None)
    try:
        assert "SIMULATION" in simulated.banner_text()
        assert "NOT MEASURED DATA" in simulated.banner_text()
        assert "LIVE HARDWARE" in live.banner_text()
    finally:
        simulated.close()
        live.close()


def test_emergency_stop_is_enabled_and_fires_without_any_state(qapp) -> None:
    """The E-stop contract: no window state may gate it."""
    stops: list[bool] = []
    window = QtMainWindow(
        is_simulated=True, on_emergency_stop=lambda: stops.append(True)
    )
    try:
        assert window.estop_button.isEnabled()
        window.estop_button.click()
        assert stops == [True]
        # It must remain live after use.
        assert window.estop_button.isEnabled()
    finally:
        window.close()


def test_the_bridge_relays_bus_events_and_unsubscribes_on_close(
    qapp,
) -> None:
    bridge = EventBridge()
    received: list[str] = []
    bridge.status_updated.connect(
        lambda event: received.append(event.message)
    )

    bus.publish(StatusUpdatedEvent(message="hello from a worker"))
    qapp.processEvents()
    assert received == ["hello from a worker"]

    bridge.close()
    bus.publish(StatusUpdatedEvent(message="after close"))
    qapp.processEvents()
    assert received == ["hello from a worker"]


def test_the_window_shows_bus_status_in_its_status_bar(qapp) -> None:
    window = QtMainWindow(
        is_simulated=True, on_emergency_stop=lambda: None
    )
    try:
        bus.publish(StatusUpdatedEvent(message="Tuning DC voltage..."))
        qapp.processEvents()
        assert window.statusBar().currentMessage() == "Tuning DC voltage..."
    finally:
        window.close()
