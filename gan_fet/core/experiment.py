"""Experiment engine: drives one characterisation run in a worker thread.

Flow per run:
  1. overwrite check against the database (one result per matrix point)
  2. SMU on (soft start from 0 V) and closed-loop ramp until the scope's
     Vds peak equals the selected test voltage (replaces the manual bench
     supply + validation dialog of v1)
  3. optional ZVS search (minimise DC input current vs bus voltage)
  4. timed sampling loop → `samples` table (+ live UI callbacks)
  5. final readings + oscilloscope screenshot → `runs` row
  6. safe shutdown (bus ramped to 0 V, output off)

The engine has no tkinter dependency: every interaction goes through
EngineCallbacks, and the UI is responsible for marshalling to its thread.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from gan_fet.core.models import (
    ExperimentParams,
    ExperimentState,
    FinalReadings,
    RunRecord,
)
from gan_fet.core.safety import SafetyMonitor, SafetyTrip
from gan_fet.core.voltage_control import PeakControlError, PeakVoltageController
from gan_fet.core.zvs import ZvsTuner
from gan_fet.instruments.multimeter import Sdm3055
from gan_fet.instruments.oscilloscope import Mso44
from gan_fet.instruments.smu import Keithley2400, SmuLimitError
from gan_fet.instruments.wavegen import Sdg6022x
from gan_fet.settings import Settings
from gan_fet.storage.db import Database

log = logging.getLogger(__name__)

TIME_SERIES_INTERVAL_S = 10.0   # cadence for the slower DMM / Isw readings


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


class ExperimentEngine:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        smu: Keithley2400,
        scope: Mso44,
        dmm: Sdm3055,
        wavegen: Sdg6022x,
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
        self.zvs_tuner = ZvsTuner(smu, settings.zvs, safety)

        self._state = ExperimentState.IDLE
        self._state_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.last_current: Optional[float] = None

    # -- state ---------------------------------------------------------

    @property
    def state(self) -> ExperimentState:
        with self._state_lock:
            return self._state

    def _set_state(self, state: ExperimentState) -> None:
        with self._state_lock:
            self._state = state
        self.callbacks.on_state(state)

    def _cancelled(self) -> bool:
        return self.state == ExperimentState.CANCELLED

    def is_busy(self) -> bool:
        return self.state != ExperimentState.IDLE

    def start(self, params: ExperimentParams) -> bool:
        if self.is_busy():
            return False
        self._set_state(ExperimentState.RUNNING)
        self._thread = threading.Thread(
            target=self._run, args=(params,), daemon=True, name="experiment"
        )
        self._thread.start()
        return True

    def toggle_pause(self) -> None:
        with self._state_lock:
            if self._state == ExperimentState.RUNNING:
                self._state = ExperimentState.PAUSED
            elif self._state == ExperimentState.PAUSED:
                self._state = ExperimentState.RUNNING
        self.callbacks.on_state(self.state)

    def cancel(self) -> None:
        with self._state_lock:
            if self._state in (ExperimentState.RUNNING, ExperimentState.PAUSED):
                self._state = ExperimentState.CANCELLED

    def wait_until_idle(self, poll_s: float = 0.2) -> None:
        while self.is_busy() or (self._thread and self._thread.is_alive()):
            time.sleep(poll_s)

    # -- run -------------------------------------------------------------

    def _run(self, params: ExperimentParams) -> None:
        """Phases: confirm → energise → sample → finalise, with the bus
        always brought back down in `finally` whatever happens."""
        point = params.point
        run_id: Optional[int] = None
        success = False
        message = ""
        try:
            if not self._confirm_overwrite_if_needed(point):
                message = "Cancelled: existing result kept."
                return

            bus_voltage, v_zvs = self._energise_bus(params)
            if self._cancelled():
                message = "Cancelled before sampling started."
                return

            run_id = self.db.create_run(point, params.duration_minutes)
            self.safety.active_run_id = run_id
            self.callbacks.on_status(f"Running: {point.describe()}")
            rms_samples = self._sampling_loop(run_id, params)

            if self._cancelled():
                self.db.set_run_status(run_id, "cancelled")
                message = "Experiment cancelled."
                return

            self._finalise_run(run_id, point, rms_samples, bus_voltage, v_zvs)
            success = True
            message = "Experiment complete."

        except SafetyTrip as trip:
            message = f"SAFETY TRIP — {trip}"
            if run_id is not None:
                self.db.set_run_status(run_id, "tripped")
        except (PeakControlError, SmuLimitError, ConnectionError) as exc:
            message = str(exc)
            self.callbacks.report_error("Experiment failed", message)
            if run_id is not None:
                self.db.set_run_status(run_id, "failed")
        except Exception as exc:
            log.exception("experiment failed")
            message = f"Unexpected error: {exc}"
            self.callbacks.report_error("Experiment failed", message)
            if run_id is not None:
                self.db.set_run_status(run_id, "failed")
        finally:
            self._safe_shutdown()
            self.safety.active_run_id = None
            self._set_state(ExperimentState.IDLE)
            self.callbacks.on_finished(success, message)

    def _confirm_overwrite_if_needed(self, point) -> bool:
        """One stored result per matrix point: ask before replacing one."""
        if self.db.find_run(point) is None:
            return True
        return bool(self.callbacks.confirm_overwrite(point.describe()))

    def _energise_bus(self, params: ExperimentParams) -> tuple[float, Optional[float]]:
        """Soft-start the SMU, drive the scope-measured Vds peak to the
        selected target, then optionally settle on the ZVS point.

        Returns (bus_voltage, v_zvs) where v_zvs is None unless a search ran.
        """
        point = params.point
        self.callbacks.on_status("Starting SMU (soft start from 0 V)...")
        if not self.smu.output_on():
            raise ConnectionError("Could not enable the SMU output")

        self.callbacks.on_status(
            f"Peak control: seeking Vds peak {point.voltage_v} V..."
        )
        bus_voltage = self.peak_controller.achieve_peak(
            float(point.voltage_v),
            cancel_check=self._cancelled,
            status=self.callbacks.on_status,
        )

        v_zvs: Optional[float] = None
        if params.find_zvs and not self._cancelled():
            self.callbacks.on_status("Searching for ZVS point...")
            result = self.zvs_tuner.find_minimum(
                cancel_check=self._cancelled, status=self.callbacks.on_status
            )
            if result is not None:
                v_zvs = bus_voltage = result.v_zvs
                self.callbacks.on_status(
                    f"ZVS point: {result.v_zvs:.1f} V ({result.i_min * 1000:.2f} mA)"
                )
        return bus_voltage, v_zvs

    def _finalise_run(
        self,
        run_id: int,
        point,
        rms_samples: list[float],
        bus_voltage: float,
        v_zvs: Optional[float],
    ) -> None:
        """Capture end-of-run instrument readings, screenshot and store them."""
        self.callbacks.on_status("Capturing final readings...")
        readings = self._capture_final_readings(rms_samples)
        screenshot = self._capture_screenshot(run_id, point)
        self.db.complete_run(
            run_id,
            readings,
            bus_voltage_v=bus_voltage,
            v_zvs=v_zvs,
            screenshot_path=str(screenshot) if screenshot else None,
        )
        if self.on_run_completed is not None:
            record = self.db.get_run(run_id)
            if record is not None:
                self.on_run_completed(record)

    def _sampling_loop(self, run_id: int, params: ExperimentParams) -> list[float]:
        duration_s = params.duration_minutes * 60.0
        interval = self.settings.smu.sample_interval_s
        start = time.time()
        last_slow_read = 0.0
        rms_samples: list[float] = []

        while (time.time() - start) < duration_s:
            if self._cancelled():
                break
            while self.state == ExperimentState.PAUSED:
                time.sleep(0.1)
                if self._cancelled():
                    return rms_samples

            tick = time.time()

            reading = self.smu.read()
            if reading is None:
                self.safety.record_read_failure("SMU read")
                time.sleep(interval)
                continue
            self.safety.record_read_success()
            smu_volts, amps = reading
            self.safety.check_sample(dc_current=amps)
            self.safety.check_compliance()
            self.last_current = amps

            rms = self.scope.rms_current()
            if rms is not None:
                rms_samples.append(rms)

            # Slower cross-checks on a relaxed cadence.
            dc_voltage = isw = None
            if tick - last_slow_read >= TIME_SERIES_INTERVAL_S:
                dc_voltage = self.dmm.dc_voltage()
                isw = self.scope.isw_rms()
                last_slow_read = tick

            elapsed = tick - start
            self.db.add_sample(
                run_id,
                time.strftime("%Y-%m-%d %H:%M:%S"),
                amps,
                smu_voltage=smu_volts,
                dc_voltage=dc_voltage,
                rms_current=rms,
                isw_rms=isw,
            )
            self.callbacks.on_sample(elapsed, amps)

            remaining = interval - (time.time() - tick)
            if remaining > 0:
                time.sleep(remaining)

        return rms_samples

    def _capture_final_readings(self, rms_samples: list[float]) -> FinalReadings:
        def with_retries(fetch, retries=2, delay=0.2):
            for attempt in range(retries + 1):
                value = fetch()
                if value is not None:
                    return value
                if attempt < retries:
                    time.sleep(delay)
            return None

        irms = (
            sum(rms_samples) / len(rms_samples)
            if rms_samples
            else with_retries(self.scope.rms_current)
        )
        return FinalReadings(
            vin=with_retries(self.dmm.dc_voltage),
            iin=self.last_current,
            fsw_hz=with_retries(self.wavegen.read_frequency),
            irms=irms,
            vds_pk=with_retries(self.scope.peak_voltage, retries=3, delay=0.3),
            isw_rms=with_retries(self.scope.isw_rms),
        )

    def _capture_screenshot(self, run_id: int, point) -> Optional[object]:
        dest = (
            self.settings.screenshots_dir
            / point.device_name
            / (
                f"run{run_id}_{point.config.replace(' ', '')}_"
                f"{point.temperature_c}C_{point.frequency_hz // 1_000_000}MHz_"
                f"{point.voltage_v}V_{point.duty_pct}duty.png"
            )
        )
        return self.scope.screenshot(dest)

    def _safe_shutdown(self) -> None:
        """End of every run: bus back to 0 V, output off."""
        try:
            if self.smu.output_is_on:
                self.smu.ramp_to(0.0)
                self.smu.output_off()
        except Exception:
            self.smu.emergency_off()
