"""Thread-safe Event Bus and event definitions for GaN FET Characterisation.

Provides a publish-subscribe broker for decoupling background core execution
from UI components, data loggers, and safety monitors.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Optional, Type, TypeVar

from gan_fet.core.models import ExperimentState, RunRecord

log = logging.getLogger(__name__)

T = TypeVar("T")


# -- Event Definitions --------------------------------------------------------

@dataclass(frozen=True)
class SampleAcquiredEvent:
    """Emitted on each sample tick during an experiment run."""

    elapsed_s: float
    amps: float
    smu_voltage: Optional[float] = None
    dc_voltage: Optional[float] = None
    rms_current: Optional[float] = None
    isw_rms: Optional[float] = None
    vds_peak: Optional[float] = None
    frequency_hz: Optional[float] = None


@dataclass(frozen=True)
class MeasurementEvent:
    """Emitted when a reading is taken outside the sampling loop.

    The frequency search and the peak controller measure the same quantities a
    run does — bus, Vds peak, DC current — but at points that are *not* run
    data: they are the search finding its operating point, and several of them
    are on the way to somewhere else. Between them they occupy almost all of a
    tuned run's wall-clock time, so without this the live telemetry sits frozen
    while the rig is at its busiest.

    Deliberately **not** a :class:`SampleAcquiredEvent`. That one is written to
    the database and drawn on the run plot, and neither is true here. Anything
    consuming this should display it and nothing else.
    """

    #: Where the reading was taken, for the status line. e.g. "frequency search".
    source: str
    frequency_hz: Optional[float] = None
    bus_voltage: Optional[float] = None
    vds_peak: Optional[float] = None
    dc_current: Optional[float] = None


@dataclass(frozen=True)
class StateChangedEvent:
    """Emitted when the experiment engine state transitions."""

    old_state: ExperimentState
    new_state: ExperimentState


@dataclass(frozen=True)
class StatusUpdatedEvent:
    """Emitted for human-readable progress updates."""

    message: str


@dataclass(frozen=True)
class SafetyTripEvent:
    """Emitted when a safety threshold is exceeded."""

    reason: str
    value: float


@dataclass(frozen=True)
class RunCompletedEvent:
    """Emitted when an experiment run finishes (successfully or failed)."""

    success: bool
    message: str
    record: Optional[RunRecord] = None


@dataclass(frozen=True)
class ErrorReportedEvent:
    """Emitted when an error dialog should be presented."""

    title: str
    message: str


@dataclass(frozen=True)
class InstrumentCommandEvent:
    """Emitted when a low-level SCPI or instrument command is sent or queried."""

    timestamp: float  # time.time()
    instrument_name: str  # e.g., 'K2410', 'SDG6022X', 'HDO4054', 'SDM3055'
    command: str  # e.g., 'OUTP ON', 'SOUR:VOLT 400.0'
    response: Optional[str] = None  # e.g., '48.2' for query
    is_query: bool = False
    risk_level: str = "LOW"  # 'HIGH', 'MEDIUM', 'LOW'
    duration_ms: float = 0.0
    is_simulated: bool = False


@dataclass(frozen=True)
class PseudoCommandEvent:
    """Emitted for high-level pseudo-commands, sequence intent, or loop updates."""

    timestamp: float  # time.time()
    category: str  # e.g., 'TRIM', 'RAMP', 'ZVS', 'SEQUENCE'
    title: str  # e.g., 'Trimming Gate Threshold'
    detail: str  # e.g., 'Target: 2.50V | Step +10mV'
    loop_id: Optional[str] = None  # Unique ID if updating a dynamic loop row
    iteration: int = 0  # Current loop iteration
    total_iterations: int = 0  # Total iterations (0 if unknown)
    is_simulated: bool = False



# -- Event Bus Implementation -------------------------------------------------

class EventBus:
    """Thread-safe publish-subscribe event bus."""

    def __init__(self):
        self._listeners: dict[Type[Any], list[Callable[[Any], None]]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, event_type: Type[T], callback: Callable[[T], None]) -> Callable[[], None]:
        """Subscribe a callback to an event type. Returns an unsubscribe function."""
        with self._lock:
            self._listeners[event_type].append(callback)

        def unsubscribe():
            with self._lock:
                if callback in self._listeners[event_type]:
                    self._listeners[event_type].remove(callback)

        return unsubscribe

    def publish(self, event: Any) -> None:
        """Publish an event instance to all registered subscribers."""
        event_type = type(event)
        with self._lock:
            subscribers = list(self._listeners[event_type])

        for callback in subscribers:
            try:
                callback(event)
            except Exception as exc:
                log.exception("Error in EventBus listener %r for event %s: %s", callback, event_type.__name__, exc)

    def clear(self) -> None:
        """Remove all registered listeners."""
        with self._lock:
            self._listeners.clear()


# Global default EventBus instance for application-wide use
bus = EventBus()
