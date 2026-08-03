"""Auto sequence: run every not-yet-completed matrix point at the selected
frequency.

With the SMU sourcing the bus, voltage changes are fully automatic (peak
control per step). The only remaining manual step is the thermal chamber, so
the operator is prompted once per temperature change.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from gan_fet.core.autotune import WavegenController, find_prior_tuned_frequency
from gan_fet.core.experiment import ExperimentEngine
from gan_fet.core.models import ExperimentParams, MatrixPoint
from gan_fet.storage.db import Database

log = logging.getLogger(__name__)


def _noop(*_args, **_kwargs):
    return None


@dataclass
class SequenceCallbacks:
    """All invoked from the sequence worker thread."""

    on_status: Callable[[str], None] = _noop
    on_step: Callable[[int, int, MatrixPoint], None] = _noop      # (index, total, point)
    on_finished: Callable[[bool, str], None] = _noop
    # Blocking prompt; return False to abort the sequence.
    prompt_operator: Callable[[str, str], bool] = field(default=lambda _t, _m: True)


@dataclass
class PlannedTestPoint:
    point: MatrixPoint
    is_completed: bool


@dataclass
class PlanSummary:
    points: list[PlannedTestPoint]
    total_count: int
    completed_count: int
    pending_count: int
    estimated_duration_minutes: float
    temperature_changes: int

    @property
    def pending_points(self) -> list[MatrixPoint]:
        return [p.point for p in self.points if not p.is_completed]

    @property
    def all_points(self) -> list[MatrixPoint]:
        return [p.point for p in self.points]


def build_plan(
    db: Database,
    device_name: str,
    frequency_hz: int,
    configs: list[str],
    duties: list[int],
    voltages: list[int],
    temperatures: list[int],
) -> list[MatrixPoint]:
    """All (temp, config, duty, voltage) combinations without a completed run,
    grouped by temperature so the chamber is adjusted as rarely as possible."""
    return build_matrix_plan(
        db,
        device_name=device_name,
        frequencies_hz=[frequency_hz],
        configs=configs,
        duties=duties,
        voltages=voltages,
        temperatures=temperatures,
        include_completed=False,
    ).pending_points


def build_matrix_plan(
    db: Database,
    device_name: str,
    frequencies_hz: list[int],
    configs: list[str],
    duties: list[int],
    voltages: list[int],
    temperatures: list[int],
    include_completed: bool = False,
    duration_minutes_per_point: float = 1.0,
) -> PlanSummary:
    """Build a custom test plan matrix across multiple frequencies and parameters.
    Points are grouped by temperature so the chamber is adjusted as rarely as possible.
    """
    completed_set = set()
    for freq in frequencies_hz:
        done_tuple_set = db.completed_points(device_name, freq)
        for (config, duty, voltage, temp) in done_tuple_set:
            completed_set.add((freq, config, duty, voltage, temp))

    planned_points: list[PlannedTestPoint] = []
    total_count = 0
    completed_count = 0
    pending_count = 0

    for temp in temperatures:
        for freq in frequencies_hz:
            for config in configs:
                for duty in duties:
                    for voltage in voltages:
                        key = (freq, config, duty, voltage, temp)
                        is_done = key in completed_set
                        total_count += 1
                        if is_done:
                            completed_count += 1
                        else:
                            pending_count += 1

                        if not is_done or include_completed:
                            mp = MatrixPoint(
                                device_name=device_name,
                                config=config,
                                frequency_hz=freq,
                                duty_pct=duty,
                                temperature_c=temp,
                                voltage_v=voltage,
                            )
                            planned_points.append(PlannedTestPoint(point=mp, is_completed=is_done))

    temp_changes = 0
    prev_temp = None
    for item in planned_points:
        if prev_temp is None or item.point.temperature_c != prev_temp:
            temp_changes += 1
            prev_temp = item.point.temperature_c

    est_duration = len(planned_points) * duration_minutes_per_point

    return PlanSummary(
        points=planned_points,
        total_count=total_count,
        completed_count=completed_count,
        pending_count=pending_count,
        estimated_duration_minutes=est_duration,
        temperature_changes=temp_changes,
    )



class AutoSequence:
    def __init__(
        self,
        db: Database,
        engine: ExperimentEngine,
        wavegen_controller: WavegenController,
        all_configs: list[str],
        callbacks: Optional[SequenceCallbacks] = None,
    ):
        self.db = db
        self.engine = engine
        self.wavegen_controller = wavegen_controller
        self.all_configs = all_configs
        self.callbacks = callbacks or SequenceCallbacks()
        self._cancel = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(
        self, plan: list[MatrixPoint], duration_minutes: float, tune_voltage: bool
    ) -> bool:
        with self._lifecycle_lock:
            if self.active or self.engine.is_busy() or not plan:
                return False
            self._cancel.clear()
            thread = threading.Thread(
                target=self._run,
                args=(plan, duration_minutes, tune_voltage),
                daemon=False,
                name="auto-sequence",
            )
            self._thread = thread
            thread.start()
        return True

    def cancel(self) -> None:
        self._cancel.set()
        self.engine.cancel()

    def join(self, timeout: Optional[float] = None) -> bool:
        """Bounded wait for the sequence worker; returns whether it stopped."""
        with self._lifecycle_lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def cancel_and_join(self, timeout: Optional[float] = None) -> bool:
        self.cancel()
        return self.join(timeout)

    def request_emergency_stop(self) -> threading.Thread:
        self._cancel.set()
        return self.engine.request_emergency_stop()

    def _cancelled(self) -> bool:
        return self._cancel.is_set()

    def _run(self, plan: list[MatrixPoint], duration_minutes: float, tune_voltage: bool) -> None:
        total = len(plan)
        completed = 0
        success = True
        previous_temp: Optional[int] = None
        cb = self.callbacks
        try:
            for index, point in enumerate(plan, start=1):
                if self._cancelled():
                    success = False
                    break
                if self.engine.safety.is_tripped:
                    success = False
                    cb.on_status(
                        "Auto sequence stopped: safety trip is latched; "
                        "operator reset is required."
                    )
                    break

                cb.on_step(index, total, point)
                cb.on_status(f"Auto sequence {index}/{total}: {point.describe()}")

                # Manual step: thermal chamber.
                if previous_temp is None or point.temperature_c != previous_temp:
                    if not cb.prompt_operator(
                        "Temperature Adjustment",
                        f"Set the chamber temperature to {point.temperature_c} °C, "
                        "then press OK to continue.",
                    ):
                        success = False
                        break
                    previous_temp = point.temperature_c

                # Gate drive: apply selection, then ramp to a previously
                # measured tuned frequency when one exists.
                self.wavegen_controller.apply(
                    point.config,
                    point.frequency_hz,
                    point.duty_pct,
                    cancel_check=self._cancelled,
                    status=cb.on_status,
                )
                prior = find_prior_tuned_frequency(self.db, point, self.all_configs)
                if prior is not None and not self._cancelled():
                    tuned_freq, src_config, src_temp = prior
                    cb.on_status(
                        f"Autotuning to {tuned_freq / 1e6:.2f} MHz "
                        f"(from {src_config} @ {src_temp} °C)..."
                    )
                    self.wavegen_controller.ramp_to_frequency(
                        tuned_freq, point.config,
                        cancel_check=self._cancelled, status=cb.on_status,
                    )

                if self._cancelled():
                    success = False
                    break

                if not self.engine.start(
                    ExperimentParams(
                        point=point,
                        duration_minutes=duration_minutes,
                        tune_voltage=tune_voltage,
                        # Always, and deliberately not a flag. A sequence is a
                        # characterisation matrix, and a point measured off
                        # resonance is not a characterisation of anything —
                        # Coss moves resonance with amplitude, so the right
                        # frequency can only be found at the operating point.
                        #
                        # This used to be omitted, so it defaulted to False and
                        # every sequenced point ran at its nominal frequency.
                        # The ramp above hid it: recalling a previously tuned
                        # frequency looks like tuning, but only works once some
                        # other run has already found one. On a fresh device
                        # there is nothing to recall and the whole matrix ran
                        # off resonance, with tuned_frequency_hz NULL
                        # throughout.
                        tune_frequency=True,
                    )
                ):
                    success = False
                    break
                outcome = self.engine.wait_until_idle()

                if self._cancelled():
                    success = False
                    break
                if outcome is None or not outcome.success:
                    success = False
                    detail = (
                        outcome.message
                        if outcome is not None
                        else "engine did not return a terminal outcome"
                    )
                    cb.on_status(
                        f"Auto sequence stopped after {index}/{total}: {detail}"
                    )
                    break
                completed += 1
                cb.on_status(f"Auto sequence progress: {completed}/{total} complete.")
        except Exception as exc:
            log.exception("auto sequence failed")
            success = False
            cb.on_status(f"Auto sequence error: {exc}")
        finally:
            try:
                self.wavegen_controller.disarm_outputs()
            except Exception:
                log.exception("Could not confirm wavegen disarmed at sequence end")
            if self._cancelled():
                cb.on_finished(False, f"Auto sequence cancelled ({completed}/{total} done).")
            elif success:
                cb.on_finished(True, f"Auto sequence complete ({completed}/{total}).")
            else:
                cb.on_finished(False, f"Auto sequence stopped ({completed}/{total} done).")
