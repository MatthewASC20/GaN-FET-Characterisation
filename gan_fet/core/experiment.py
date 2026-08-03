"""Experiment engine: drives one characterisation run in a worker thread.

Flow per run:
  1. append a new auditable attempt (a prior successful attempt remains intact)
  2. SMU on (soft start from 0 V) and closed-loop ramp until the scope's
     Vds peak equals the selected test voltage (replaces the manual bench
     supply + validation dialog of v1)
  3. optional DC voltage tune (minimise DC input current vs bus voltage)
  4. timed sampling loop → `samples` table (+ live UI callbacks)
  5. final readings + oscilloscope screenshot → `runs` row
  6. safe shutdown (bus ramped to 0 V, output off)

The engine has no tkinter dependency: every interaction goes through
EngineCallbacks, and the UI is responsible for marshalling to its thread.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from gan_fet.core.events import (
    ErrorReportedEvent,
    RunCompletedEvent,
    SampleAcquiredEvent,
    StateChangedEvent,
    StatusUpdatedEvent,
    bus,
)
from gan_fet.core.models import (
    ExperimentParams,
    ExperimentState,
    FinalReadings,
    RunRecord,
    freq_label,
    sanitize_device_name,
)
from gan_fet.core.frequency_tune import FrequencyTuner
from gan_fet.core.safety import SafetyMonitor, SafetyTrip
from gan_fet.core.voltage_control import PeakControlError, PeakVoltageController
from gan_fet.core.voltage_tune import VoltageTuneMeasurementError, VoltageTuner
from gan_fet.instruments.base import (
    MultimeterInterface,
    OscilloscopeInterface,
    SmuInterface,
    WavegenInterface,
)
from gan_fet.instruments.smu import SmuLimitError
from gan_fet.settings import Settings
from gan_fet.storage.db import Database

log = logging.getLogger(__name__)

TIME_SERIES_INTERVAL_S = 10.0   # cadence for the slower DMM / Isw readings
SCREENSHOT_SAFETY_POLL_MAX_S = 0.25


def _utc_timestamp_ms() -> str:
    """Return an unambiguous UTC wall-clock timestamp with millisecond precision."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _noop(*_args, **_kwargs):
    return None


@dataclass
class EngineCallbacks:
    """All invoked from the engine's worker thread."""

    on_sample: Callable[[float, float], None] = _noop          # (elapsed_s, amps)
    on_state: Callable[[ExperimentState], None] = _noop
    on_status: Callable[[str], None] = _noop
    on_finished: Callable[[bool, str], None] = _noop           # (success, message)
    confirm_overwrite: Callable[[str], bool] = field(default=lambda _desc: True)
    report_error: Callable[[str, str], None] = _noop           # (title, message)


@dataclass(frozen=True)
class RunOutcome:
    """Terminal result of one engine invocation."""

    success: bool
    status: str
    message: str
    run_id: Optional[int] = None
    record: Optional[RunRecord] = None


class FinalReadingsError(RuntimeError):
    """One or more mandatory final measurements could not be captured."""


class ExperimentCancelled(RuntimeError):
    """Internal control-flow signal for a requested cancellation."""


