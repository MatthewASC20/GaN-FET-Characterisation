"""Safety interlocks for the 400 V GaN rig.

One SafetyMonitor instance is shared by the engine, the tuners and the UI's
EMERGENCY STOP button. A trip is idempotent and always ends with the SMU
output off and the wavegen gates off.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Callable, Optional

from gan_fet.core.events import SafetyTripEvent, bus
from gan_fet.core.models import TripContext
from gan_fet.instruments.base import SmuInterface, WavegenInterface
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
        smu: SmuInterface,
        wavegen: WavegenInterface,
        db: Database,
        on_trip: Optional[Callable[[str, str], None]] = None,
    ):
        self.settings = settings
        self.smu = smu
        self.wavegen = wavegen
        self.db = db
        self.on_trip = on_trip
        self._lock = threading.Lock()
        # Serializes every operation that can energize or de-energize the
        # hardware.  The trip latch is set before emergency shutdown waits for
        # this lock, so a concurrent arm/enable either completes first and is
        # immediately shut down, or observes the latch and refuses to start.
        self._hardware_lock = threading.RLock()
        self._consecutive_failures: dict[str, int] = defaultdict(int)
        self._tripped = False
        self._trip_kind: Optional[str] = None
        self._trip_detail: Optional[str] = None
        self._trip_generation = 0
        self._active_run_id: Optional[int] = None
        # Last-known telemetry, cached as it flows past.  A trip must never
        # perform fresh I/O to describe itself: the latch is set before any
        # shutdown I/O begins, and querying instruments here would delay it.
        self._last_vds_peak_v: Optional[float] = None
        self._last_dc_current_a: Optional[float] = None
        self._last_frequency_hz: Optional[float] = None

    # -- watchdog ---------------------------------------------------------

    def record_read_success(self, source: str = "default") -> None:
        with self._lock:
            self._consecutive_failures[source] = 0

    def record_read_failure(self, source: str) -> None:
        with self._lock:
            self._consecutive_failures[source] += 1
            failures = self._consecutive_failures[source]
        if failures >= self.settings.watchdog_consecutive_failures:
            self.trip(
                "watchdog",
                f"{failures} consecutive failed reads (last: {source})",
            )

    @property
    def is_tripped(self) -> bool:
        with self._lock:
            return self._tripped

    @property
    def trip_reason(self) -> Optional[tuple[str, str]]:
        with self._lock:
            if not self._tripped:
                return None
            return (
                self._trip_kind or "safety",
                self._trip_detail or "safety trip latched",
            )

    @property
    def active_run_id(self) -> Optional[int]:
        with self._lock:
            return self._active_run_id

    @active_run_id.setter
    def active_run_id(self, value: Optional[int]) -> None:
        with self._lock:
            self._active_run_id = value

    def reset_trip(self) -> None:
        """Clear the latch only after an actively confirmed safe shutdown."""
        with self._hardware_lock:
            with self._lock:
                if self._active_run_id is not None:
                    raise RuntimeError(
                        "Cannot reset safety latch while a run is active"
                    )
                generation = self._trip_generation

            if not self.shutdown_outputs():
                raise RuntimeError(
                    "Cannot reset safety latch: output shutdown was not confirmed"
                )

            with self._lock:
                if self._active_run_id is not None:
                    raise RuntimeError(
                        "Cannot reset safety latch while a run is active"
                    )
                if self._trip_generation != generation:
                    raise RuntimeError(
                        "Cannot reset safety latch: a new trip occurred during reset"
                    )
                self._tripped = False
                self._trip_kind = None
                self._trip_detail = None
                self._consecutive_failures.clear()
        log.warning("Safety trip latch reset by operator")

    # -- controlled energization ------------------------------------------

    def arm_wavegen(self, config: str) -> bool:
        """Arm gate outputs only while holding the shared safety permit."""
        with self._hardware_lock:
            reason = self.trip_reason
            if reason is not None:
                raise SafetyTrip(*reason)
            if not self.wavegen.arm_outputs(config):
                return False
            if not self.wavegen.outputs_armed:
                raise ConnectionError(
                    "Wavegen output state was not verified as armed"
                )

            # An E-stop may latch while the transport call is in progress.
            reason = self.trip_reason
            if reason is not None:
                self.wavegen.outputs_off()
                raise SafetyTrip(*reason)
            return True

    def enable_bus(self) -> bool:
        """Enable the SMU only with verified gates and an unlatched interlock."""
        with self._hardware_lock:
            reason = self.trip_reason
            if reason is not None:
                raise SafetyTrip(*reason)
            if not self.wavegen.outputs_armed:
                raise RuntimeError("Cannot enable the SMU before gate verification")
            if not self.smu.output_on():
                return False

            # Close the small window in which an E-stop can latch during the
            # output-on transport operation.
            reason = self.trip_reason
            if reason is not None:
                self.smu.emergency_off()
                raise SafetyTrip(*reason)
            return True

    # -- limit checks -------------------------------------------------------

    def note_frequency(self, frequency_hz: Optional[float]) -> None:
        """Record the gate frequency currently commanded.

        Pushed by whoever sets it rather than read back at trip time, so the
        trip path stays free of instrument I/O.
        """
        with self._lock:
            self._last_frequency_hz = frequency_hz

    def trip_context(self) -> TripContext:
        """Describe the present operating point without touching hardware.

        ``smu.setpoint_v`` is an in-memory attribute, not a query; everything
        else comes from values cached as they passed through the monitor.
        """
        with self._lock:
            vds_peak = self._last_vds_peak_v
            dc_current = self._last_dc_current_a
            frequency = self._last_frequency_hz
        try:
            setpoint = float(self.smu.setpoint_v)
        except Exception:  # pragma: no cover - defensive
            setpoint = None
        return TripContext(
            frequency_hz=frequency,
            bus_setpoint_v=setpoint,
            vds_peak_v=vds_peak,
            dc_current_a=dc_current,
        )

    def check_sample(
        self,
        dc_current: Optional[float] = None,
        vds_peak: Optional[float] = None,
    ) -> None:
        with self._lock:
            if dc_current is not None:
                self._last_dc_current_a = dc_current
            if vds_peak is not None:
                self._last_vds_peak_v = vds_peak
        reason = self.trip_reason
        if reason is not None:
            raise SafetyTrip(*reason)
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
            tripped = self.smu.compliance_tripped()
            if tripped is None:
                self.record_read_failure("smu compliance")
                return
            self.record_read_success("smu compliance")
            if tripped:
                self.trip("compliance", "SMU current compliance limit reached")
        except SafetyTrip:
            raise
        except Exception as exc:
            log.warning("SMU compliance query failed: %s", exc)
            self.record_read_failure("smu compliance")

    # -- trip --------------------------------------------------------------

    def trip(self, kind: str, detail: str) -> None:
        """Shut the rig down, record the event, notify, then raise SafetyTrip."""
        with self._lock:
            self._trip_generation += 1
            first_trip = not self._tripped
            if first_trip:
                self._tripped = True
                self._trip_kind = kind
                self._trip_detail = detail
            else:
                kind = self._trip_kind or kind
                detail = self._trip_detail or detail
            # Capture the association before shutdown.  The engine may finish
            # concurrently with an emergency path and clear ``active_run_id``.
            run_id = self._active_run_id

        log.error("SAFETY TRIP [%s]: %s", kind, detail)
        shutdown_ok = self.shutdown_outputs()
        if not shutdown_ok:
            with self._lock:
                suffix = "OUTPUT SHUTDOWN NOT CONFIRMED"
                latched_detail = self._trip_detail or detail
                if suffix not in latched_detail:
                    latched_detail = f"{latched_detail} — {suffix}"
                self._trip_detail = latched_detail
                kind = self._trip_kind or kind
                detail = latched_detail
        if first_trip:
            try:
                self.db.add_safety_event(run_id, kind, detail, self.trip_context())
            except Exception:
                log.exception("could not record safety event")
            bus.publish(
                SafetyTripEvent(reason=f"{kind}: {detail}", value=0.0)
            )
            if self.on_trip is not None:
                try:
                    self.on_trip(kind, detail)
                except Exception:
                    log.exception("safety on_trip callback failed")
        raise SafetyTrip(kind, detail)

    def emergency_stop(
        self,
        *,
        latched_event: Optional[threading.Event] = None,
    ) -> bool:
        """Latch E-stop and return whether output shutdown was confirmed.

        ``latched_event`` is set immediately after the in-memory interlock is
        latched, before transport I/O.  It lets the engine make an E-stop a
        deterministic terminal safety outcome without blocking the caller on
        hardware shutdown.
        """
        log.error("EMERGENCY STOP pressed")
        with self._lock:
            self._trip_generation += 1
            first_trip = not self._tripped
            if first_trip:
                self._tripped = True
                self._trip_kind = "estop"
                self._trip_detail = "operator emergency stop"
            kind = self._trip_kind or "estop"
            detail = self._trip_detail or "operator emergency stop"
            run_id = self._active_run_id
        if latched_event is not None:
            latched_event.set()
        shutdown_ok = self.shutdown_outputs()
        with self._lock:
            kind = self._trip_kind or "estop"
            detail = self._trip_detail or "operator emergency stop"
            if not shutdown_ok:
                suffix = "OUTPUT SHUTDOWN NOT CONFIRMED"
                if suffix not in detail:
                    detail = f"{detail} — {suffix}"
                self._trip_detail = detail
        if not first_trip:
            return shutdown_ok
        try:
            self.db.add_safety_event(run_id, kind, detail, self.trip_context())
        except Exception:
            log.exception("could not record emergency-stop event")
        bus.publish(SafetyTripEvent(reason=f"{kind}: {detail}", value=0.0))
        if self.on_trip is not None:
            try:
                self.on_trip(kind, detail)
            except Exception:
                log.exception("safety on_trip callback failed")
        return shutdown_ok

    def shutdown_outputs(self) -> bool:
        """Attempt both independent shutdown paths and report confirmation."""
        with self._hardware_lock:
            wavegen_ok = False
            smu_ok = False
            # Remove gate drive first; a failed SMU transport must never
            # prevent the independent SMU shutdown attempt.
            try:
                self.wavegen.outputs_off()
                wavegen_ok = True
            except Exception:
                log.exception("Wavegen outputs could not be confirmed OFF")
            try:
                smu_ok = bool(self.smu.emergency_off())
            except Exception:
                log.exception("SMU emergency shutdown failed")
        if not (wavegen_ok and smu_ok):
            log.critical(
                "OUTPUT SHUTDOWN UNCONFIRMED (wavegen=%s, smu=%s)",
                wavegen_ok,
                smu_ok,
            )
        return wavegen_ok and smu_ok
