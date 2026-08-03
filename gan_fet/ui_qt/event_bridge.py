"""Bridge the core event bus onto Qt signals.

Core code publishes typed events from worker threads. A ``QObject``
re-emitting them as signals gives every Qt consumer thread-correct delivery
for free: Qt queues cross-thread signal connections onto the receiver's
thread, which is the entire job ``UiDispatcher`` performs for Tk.

One bridge per application. Widgets connect to its signals instead of
touching the bus, so nothing in ``ui_qt`` ever runs listener code on a
worker thread.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

from gan_fet.core.events import (
    InstrumentCommandEvent,
    MeasurementEvent,
    PseudoCommandEvent,
    SafetyTripEvent,
    SampleAcquiredEvent,
    StatusUpdatedEvent,
    bus,
)


class EventBridge(QObject):
    """Subscribes to the global bus and re-emits each event as a signal."""

    sample_acquired = pyqtSignal(object)
    status_updated = pyqtSignal(object)
    measurement = pyqtSignal(object)
    safety_trip = pyqtSignal(object)
    instrument_command = pyqtSignal(object)
    pseudo_command = pyqtSignal(object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._unsubscribes = [
            bus.subscribe(SampleAcquiredEvent, self.sample_acquired.emit),
            bus.subscribe(StatusUpdatedEvent, self.status_updated.emit),
            bus.subscribe(MeasurementEvent, self.measurement.emit),
            bus.subscribe(SafetyTripEvent, self.safety_trip.emit),
            bus.subscribe(
                InstrumentCommandEvent, self.instrument_command.emit
            ),
            bus.subscribe(PseudoCommandEvent, self.pseudo_command.emit),
        ]

    def close(self) -> None:
        """Stop relaying. Idempotent; the window calls this on close."""
        while self._unsubscribes:
            self._unsubscribes.pop()()