class ExperimentEngine:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        smu: SmuInterface,
        scope: OscilloscopeInterface,
        dmm: Optional[MultimeterInterface],
        wavegen: WavegenInterface,
        safety: SafetyMonitor,
        callbacks: Optional[EngineCallbacks] = None,
        on_run_completed: Optional[Callable[[RunRecord], None]] = None,
    ):
        self.db = db
        self.settings = settings
        self.smu = smu
        self.scope = scope
        self.dmm = dmm
        self.wavegen = wavegen
        self.safety = safety
        self.callbacks = callbacks or EngineCallbacks()
        self.on_run_completed = on_run_completed  # e.g. Sheets sync enqueue

        self.peak_controller = PeakVoltageController(
            smu, scope, settings.peak_control, safety
        )
        self.voltage_tuner = VoltageTuner(smu, settings.voltage_tune, safety, scope=scope)
        self.frequency_tuner = FrequencyTuner(
            wavegen,
            smu,
            scope,
            self.peak_controller,
            settings.frequency_tune,
            settings.safety,
            safety,
        )

        self._state = ExperimentState.IDLE
        self._state_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._outcome_event = threading.Event()
        self._cancel_event = threading.Event()
        self._emergency_stop_requested = threading.Event()
        self._run_association_event = threading.Event()
        self._run_generation = 0
        self._run_id_for_audit: Optional[int] = None
        self._safety_event_watermark = 0
        self._sampling_active = threading.Event()
        self._last_outcome: Optional[RunOutcome] = None
        self.last_current: Optional[float] = None

    # -- state ---------------------------------------------------------

    @property
    def state(self) -> ExperimentState:
        with self._state_lock:
            return self._state

    def _set_state(self, state: ExperimentState) -> None:
        with self._state_lock:
            old_state = self._state
            self._state = state
        if old_state == state:
            return
        try:
            self.callbacks.on_state(state)
        except Exception:
            log.exception("Engine state callback failed")
        bus.publish(StateChangedEvent(old_state=old_state, new_state=state))

    def _update_status(self, msg: str) -> None:
        try:
            self.callbacks.on_status(msg)
        except Exception:
            log.exception("Engine status callback failed")
        bus.publish(StatusUpdatedEvent(message=msg))

    def _report_error(self, title: str, message: str) -> None:
        try:
            self.callbacks.report_error(title, message)
        except Exception:
            log.exception("Engine error callback failed")
        bus.publish(ErrorReportedEvent(title=title, message=message))

    def _cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def is_busy(self) -> bool:
        with self._state_lock:
            return self._state != ExperimentState.IDLE or (
                self._thread is not None and self._thread.is_alive()
            )

    def start(self, params: ExperimentParams) -> bool:
        with self._state_lock:
            if self._state != ExperimentState.IDLE or (
                self._thread is not None and self._thread.is_alive()
            ):
                return False
            safety_event_watermark = self.db.latest_safety_event_id()
            old_state = self._state
            self._state = ExperimentState.RUNNING
            self._last_outcome = None
            self._outcome_event.clear()
            self._cancel_event.clear()
            self._emergency_stop_requested.clear()
            self._run_generation += 1
            self._run_association_event = threading.Event()
            self._run_id_for_audit = None
            self._safety_event_watermark = safety_event_watermark
            self._sampling_active.clear()
            thread = threading.Thread(
                target=self._run, args=(params,), daemon=False, name="experiment"
            )
            self._thread = thread
        try:
            self.callbacks.on_state(ExperimentState.RUNNING)
        except Exception:
            log.exception("Engine state callback failed")
        bus.publish(
            StateChangedEvent(
                old_state=old_state,
                new_state=ExperimentState.RUNNING,
            )
        )
        try:
            thread.start()
        except Exception:
            with self._state_lock:
                self._state = ExperimentState.IDLE
                self._thread = None
                self._run_association_event.set()
            try:
                self.callbacks.on_state(ExperimentState.IDLE)
            except Exception:
                log.exception("Engine state callback failed")
            bus.publish(
                StateChangedEvent(
                    old_state=ExperimentState.RUNNING,
                    new_state=ExperimentState.IDLE,
                )
            )
            raise
        return True

    @property
    def last_outcome(self) -> Optional[RunOutcome]:
        with self._state_lock:
            return self._last_outcome

    def toggle_pause(self) -> None:
        pause_unavailable = False
        with self._state_lock:
            old_state = self._state
            if (
                self._state == ExperimentState.RUNNING
                and self._sampling_active.is_set()
            ):
                self._state = ExperimentState.PAUSED
            elif self._state == ExperimentState.RUNNING:
                pause_unavailable = True
            elif self._state == ExperimentState.PAUSED:
                self._state = ExperimentState.RUNNING
            new_state = self._state
        if pause_unavailable:
            self._update_status(
                "Pause is available only during the timed sampling phase."
            )
        if old_state != new_state:
            try:
                self.callbacks.on_state(new_state)
            except Exception:
                log.exception("Engine state callback failed")
            bus.publish(
                StateChangedEvent(old_state=old_state, new_state=new_state)
            )

    def cancel(self) -> None:
        cancelled = False
        with self._state_lock:
            old_state = self._state
            if self._state in (ExperimentState.RUNNING, ExperimentState.PAUSED):
                self._state = ExperimentState.CANCELLED
                cancelled = True
            new_state = self._state
        if cancelled:
            self._cancel_event.set()
        if old_state != new_state:
            try:
                self.callbacks.on_state(new_state)
            except Exception:
                log.exception("Engine state callback failed")
            bus.publish(
                StateChangedEvent(old_state=old_state, new_state=new_state)
            )

    def wait_until_idle(
        self,
        timeout: Optional[float] = None,
        poll_s: float = 0.2,
    ) -> Optional[RunOutcome]:
        """Wait for the current worker and return its terminal outcome.

        ``None`` means the timeout expired before the worker terminated.
        ``poll_s`` is retained for API compatibility; joining the worker avoids
        active polling.
        """
        del poll_s
        with self._state_lock:
            thread = self._thread
            outcome = self._last_outcome
        if thread is None:
            return outcome
        thread.join(timeout)
        if thread.is_alive():
            return None
        self._outcome_event.wait(0.1)
        return self.last_outcome

    def join(self, timeout: Optional[float] = None) -> Optional[RunOutcome]:
        return self.wait_until_idle(timeout=timeout)

    def cancel_and_join(
        self, timeout: Optional[float] = None
    ) -> Optional[RunOutcome]:
        self.cancel()
        return self.join(timeout)

    def request_emergency_stop(self) -> threading.Thread:
        """Latch E-stop, cancel the run, and shut hardware down off-thread.

        The worker signals as soon as the safety latch is set.  Waiting for
        that in-memory transition (not for transport I/O) prevents the run
        from racing to a normal ``cancelled`` outcome before the E-stop is
        auditable as a safety trip.  If the worker has not created its run row
        yet, the shutdown worker backfills the just-created standalone audit
        event after the run association becomes available.
        """
        self._emergency_stop_requested.set()
        with self._state_lock:
            # Once IDLE is published the terminal outcome is already fixed;
            # an E-stop after that point is a standalone bench event, even if
            # the worker is still returning from its final callback.
            associate_with_run = self._state != ExperimentState.IDLE
            run_generation = self._run_generation
            association_event = self._run_association_event
            safety_event_watermark = self._safety_event_watermark
        latched = threading.Event()

        def shutdown_and_associate() -> None:
            try:
                self.safety.emergency_stop(latched_event=latched)
            finally:
                if associate_with_run:
                    association_event.wait()
                    with self._state_lock:
                        run_id = (
                            self._run_id_for_audit
                            if self._run_generation == run_generation
                            else None
                        )
                    if run_id is not None:
                        try:
                            self.db.associate_latest_unassigned_safety_event(
                                run_id,
                                "estop",
                                after_id=safety_event_watermark,
                            )
                        except Exception:
                            log.exception(
                                "Could not associate immediate E-stop audit "
                                "with run %s",
                                run_id,
                            )

        shutdown_thread = threading.Thread(
            target=shutdown_and_associate,
            daemon=False,
            name="emergency-shutdown",
        )
        shutdown_thread.start()
        if not latched.wait(timeout=1.0):
            log.critical("Emergency-stop worker did not confirm the safety latch")
        self.cancel()
        return shutdown_thread

    # -- run -------------------------------------------------------------

    def _run(self, params: ExperimentParams) -> None:
        with self._state_lock:
            run_generation = self._run_generation
            association_event = self._run_association_event
        point = params.point
        run_id: Optional[int] = None
        success = False
        terminal_status = "failed"
        message = ""
        record: Optional[RunRecord] = None
        self.last_current = None
        try:
            self._validate_params(params)
            existing = self.db.find_run(point)
            if (
                existing is not None
                and existing.status == "completed"
                and not self.callbacks.confirm_overwrite(point.describe())
            ):
                terminal_status = "cancelled"
                message = "Cancelled: existing result kept."
                return

            # Create the auditable attempt before any gate or bus output is
            # armed.  Safety events during setup can now reference this run.
            run_id = self.db.create_run(point, params.duration_minutes)
            self.safety.active_run_id = run_id
            with self._state_lock:
                if self._run_generation == run_generation:
                    self._run_id_for_audit = run_id
            association_event.set()

            if self._cancelled():
                terminal_status = "cancelled"
                message = "Cancelled before outputs were armed."
                return

            reason = self.safety.trip_reason
            if reason is not None:
                raise SafetyTrip(*reason)

            # The oscilloscope participates in both closed-loop voltage
            # control and the live over-voltage interlock.  Confirm the exact
            # HDO4054 identity on every run before either gate or bus output
            # can be energized; accepting another command dialect here could
            # turn a plausible-looking value into an unsafe control input.
            self._update_status("Verifying LeCroy HDO4054 identity...")
            scope_identity = self.scope.verify_identity()
            log.info("Verified oscilloscope identity: %s", scope_identity)
            if self._cancelled():
                terminal_status = "cancelled"
                message = "Cancelled after scope verification."
                return
            reason = self.safety.trip_reason
            if reason is not None:
                raise SafetyTrip(*reason)

            # Scale the ZVS dwell threshold to this point's target peak.
            # Best effort: the dwell is diagnostic, so losing it must never
            # stop a characterisation run.
            self._apply_zvs_threshold(float(point.voltage_v))

            self._update_status("Arming verified wavegen outputs...")
            if not self.safety.arm_wavegen(point.config):
                raise ConnectionError("Could not arm the wavegen outputs")
            if self._cancelled():
                terminal_status = "cancelled"
                message = "Cancelled after gate arming and before bus enable."
                return
            reason = self.safety.trip_reason
            if reason is not None:
                raise SafetyTrip(*reason)

            # -- bring the bus up -----------------------------------------
            self._update_status("Starting SMU (soft start from 0 V)...")
            if not self.safety.enable_bus():
                raise ConnectionError("Could not enable the SMU output")

            self._update_status(
                f"Peak control: seeking Vds peak {point.voltage_v} V..."
            )
            bus_voltage = self.peak_controller.achieve_peak(
                float(point.voltage_v),
                cancel_check=self._cancelled,
                status=self._update_status,
            )

            # The frequency search runs before the DC voltage tune: it
            # holds the peak on target throughout, so it leaves a well-defined
            # operating point for anything that follows.
            tune_result = None
            if params.tune_frequency:
                self._update_status("Searching gate frequency for minimum P_in...")
                tune_result = self.frequency_tuner.find_minimum(
                    float(point.frequency_hz),
                    float(point.voltage_v),
                    point.config,
                    warm_start_hz=self._prior_tuned_frequency_hz(point),
                    cancel_check=self._cancelled,
                    status=self._update_status,
                )
                bus_voltage = tune_result.bus_voltage_v
                dwell = tune_result.zvs_dwell_fraction
                dwell_text = (
                    "ZVS unmeasured"
                    if dwell is None
                    else f"ZVS dwell {dwell:.3f}"
                )
                self._update_status(
                    f"Frequency: {tune_result.frequency_hz / 1e6:.4f} MHz "
                    f"(P_in {tune_result.input_power_w:.2f} W, "
                    f"{dwell_text}, {len(tune_result.minima_hz)} minima)"
                )
                if tune_result.no_zvs_at_winner:
                    # Recorded against the run, not just logged: a point taken
                    # with no ZVS is still valid data, but it is not the
                    # operating point the search was asked to find.
                    self.db.add_safety_event(
                        self._run_id_for_audit,
                        "frequency_tune_no_zvs",
                        f"Chosen frequency {tune_result.frequency_hz / 1e6:.4f} "
                        "MHz shows no ZVS; the search likely settled outside "
                        "the resonant basin",
                    )
                if self._cancelled():
                    terminal_status = "cancelled"
                    message = "Cancelled after the frequency search."
                    return

            tuned_voltage_v: Optional[float] = None
            if params.tune_voltage:
                self._update_status(
                    "Tuning DC voltage (searching for the ZVS point)..."
                )
                result = self.voltage_tuner.find_minimum(
                    cancel_check=self._cancelled, status=self._update_status
                )
                if result is None:
                    if self._cancelled():
                        raise ExperimentCancelled(
                            "Experiment cancelled during the DC voltage tune."
                        )
                    raise VoltageTuneMeasurementError(
                        "Requested DC voltage tune returned no measurement "
                        "result"
                    )
                tuned_voltage_v = result.tuned_voltage_v
                bus_voltage = result.tuned_voltage_v
                self._update_status(
                    f"DC voltage tuned to {result.tuned_voltage_v:.1f} V "
                    f"(ZVS point, {result.i_min * 1000:.2f} mA)"
                )

            if self._cancelled():
                terminal_status = "cancelled"
                message = "Cancelled before sampling started."
                return

            # -- sampling loop ---------------------------------------------
            self._update_status(f"Running: {point.describe()}")
            self._sampling_active.set()
            try:
                rms_samples = self._sampling_loop(run_id, params)
            finally:
                self._sampling_active.clear()

            if self._cancelled():
                terminal_status = "cancelled"
                message = "Experiment cancelled."
                return

            # -- final readings ---------------------------------------------
            self._update_status("Capturing final readings...")
            self._poll_energized_safety("before final readings")
            readings = self._capture_final_readings(rms_samples, point.config)
            zvs_dwell = self._read_zvs_dwell()
            self._raise_if_cancelled(
                "Experiment cancelled during final readings."
            )
            self._validate_final_readings(
                readings,
                point.config,
                # A requested DC voltage tune deliberately moves the bus away
                # from the pre-tune peak target.  The target is a safe search seed in
                # that mode; the final point must remain finite and within all
                # safety ceilings, but is not forced back to the seed.
                target_vds=(
                    None if tuned_voltage_v is not None else float(point.voltage_v)
                ),
            )
            self._poll_energized_safety("before screenshot capture")
            screenshot = self._capture_screenshot_with_safety_monitoring(
                run_id, point
            )
            self._poll_energized_safety("after screenshot capture")
            self.db.complete_run(
                run_id,
                readings,
                bus_voltage_v=bus_voltage,
                tuned_voltage_v=tuned_voltage_v,
                screenshot_path=str(screenshot) if screenshot else None,
                tuned_frequency_hz=(
                    None if tune_result is None else tune_result.frequency_hz
                ),
                tuned_input_power_w=(
                    None if tune_result is None else tune_result.input_power_w
                ),
                sweep_direction=(
                    None if tune_result is None else tune_result.direction
                ),
                zvs_dwell_fraction=zvs_dwell,
            )
            success = True
            terminal_status = "completed"
            message = "Experiment complete."

        except ExperimentCancelled as exc:
            terminal_status = "cancelled"
            message = str(exc)
        except SafetyTrip as trip:
            terminal_status = "tripped"
            message = f"SAFETY TRIP — {trip}"
        except PeakControlError as exc:
            if self._cancelled() or str(exc).strip().lower() == "cancelled":
                terminal_status = "cancelled"
                message = "Experiment cancelled during peak control."
            else:
                terminal_status = "failed"
                message = str(exc)
                self._report_error("Experiment failed", message)
        except (
            FinalReadingsError,
            SmuLimitError,
            ConnectionError,
            RuntimeError,
            ValueError,
        ) as exc:
            terminal_status = "failed"
            message = str(exc)
            self._report_error("Experiment failed", message)
        except Exception as exc:
            terminal_status = "failed"
            log.exception("experiment failed")
            message = f"Unexpected error: {exc}"
            self._report_error("Experiment failed", message)
        finally:
            # Also releases an E-stop audit worker when validation or overwrite
            # rejection ends this invocation before a run row exists.
            association_event.set()
            self._sampling_active.clear()
            shutdown_ok = self._safe_shutdown()
            if not shutdown_ok and success:
                success = False
                terminal_status = "failed"
                message = (
                    "Experiment measurements completed, but output shutdown "
                    "could not be confirmed."
                )

            # A latched interlock always outranks ordinary cancellation or a
            # nominally completed measurement.  In particular, the E-stop API
            # sets its latch before making cancellation visible to this worker.
            trip_reason = self.safety.trip_reason
            if trip_reason is not None:
                success = False
                terminal_status = "tripped"
                kind, detail = trip_reason
                message = f"SAFETY TRIP — {kind}: {detail}"

            if run_id is not None and not success:
                try:
                    self.db.set_run_status(run_id, terminal_status)
                except Exception:
                    log.exception(
                        "Could not set run %s status to %s",
                        run_id,
                        terminal_status,
                    )

            self.safety.active_run_id = None
            if run_id is not None:
                try:
                    record = self.db.get_run(run_id)
                except Exception:
                    log.exception("Could not load terminal run record %s", run_id)

            outcome = RunOutcome(
                success=success,
                status=terminal_status,
                message=message,
                run_id=run_id,
                record=record,
            )
            with self._state_lock:
                self._last_outcome = outcome
            self._outcome_event.set()

            try:
                self._set_state(ExperimentState.IDLE)
            except Exception:
                log.exception("Engine state callback failed during shutdown")

            if success and record is not None and self.on_run_completed is not None:
                try:
                    self.on_run_completed(record)
                except Exception:
                    log.exception("Run-completed callback failed")

            try:
                self.callbacks.on_finished(success, message)
            except Exception:
                log.exception("Engine finished callback failed")
            bus.publish(
                RunCompletedEvent(
                    success=success,
                    message=message,
                    record=record,
                )
            )

    def _apply_zvs_threshold(self, target_peak_v: float) -> None:
        """Scale the scope's ZVS dwell threshold to this point's target peak.

        The only measurement setting the software writes. A fixed level would
        mean a different fraction of the swing at each matrix voltage, and
        expecting the operator to retune it at every point invites silently
        wrong data. Failure is logged, never raised: the dwell is diagnostic
        and must not be able to stop a characterisation run.
        """
        fraction = self.settings.frequency_tune.zvs_threshold_frac
        level = max(0.0, target_peak_v * fraction)
        if level <= 0.0:
            return
        try:
            if self.scope.set_zvs_threshold(level):
                log.info(
                    "ZVS dwell threshold set to %.2f V (%.1f%% of %.0f V)",
                    level,
                    fraction * 100.0,
                    target_peak_v,
                )
        except Exception:
            log.warning("could not set the ZVS dwell threshold", exc_info=True)

    def _read_zvs_dwell(self) -> Optional[float]:
        """Read the ZVS dwell fraction, or ``None`` if it is unavailable.

        Deliberately not defaulted to zero: "no dwell" means hard switching,
        while "no measurement" means the scope is not configured for it. They
        are opposite conclusions and must stay distinguishable in the data.
        """
        try:
            value = self.scope.zvs_dwell_fraction()
        except Exception:
            log.warning("ZVS dwell read failed", exc_info=True)
            return None
        if value is None:
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        return numeric if math.isfinite(numeric) else None

    def _prior_tuned_frequency_hz(self, point) -> Optional[float]:
        """Warm start from an earlier successful search, if there is one.

        Only a tuned result for this exact point is used. Coss(V) moves the
        resonance between voltage points, so borrowing across them would seed
        the search in the wrong place — the coarse sweep can find it anyway,
        and a wrong warm start narrows the window around the wrong centre.
        """
        try:
            record = self.db.find_run(point)
        except Exception:
            log.exception("could not look up a prior tuned frequency")
            return None
        if record is None or record.status != "completed":
            return None
        tuned = record.tuned_frequency_hz
        if tuned is None:
            return None
        try:
            value = float(tuned)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value > 0.0 else None

    def _sampling_loop(self, run_id: int, params: ExperimentParams) -> list[float]:
        duration_s = params.duration_minutes * 60.0
        interval = self.settings.smu.sample_interval_s
        start = time.monotonic()
        paused_since: Optional[float] = None
        paused_total_s = 0.0
        last_slow_read = 0.0
        rms_samples: list[float] = []
        try:
            measured_frequency = self.wavegen.read_frequency()
        except Exception:
            log.exception("Wavegen frequency read raised at sampling start")
            measured_frequency = None
        if (
            self._is_valid_reading(measured_frequency)
            and measured_frequency is not None
            and float(measured_frequency) > 0
        ):
            actual_frequency_hz: Optional[float] = float(measured_frequency)
            self.safety.record_read_success("wavegen frequency (run)")
        else:
            actual_frequency_hz = None
            self.safety.record_read_failure("wavegen frequency (run)")

        while True:
            if self._cancelled():
                break

            tick = time.monotonic()
            paused = self.state == ExperimentState.PAUSED
            if paused:
                if paused_since is None:
                    paused_since = tick
                current_pause_s = tick - paused_since
            else:
                if paused_since is not None:
                    paused_total_s += tick - paused_since
                    paused_since = None
                current_pause_s = 0.0

            elapsed = tick - start - paused_total_s - current_pause_s
            if elapsed >= duration_s:
                break

            # Vds and compliance are safety-critical and remain independent of
            # whether the primary SMU telemetry read succeeds or the run is
            # paused. Outputs remain energized during pause, so these checks
            # must continue at the normal sampling cadence.
            vds_peak = self.scope.peak_voltage()
            if self._is_valid_reading(vds_peak):
                self.safety.record_read_success("scope peak voltage (run)")
                self.safety.check_sample(vds_peak=vds_peak)
            else:
                vds_peak = None
                self.safety.record_read_failure("scope peak voltage (run)")
            self.safety.check_compliance()

            reading = self.smu.read()
            if (
                reading is None
                or not self._is_valid_reading(reading[0])
                or not self._is_valid_reading(reading[1])
            ):
                self.safety.record_read_failure("SMU read")
                remaining = interval - (time.monotonic() - tick)
                if remaining > 0:
                    self._cancel_event.wait(remaining)
                continue
            self.safety.record_read_success("SMU read")
            smu_volts, amps = reading
            self.safety.check_sample(dc_current=amps)
            self.last_current = amps

            if paused:
                remaining = interval - (time.monotonic() - tick)
                if remaining > 0:
                    self._cancel_event.wait(remaining)
                continue

            rms = self.scope.rms_current()
            if self._is_valid_reading(rms):
                self.safety.record_read_success("scope RMS current")
                assert rms is not None
                rms_samples.append(float(rms))
            else:
                rms = None
                self.safety.record_read_failure("scope RMS current")

            # Slower cross-checks on a relaxed cadence.
            dc_voltage = isw = None
            if tick - last_slow_read >= TIME_SERIES_INTERVAL_S:
                if self.dmm is not None:
                    dc_voltage = self.dmm.dc_voltage()
                    if self._is_valid_reading(dc_voltage):
                        self.safety.record_read_success("DMM DC voltage")
                    else:
                        dc_voltage = None
                        self.safety.record_read_failure("DMM DC voltage")
                else:
                    smu_read = self.smu.read()
                    dc_voltage = smu_read[0] if smu_read else None

                isw = self.scope.isw_rms()
                if self._is_valid_reading(isw):
                    self.safety.record_read_success("scope switch RMS")
                else:
                    isw = None
                    self.safety.record_read_failure("scope switch RMS")
                last_slow_read = tick

            self.db.add_sample(
                run_id,
                _utc_timestamp_ms(),
                amps,
                smu_voltage=smu_volts,
                dc_voltage=dc_voltage,
                rms_current=rms,
                isw_rms=isw,
                elapsed_s=elapsed,
            )
            try:
                self.callbacks.on_sample(elapsed, amps)
            except Exception:
                log.exception("Engine sample callback failed")
            bus.publish(
                SampleAcquiredEvent(
                    elapsed_s=elapsed,
                    amps=amps,
                    smu_voltage=smu_volts,
                    dc_voltage=dc_voltage,
                    rms_current=rms,
                    isw_rms=isw,
                    vds_peak=vds_peak,
                    frequency_hz=actual_frequency_hz,
                )
            )

            remaining = interval - (time.monotonic() - tick)
            if remaining > 0:
                self._cancel_event.wait(remaining)

        return rms_samples

    @staticmethod
    def _is_valid_reading(value: Optional[float]) -> bool:
        if value is None:
            return False
        try:
            return math.isfinite(float(value))
        except (TypeError, ValueError):
            return False

    def _validate_params(self, params: ExperimentParams) -> None:
        point = params.point
        safe_device_name = sanitize_device_name(point.device_name)
        if not safe_device_name or safe_device_name != point.device_name:
            raise ValueError(
                "Device name must be non-empty and contain only letters, "
                "numbers, spaces, underscores, or hyphens"
            )
        if point.config not in (
            "Dual Conduction",
            "Single Conduction",
            "Single Device",
        ):
            raise ValueError(f"Unknown hardware configuration: {point.config}")
        if not math.isfinite(params.duration_minutes) or params.duration_minutes <= 0:
            raise ValueError("Experiment duration must be a finite positive number")
        if (
            not math.isfinite(float(point.frequency_hz))
            or point.frequency_hz <= 0
        ):
            raise ValueError("Wavegen frequency must be positive")
        if (
            not math.isfinite(float(point.duty_pct))
            or not (0.1 <= point.duty_pct <= 99.9)
        ):
            raise ValueError("Duty cycle must be between 0.1% and 99.9%")
        if (
            not math.isfinite(float(point.voltage_v))
            or point.voltage_v <= 0
        ):
            raise ValueError("Vds target must be positive")
        if point.voltage_v > self.settings.safety.max_vds_peak_v:
            raise ValueError(
                f"Vds target {point.voltage_v} V exceeds configured safety limit "
                f"{self.settings.safety.max_vds_peak_v:.1f} V"
            )

    def _poll_energized_safety(
        self,
        phase: str,
        *,
        include_scope: bool = True,
    ) -> None:
        """Poll live limits around energized final operations.

        ``include_scope=False`` is used while the scope is capturing a screen.
        Scope drivers and their VISA/SCPI sessions are not required to support
        concurrent conversations, so the capture worker owns that instrument
        until it returns.  SMU current and compliance remain independently
        monitored in the caller's thread.
        """
        try:
            current = self.smu.measure_dc_current()
        except Exception:
            log.exception("SMU current read raised %s", phase)
            current = None
        current_source = f"SMU current ({phase})"
        if self._is_valid_reading(current):
            assert current is not None
            current_value = float(current)
            self.safety.record_read_success(current_source)
            self.safety.check_sample(dc_current=current_value)
            self.last_current = current_value
        else:
            self.safety.record_read_failure(current_source)

        if include_scope:
            try:
                peak = self.scope.peak_voltage()
            except Exception:
                log.exception("Scope peak-voltage read raised %s", phase)
                peak = None
            peak_source = f"scope peak voltage ({phase})"
            if self._is_valid_reading(peak):
                assert peak is not None
                self.safety.record_read_success(peak_source)
                self.safety.check_sample(vds_peak=float(peak))
            else:
                self.safety.record_read_failure(peak_source)

        self.safety.check_compliance()

    def _capture_final_readings(
        self,
        rms_samples: list[float],
        config: str,
    ) -> FinalReadings:
        def with_retries(
            fetch,
            source: str,
            retries=2,
            delay=0.2,
            *,
            safety_value: Optional[str] = None,
        ):
            for attempt in range(retries + 1):
                self._raise_if_cancelled(
                    "Experiment cancelled during final readings."
                )
                try:
                    value = fetch()
                except Exception:
                    log.exception("Final %s read raised", source)
                    value = None
                self._raise_if_cancelled(
                    "Experiment cancelled during final readings."
                )
                if self._is_valid_reading(value):
                    self.safety.record_read_success(source)
                    numeric = float(value)
                    if safety_value == "dc_current":
                        self.safety.check_sample(dc_current=numeric)
                    elif safety_value == "vds_peak":
                        self.safety.check_sample(vds_peak=numeric)
                    self.safety.check_compliance()
                    return numeric
                self.safety.record_read_failure(source)
                self.safety.check_compliance()
                if attempt < retries:
                    if self._cancel_event.wait(delay):
                        self._raise_if_cancelled(
                            "Experiment cancelled during final readings."
                        )
            return None

        irms = (
            sum(rms_samples) / len(rms_samples)
            if rms_samples
            else with_retries(self.scope.rms_current, "scope RMS current (final)")
        )
        last_current = self.last_current
        if self._is_valid_reading(last_current):
            assert last_current is not None
            iin = float(last_current)
            self.safety.record_read_success("SMU current (final)")
            self.safety.check_sample(dc_current=iin)
            self.safety.check_compliance()
        else:
            iin = with_retries(
                self.smu.measure_dc_current,
                "SMU current (final)",
                safety_value="dc_current",
            )
        isw_rms = (
            with_retries(
                self.scope.isw_rms,
                "scope switch RMS (final)",
            )
            if config == "Dual Conduction"
            else None
        )
        vin_getter = (
            self.dmm.dc_voltage
            if self.dmm is not None
            else (lambda: (self.smu.read() or (None, None))[0])
        )
        return FinalReadings(
            vin=with_retries(vin_getter, "DC voltage (final)"),
            iin=iin,
            fsw_hz=with_retries(
                self.wavegen.read_frequency,
                "wavegen frequency (final)",
            ),
            irms=irms,
            vds_pk=with_retries(
                self.scope.peak_voltage,
                "scope peak voltage (final)",
                retries=3,
                delay=0.3,
                safety_value="vds_peak",
            ),
            isw_rms=isw_rms,
        )

    def _raise_if_cancelled(self, message: str) -> None:
        if self._cancelled():
            raise ExperimentCancelled(message)

    def _validate_final_readings(
        self,
        readings: FinalReadings,
        config: str,
        *,
        target_vds: Optional[float],
    ) -> None:
        required = {
            "DC input voltage": readings.vin,
            "DC input current": readings.iin,
            "switching frequency": readings.fsw_hz,
            "RMS current": readings.irms,
            "Vds peak": readings.vds_pk,
        }
        if config == "Dual Conduction":
            required["switch-current RMS"] = readings.isw_rms
        missing = [
            name
            for name, value in required.items()
            if not self._is_valid_reading(value)
        ]
        if missing:
            raise FinalReadingsError(
                "Required final readings unavailable or invalid: "
                + ", ".join(missing)
            )
        self.safety.check_sample(
            dc_current=readings.iin,
            vds_peak=readings.vds_pk,
        )
        self.safety.check_compliance()
        assert readings.vds_pk is not None
        tolerance = float(self.settings.peak_control.tolerance_v)
        if (
            target_vds is not None
            and abs(float(readings.vds_pk) - target_vds) > tolerance
        ):
            raise FinalReadingsError(
                f"Final Vds peak {readings.vds_pk:.1f} V is outside "
                f"target {target_vds:.1f} V ± {tolerance:.1f} V"
            )

    def _capture_screenshot(self, run_id: int, point) -> Optional[object]:
        frequency = freq_label(point.frequency_hz).replace(".", "p")
        dest = (
            self.settings.screenshots_dir
            / point.device_name
            / (
                f"run{run_id}_{point.config.replace(' ', '')}_"
                f"{point.temperature_c}C_{frequency}_"
                f"{point.voltage_v}V_{point.duty_pct}duty.png"
            )
        )
        return self.scope.screenshot(dest)

    def _capture_screenshot_with_safety_monitoring(
        self,
        run_id: int,
        point,
    ) -> Optional[object]:
        """Capture a scope screen without suspending independent interlocks.

        Screenshot APIs are synchronous and can spend seconds waiting for a
        binary transfer.  The scope call therefore runs in one owned worker
        while this experiment thread polls the SMU.  The scope itself is not
        queried concurrently: many VISA/SCPI sessions serialize poorly (or
        not at all) across threads.

        Python cannot safely terminate a thread blocked inside a vendor I/O
        call.  The worker is consequently non-daemon and always joined before
        this method returns or raises.  Production scope transports have their
        own I/O timeout; once the call unwinds, a pending cancellation, safety
        trip, or capture exception is propagated in that priority order.
        """
        finished = threading.Event()
        results: list[Optional[object]] = []
        errors: list[BaseException] = []

        def capture() -> None:
            try:
                results.append(self._capture_screenshot(run_id, point))
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()

        capture_thread = threading.Thread(
            target=capture,
            daemon=False,
            name=f"scope-screenshot-{run_id}",
        )
        capture_thread.start()

        pending_cancel: Optional[ExperimentCancelled] = None
        pending_error: Optional[BaseException] = None
        poll_interval = min(
            SCREENSHOT_SAFETY_POLL_MAX_S,
            max(0.01, float(self.settings.smu.sample_interval_s)),
        )
        try:
            while not finished.wait(poll_interval):
                reason = self.safety.trip_reason
                if reason is not None:
                    # A trip has already removed power.  Avoid racing its
                    # shutdown traffic with more SMU queries while the scope
                    # worker finishes its bounded I/O operation.
                    if not isinstance(pending_error, SafetyTrip):
                        pending_error = SafetyTrip(*reason)
                    continue

                if self._cancelled() and pending_cancel is None:
                    pending_cancel = ExperimentCancelled(
                        "Experiment cancelled during final capture."
                    )

                try:
                    self._poll_energized_safety(
                        "during screenshot capture",
                        include_scope=False,
                    )
                except SafetyTrip as trip:
                    pending_error = trip
                except BaseException as exc:
                    pending_error = exc
        finally:
            capture_thread.join()

        reason = self.safety.trip_reason
        if reason is not None:
            raise SafetyTrip(*reason)
        if pending_error is not None:
            raise pending_error
        if self._cancelled():
            raise pending_cancel or ExperimentCancelled(
                "Experiment cancelled during final capture."
            )
        if errors:
            raise errors[0]
        return results[0] if results else None

    def _safe_shutdown(self) -> bool:
        """End every run with the bus and gate outputs confirmed off."""
        smu_ok = True
        wavegen_ok = True
        try:
            if self.smu.output_is_on:
                self.smu.ramp_to(0.0)
            if not self.smu.output_off():
                raise ConnectionError("SMU output-off command was not acknowledged")
        except Exception:
            log.exception("Controlled SMU shutdown failed; using emergency-off")
            try:
                smu_ok = bool(self.smu.emergency_off())
            except Exception:
                smu_ok = False
                log.exception("SMU emergency-off raised")

        try:
            self.wavegen.outputs_off()
        except Exception:
            wavegen_ok = False
            log.exception("Wavegen outputs could not be confirmed off")

        if not (smu_ok and wavegen_ok):
            log.critical(
                "Experiment shutdown was not confirmed (smu=%s, wavegen=%s)",
                smu_ok,
                wavegen_ok,
            )
        return smu_ok and wavegen_ok
