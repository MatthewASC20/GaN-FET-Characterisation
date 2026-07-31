"""Single-owner guard for rig access.

Every operation that drives an instrument — a manual ZVS search, a bus
ramp-down, an experiment, an auto sequence — must hold this guard for its
whole duration. Without it two background threads can issue SCPI to the SMU
at once (e.g. a ZVS search ramping up while "Bus Off" ramps down), leaving
the bus at an unpredictable voltage.

The emergency-stop path deliberately does NOT acquire: it must be able to cut
the output while another owner holds the rig.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from enum import Enum
from typing import Iterator, Optional


class RigOwner(Enum):
    MANUAL = "manual operation"
    EXPERIMENT = "experiment"
    SEQUENCE = "auto sequence"
    SHUTDOWN = "shutdown"


class RigBusy(RuntimeError):
    """Raised by `hold` when another owner already has the rig."""

    def __init__(self, current: RigOwner):
        super().__init__(f"The rig is busy: {current.value} in progress.")
        self.current = current


class RigActivity:
    def __init__(self):
        self._lock = threading.Lock()
        self._owner: Optional[RigOwner] = None
        self._cancel = threading.Event()

    @property
    def owner(self) -> Optional[RigOwner]:
        with self._lock:
            return self._owner

    @property
    def busy(self) -> bool:
        return self.owner is not None

    def try_acquire(self, owner: RigOwner) -> bool:
        with self._lock:
            if self._owner is not None:
                return False
            self._owner = owner
            self._cancel.clear()
            return True

    def release(self, owner: RigOwner) -> None:
        """Release only if `owner` still holds it (a late release from a
        finished task must not free a newer owner's claim)."""
        with self._lock:
            if self._owner is owner:
                self._owner = None
                self._cancel.clear()

    @contextmanager
    def hold(self, owner: RigOwner) -> Iterator["RigActivity"]:
        if not self.try_acquire(owner):
            raise RigBusy(self.owner or owner)
        try:
            yield self
        finally:
            self.release(owner)

    # -- cancellation ------------------------------------------------------

    def request_cancel(self) -> None:
        """Ask the current owner to stop; owners poll `cancelled`."""
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel_check(self):
        """Predicate for the control loops (`achieve_peak`, `find_minimum`)."""
        return lambda: self._cancel.is_set()
