"""The Qt front-end's safety spine, against the real simulated rig.

Skipped wholesale when PyQt6 is not installed. Runs on the offscreen
platform plugin — unlike the tkstub these are real widgets driving the real
``RigOperations``/``SafetyMonitor``/driver stack over ``SimulatedRigPlant``,
so a pass means the operations actually executed and were confirmed by
readback.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from gan_fet.core.events import StatusUpdatedEvent, bus  # noqa: E402
from gan_fet.core.experiment import ExperimentEngine  # noqa: E402
from gan_fet.core.safety import SafetyMonitor  # noqa: E402
from gan_fet.instruments.rig import build_instrument_rig  # noqa: E402
from gan_fet.settings import Settings  # noqa: E402
from gan_fet.storage.db import Database  # noqa: E402
from gan_fet.ui_qt.dispatcher import QtDispatcher  # noqa: E402
from gan_fet.ui_qt.event_bridge import EventBridge  # noqa: E402
from gan_fet.ui_qt.main_window import QtMainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def pump(qapp, predicate, timeout_s: float = 10.0) -> bool:
    """Process Qt events until ``predicate()`` holds or the timeout runs out."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture()
def spine(qapp, tmp_path):
    """A Qt window over the full simulated composition, torn down safely."""
    settings = Settings()
    rig = build_instrument_rig(settings, simulate=True)
    db = Database(tmp_path / "qt.db")
    safety = SafetyMonitor(settings.safety, rig.smu, rig.wavegen, db)
    engine = ExperimentEngine(
        db, settings, rig.smu, rig.scope, rig.dmm, rig.wavegen, safety
    )
    closed: list[bool] = []
    window = QtMainWindow(
        settings=settings,
        engine=engine,
        safety=safety,
        smu=rig.smu,
        wavegen_controller=rig.wavegen_controller,
        is_simulated=True,
        close_resources=lambda: closed.append(True),
    )
    # Dialogs are recorded rather than raised: offscreen tests must never
    # block on a message box.
    notices: list[tuple[str, str, str]] = []
    window.show_info = lambda t, m: notices.append(("info", t, m))
    window.show_error = lambda t, m: notices.append(("error", t, m))
    window.confirm = lambda t, m, dangerous=False: True
    window._notices = notices
    window._resources_closed_calls = closed

    yield window

    if not window._resources_closed:
        window.close()
        assert pump(qapp, lambda: window._resources_closed, 15.0)
    rig.close_clients()
    db.close()


def test_the_banner_states_the_mode_unmistakably(qapp, spine) -> None:
    assert "SIMULATION" in spine.banner_text()
    assert "NOT MEASURED DATA" in spine.banner_text()


def test_bus_off_confirms_both_outputs_by_readback(qapp, spine) -> None:
    spine.bus_off_button.click()
    assert pump(qapp, lambda: spine.operations.active_kind is None)
    assert not spine.smu.output_is_on
    assert not spine.wavegen_controller.outputs_armed
    assert "OFF" in spine.statusBar().currentMessage()


def test_emergency_stop_latches_and_stays_enabled(qapp, spine) -> None:
    spine.estop_button.click()
    worker = spine.rig._emergency_worker
    assert worker is not None
    assert pump(qapp, lambda: not worker.is_alive())
    qapp.processEvents()
    assert spine.safety.is_tripped
    assert not spine.smu.output_is_on
    assert spine.estop_button.isEnabled()
    assert "EMERGENCY STOP complete" in spine.statusBar().currentMessage()
    # The latch gates the ordinary shutdown path but never the E-stop.
    spine.refresh_controls()
    assert spine.estop_button.isEnabled()


def test_reset_safety_clears_a_latched_trip_after_confirmation(
    qapp, spine
) -> None:
    spine.safety.emergency_stop()
    spine.refresh_controls()
    assert spine.safety.is_tripped

    spine.reset_safety_button.click()
    assert pump(
        qapp,
        lambda: spine.operations.active_kind is None
        and not spine.safety.is_tripped,
    )
    assert "reset" in spine.statusBar().currentMessage().lower()


def test_dispatcher_runs_posted_work_on_the_ui_thread(qapp) -> None:
    dispatcher = QtDispatcher()
    ran_on: list[int] = []
    done = threading.Event()

    def record() -> None:
        ran_on.append(threading.get_ident())
        done.set()

    worker = threading.Thread(target=lambda: dispatcher.post(record))
    worker.start()
    worker.join()
    assert pump(qapp, done.is_set)
    assert ran_on == [threading.get_ident()]


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


def test_closing_shuts_down_and_closes_resources_exactly_once(
    qapp, spine
) -> None:
    spine.close()
    assert pump(qapp, lambda: spine._resources_closed, 15.0)
    assert pump(qapp, lambda: not spine.isVisible())
    assert spine._resources_closed_calls == [True]
    assert not spine.smu.output_is_on
    assert not spine.wavegen_controller.outputs_armed
