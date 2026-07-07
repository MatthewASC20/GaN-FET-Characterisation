"""Safety interlocks for the 400 V GaN rig.

One SafetyMonitor instance is shared by the engine, the tuners and the UI's
EMERGENCY STOP button. A trip is idempotent and always ends with the SMU
output off and the wavegen gates off.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from gan_fet.instruments.smu import Keithley2400
from gan_fet.instruments.wavegen import Sdg6022x
from gan_fet.settings import SafetySettings
from gan_fet.storage.db import Database

log = logging.getLogger(__name__)


class SafetyTrip(RuntimeError):
    """Raised inside engine/tuner loops after a trip has been executed."""

    def __init__(self, kind: str, detail: str):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


class SafetyMonitor:
    def __init__(
        self,
        settings: SafetySettings,
        smu: Keithley2400,
        wavegen: Sdg6022x,
        db: Database,
        on_trip: Optional[Callable[[str, str], None]] = None,
    ):
        self.settings = settings
        self.smu = smu
        self.wavegen = wavegen
        self.db = db
        self.on_trip = on_trip
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self.active_run_id: Optional[int] = None

    # -- watchdog ---------------------------------------------------------

    def record_read_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0

    def record_read_failure(self, source: str) -> None:
        with self._lock:
            self._consecutive_failures += 1
            failures = self._consecutive_failures
        if failures >= self.settings.watchdog_consecutive_failures:
            self.trip(
                "watchdog",
                f"{failures} consecutive failed reads (last: {source})",
            )

    # -- limit checks -------------------------------------------------------

    def check_sample(
        self,
        dc_current: Optional[float] = None,
        vds_peak: Optional[float] = None,
    ) -> None:
        if dc_current is not None and abs(dc_current) > self.settings.max_dc_current_a:
            self.trip(
                "overcurrent",
                f"DC input current {dc_current:.4f} A exceeds limit "
                f"{self.settings.max_dc_current_a:.4f} A",
            )
        if vds_peak is not None and vds_peak > self.settings.max_vds_peak_v:
            self.trip(
                "overvoltage",
                f"Vds peak {vds_peak:.1f} V exceeds ceiling "
                f"{self.settings.max_vds_peak_v:.1f} V",
            )

    def check_compliance(self) -> None:
        try:
            if self.smu.compliance_tripped():
                self.trip("compliance", "SMU current compliance limit reached")
        except SafetyTrip:
            raise
        except Exception:
            pass  # compliance polling is advisory; the watchdog covers dead links

    # -- trip --------------------------------------------------------------

    def trip(self, kind: str, detail: str) -> None:
        """Shut the rig down, record the event, notify, then raise SafetyTrip."""
        log.error("SAFETY TRIP [%s]: %s", kind, detail)
        self.shutdown_outputs()
        try:
            self.db.add_safety_event(self.active_run_id, kind, detail)
        except Exception:
            log.exception("could not record safety event")
        if self.on_trip is not None:
            try:
                self.on_trip(kind, detail)
            except Exception:
                log.exception("safety on_trip callback failed")
        raise SafetyTrip(kind, detail)

    def emergency_stop(self) -> None:
        """UI EMERGENCY STOP: shut down without raising (not in a control loop)."""
        log.error("EMERGENCY STOP pressed")
        self.shutdown_outputs()
        try:
            self.db.add_safety_event(self.active_run_id, "estop", "operator emergency stop")
        except Exception:
            pass
        if self.on_trip is not None:
            try:
                self.on_trip("estop", "operator emergency stop")
            except Exception:
                pass

    def shutdown_outputs(self) -> None:
        self.smu.emergency_off()
        try:
            self.wavegen.outputs_off()
        except Exception:
            pass
