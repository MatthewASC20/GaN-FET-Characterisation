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
    done = db.completed_points(device_name, frequency_hz)
    plan: list[MatrixPoint] = []
    for temp in temperatures:
        for config in configs:
            for duty in duties:
                for voltage in voltages:
                    if (config, duty, voltage, temp) not in done:
                        plan.append(MatrixPoint(
                            device_name=device_name, config=config,
                            frequency_hz=frequency_hz, duty_pct=duty,
                            temperature_c=temp, voltage_v=voltage,
                        ))
    return plan


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
        self._thread: Optional[threading.Thread] = None

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(
        self, plan: list[MatrixPoint], duration_minutes: float, find_zvs: bool
    ) -> bool:
        if self.active or self.engine.is_busy() or not plan:
            return False
        self._cancel.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(plan, duration_minutes, find_zvs),
            daemon=True,
            name="auto-sequence",
        )
        self._thread.start()
        return True

    def cancel(self) -> None:
        self._cancel.set()
        self.engine.cancel()

    def _cancelled(self) -> bool:
        return self._cancel.is_set()

    def _run(self, plan: list[MatrixPoint], duration_minutes: float, find_zvs: bool) -> None:
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
                    point.config, point.frequency_hz, point.duty_pct
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
                        find_zvs=find_zvs,
                    )
                ):
                    success = False
                    break
                self.engine.wait_until_idle()

                if self._cancelled():
                    success = False
                    break
                completed += 1
                cb.on_status(f"Auto sequence progress: {completed}/{total} complete.")
        except Exception as exc:
            log.exception("auto sequence failed")
            success = False
            cb.on_status(f"Auto sequence error: {exc}")
        finally:
            if self._cancelled():
                cb.on_finished(False, f"Auto sequence cancelled ({completed}/{total} done).")
            elif success:
                cb.on_finished(True, f"Auto sequence complete ({completed}/{total}).")
            else:
                cb.on_finished(False, f"Auto sequence stopped ({completed}/{total} done).")
