"""The rig operations themselves, separated from the window that triggers them.

Each of these energises or de-energises hardware on a worker thread and reports
back through the dispatcher. That contract is the reason this is a class rather
than a set of functions: it holds the coordinator, the worker pool and the
dispatcher explicitly, so it is visible at a glance that nothing here touches
Tcl and everything comes back through one route.

What it needs from the window — dialogs, the status line, control refresh — is
declared as :class:`RigUi` and injected. The window implements it; nothing here
imports tkinter.
"""

from __future__ import annotations

import logging
import threading
from functools import partial
from typing import Any, Callable, Optional, Protocol

from gan_fet.core.autotune import FrequencyRampAborted
from gan_fet.core.safety import SafetyTrip
from gan_fet.ui.operations.background import Dispatcher, run_in_background
from gan_fet.ui.operations.rig_ops import raise_if_aborted
from gan_fet.ui.operations.worker_pool import WorkerPool
from gan_fet.ui.widgets import OperationCoordinator, OperationToken

log = logging.getLogger(__name__)

#: How long emergency stop waits for each worker it is trying to stop. Long
#: enough for an instrument conversation to finish, short enough that a stuck
#: one is reported rather than waited on indefinitely.
_EMERGENCY_JOIN_S = 10.0


def high_risk_warnings(
    previous_duty: Optional[int], new_duty: int
) -> list[str]:
    """Changes worth stopping the operator for before they are applied.

    Duty cycle is here because it changes how long the switch conducts each
    cycle, which changes the thermal load on a device that may already be at
    temperature — and unlike frequency or configuration, it is easy to alter
    by one keystroke without meaning to.

    An unknown previous duty is not a change. The first apply after startup
    has nothing to compare against, and warning about it every time would
    teach the operator to dismiss the dialog without reading it.
    """
    if previous_duty is None or new_duty == previous_duty:
        return []
    return [f"Duty cycle change from {previous_duty}% to {new_duty}%."]


def high_risk_prompt(warnings: list[str]) -> tuple[str, str]:
    """The confirmation shown for the changes in ``warnings``."""
    return (
        "Confirm High-Risk Change",
        "The following high-risk changes were detected:\n\n"
        + "\n".join(f"- {w}" for w in warnings)
        + "\n\nThese changes may damage the device. Proceed?",
    )


def reset_confirmation(trip_reason) -> tuple[str, str]:
    """The prompt shown before clearing the safety latch.

    The latched reason is quoted back. Clearing an interlock without being
    reminded what set it is how an operator resets the same fault twice
    instead of fixing it, and the reason is the only record of that on screen.
    """
    detail = (
        f"\n\nLatched reason: {trip_reason[0]} — {trip_reason[1]}"
        if trip_reason
        else ""
    )
    return (
        "Reset Safety Interlock",
        "Confirm that the rig has been inspected and both outputs are OFF."
        f"{detail}\n\nReset the safety latch?",
    )


class RigUi(Protocol):
    """What a rig operation needs from the window it runs in."""

    def set_status(self, message: str) -> None:
        """Show a line in the status bar."""

    def hardware_online(self, action: str) -> bool:
        """Whether ``action`` may proceed, reporting to the operator if not.

        The window owns this because refusing also moves the operator to the
        Configuration tab, which is where the problem is fixed.
        """

    def report_busy(self, active_kind: Optional[str]) -> None:
        """Tell the operator which operation currently holds the rig."""

    def show_info(self, title: str, message: str) -> None:
        """Report something that is not a problem."""

    def show_error(self, title: str, message: str) -> None:
        """Report an operation that failed."""

    def confirm(self, title: str, message: str, *, dangerous: bool = False) -> bool:
        """Ask a yes/no question. ``dangerous`` marks it as a warning."""

    def is_closing(self) -> bool:
        """Whether the application is shutting down."""

    def refresh_controls(self) -> None:
        """Re-apply enabled state after an operation starts or ends."""

    def refresh_confirm(self) -> None:
        """Re-resolve the confirm/autotune presentation."""

    def smu_state_changed(self) -> None:
        """Re-read the SMU status line and telemetry."""

    def set_status_async(self, message: str) -> None:
        """Set the status line from a worker thread."""

    def set_tuning(self, active: bool, *, autotune: bool = False) -> None:
        """Show that a tuner is running, or has stopped.

        ``autotune`` distinguishes the two: both mark the tuner busy, but only
        autotune restyles its own button.
        """

    def voltage_tune_stopping(self) -> None:
        """Show that a cooperative stop has been asked for."""

    def flash(self, message: str) -> None:
        """Show a brief confirmation that disappears on its own."""


class RigOperations:
    """Starts, tracks and completes the operator's rig actions."""

    def __init__(
        self,
        *,
        ui: RigUi,
        operations: OperationCoordinator,
        pool: WorkerPool,
        dispatcher: Dispatcher,
        safety,
        smu,
        wavegen_controller,
        settings,
        engine,
        hardware_labels: dict[str, str],
    ) -> None:
        self.ui = ui
        self.operations = operations
        self.pool = pool
        self.dispatcher = dispatcher
        self.safety = safety
        self.smu = smu
        self.wavegen_controller = wavegen_controller
        self.settings = settings
        self.engine = engine
        # Bound after construction: AutoSequence needs the engine, which needs
        # this. Only emergency stop uses it, and only after the UI exists.
        self.sequence: Any = None
        self._emergency_worker: Optional[threading.Thread] = None
        self._hardware_labels = hardware_labels

    # -- operation lifecycle ------------------------------------------------

    def begin(
        self, kind: str, *, show_busy: bool = True
    ) -> Optional[OperationToken]:
        """Claim the rig for ``kind``, or report why that is not possible.

        Returns ``None`` when the operation must not start. Refusing during
        shutdown comes first and silently: the window is going away, and a
        dialog raised against it would have nowhere to go.
        """
        if self.ui.is_closing():
            return None
        action = self._hardware_labels.get(kind)
        if action is not None and not self.ui.hardware_online(action):
            return None
        token = self.operations.try_begin(kind)
        if token is None and show_busy:
            self.ui.report_busy(self.operations.active_kind)
        self.ui.refresh_controls()
        return token

    def finish(self, token: OperationToken) -> None:
        """Release the rig and let the controls catch up."""
        self.operations.finish(token)
        self.ui.refresh_controls()
        self.ui.refresh_confirm()

    def run(self, work, on_done, *, name: str, on_error=None):
        """Run one operation off the Tk thread and report how it ended."""
        return run_in_background(
            pool=self.pool,
            dispatcher=self.dispatcher,
            work=work,
            on_done=on_done,
            name=name,
            on_error=on_error,
        )

    # -- bus off -------------------------------------------------------------

    def bus_off(self) -> None:
        """Ramp the bus down and disarm both outputs, confirming by readback.

        Every step is verified rather than assumed: ``output_off()`` has to
        acknowledge, and the final state is re-read from both instruments. An
        instrument that accepted a command it did not carry out is exactly the
        failure this exists to catch, and it is the reason the emergency path
        below is reachable at all.
        """
        if not self.ui.hardware_online(self._hardware_labels["bus_off"]):
            return
        token = self.begin("bus_off")
        if token is None:
            return

        def work() -> None:
            self.smu.ramp_to(0.0, cancel_check=token.cancel_event.is_set)
            if not self.smu.output_off():
                raise RuntimeError("SMU output-off was not acknowledged")
            self.wavegen_controller.disarm_outputs()
            if self.smu.output_is_on or self.wavegen_controller.outputs_armed:
                raise RuntimeError("Output readback did not confirm the rig is safe")

        def fall_back(error: BaseException) -> Optional[BaseException]:
            """Take the emergency path on the worker thread, while the rig is
            still in whatever state the failure left it."""
            if not self.safety.shutdown_outputs():
                if not self.safety.emergency_stop():
                    return RuntimeError(
                        f"{error}; emergency output-off was unconfirmed"
                    )
            return None

        self.run(
            work,
            lambda error: self.on_bus_off_done(token, error),
            name="bus-off",
            on_error=fall_back,
        )
        self.ui.set_status("Ramping bus to 0 V...")

    def on_bus_off_done(
        self, token: OperationToken, error: Optional[BaseException]
    ) -> None:
        self.finish(token)
        self.ui.smu_state_changed()
        self.ui.set_status(
            "Bus and gate outputs are OFF."
            if error is None
            else f"Bus Off required the emergency shutdown path: {error}"
        )

    # -- reset the safety latch ----------------------------------------------

    def reset_safety(self) -> None:
        """Clear the safety latch, after confirming the rig has been inspected.

        Nothing here makes the rig safe; it only stops the software refusing to
        energise it. That is why the operator is asked to confirm rather than
        told, and why the latched reason is quoted back to them.
        """
        if not self.ui.hardware_online(self._hardware_labels["reset_safety"]):
            return
        if not getattr(self.safety, "is_tripped", False):
            # Not an error. Resetting nothing is harmless, but reporting it
            # matters: an operator who believes they just cleared a trip, and
            # did not, will read the next refusal as the software misbehaving.
            self.ui.show_info(
                "Reset Safety", "No safety trip is currently latched."
            )
            return
        title, message = reset_confirmation(
            getattr(self.safety, "trip_reason", None)
        )
        if not self.ui.confirm(title, message, dangerous=True):
            return
        token = self.begin("reset_safety")
        if token is None:
            return
        self.run(
            self.safety.reset_trip,
            lambda error: self.on_reset_safety_done(token, error),
            name="reset-safety",
        )

    def on_reset_safety_done(
        self, token: OperationToken, error: Optional[BaseException]
    ) -> None:
        self.finish(token)
        if error is not None:
            # The reset is allowed to fail — SafetyMonitor refuses one that
            # races a fresh trip — and the operator has to be told, because
            # the rig is still latched and they were expecting otherwise.
            self.ui.show_error("Reset Safety", f"Safety reset failed: {error}")
            self.ui.set_status("Safety latch remains active.")
        else:
            self.ui.set_status("Safety latch reset. Rig remains disarmed.")
        self.ui.smu_state_changed()

    # -- apply the wavegen settings -----------------------------------------

    def apply_wavegen(
        self,
        *,
        config: str,
        frequency_hz: int,
        duty_pct: int,
        after_success: Optional[Callable[[], object]] = None,
    ) -> bool:
        """Push the selected gate settings to the wavegen.

        ``after_success`` lets a caller queue what it actually wanted to do —
        start a run, launch a search — behind the apply, so the operator
        answers one prompt instead of two. It returns ``object`` rather than
        ``None`` so callbacks that report a value can be passed unwrapped; the
        result is deliberately discarded.

        Returns whether the operation started, not whether it succeeded.
        """
        if not self.ui.hardware_online(self._hardware_labels["apply_wavegen"]):
            return False
        warnings = high_risk_warnings(
            self.wavegen_controller.applied_duty, duty_pct
        )
        if warnings and not self.ui.confirm(
            *high_risk_prompt(warnings), dangerous=True
        ):
            return False
        token = self.begin("apply_wavegen")
        if token is None:
            return False
        self.ui.set_status("Applying wavegen settings...")

        def work() -> None:
            self.wavegen_controller.apply(
                config,
                frequency_hz,
                duty_pct,
                duty_rate_pct_s=self.settings.wavegen.duty_ramp_rate_pct_s,
                freq_rate_khz_s=self.settings.wavegen.freq_ramp_rate_khz_s,
                cancel_check=token.cancel_event.is_set,
                status=self.ui.set_status_async,
            )
            # A cancelled ramp leaves the wavegen part-way between the old
            # settings and the new ones, so it has not been applied even
            # though nothing raised.
            if token.cancel_event.is_set():
                raise InterruptedError("wavegen configuration cancelled")

        self.run(
            work,
            lambda error: self.on_apply_wavegen_done(
                token, error, after_success=after_success
            ),
            name="apply-wavegen",
        )
        return True

    def on_apply_wavegen_done(
        self,
        token: OperationToken,
        error: Optional[BaseException],
        *,
        after_success: Optional[Callable[[], object]] = None,
    ) -> None:
        self.finish(token)
        if error is not None:
            if isinstance(error, InterruptedError):
                # The operator stopped it. Nothing to report but the fact.
                self.ui.set_status("Wavegen configuration cancelled.")
            else:
                self.ui.show_error(
                    "Wavegen Error", f"Failed to configure wavegen: {error}"
                )
                self.ui.set_status("Wavegen configuration failed.")
            return
        self.ui.set_status("Wavegen parameters applied.")
        self.ui.refresh_confirm()
        # Only on success, and not while closing. Whatever was queued behind
        # the apply assumed the wavegen now holds the selected settings; if it
        # does not, running it would drive the rig from parameters nobody
        # chose.
        if after_success is not None and not self.ui.is_closing():
            after_success()

    # -- emergency stop -------------------------------------------------------

    def emergency_stop(self) -> None:
        """Latch the interlock and bring both outputs down, now.

        **Deliberately does not claim the rig.** Every other operation goes
        through :meth:`begin`, which refuses when something else is running —
        and something else running is precisely when this is needed. The one
        thing it will not do is start a second time while the first is still
        going, because that would race two shutdown sequences against each
        other.
        """
        if self._emergency_worker is not None and self._emergency_worker.is_alive():
            return

        self.ui.set_status(
            "EMERGENCY STOP requested — shutting outputs down..."
        )
        self.ui.refresh_controls()
        sequence_was_active = self.operations.active_kind == "sequence" or bool(
            self.sequence is not None and self.sequence.active
        )

        def work() -> None:
            # These APIs latch the interlock *before* exposing cancellation to
            # the experiment worker. Cancelling first would race a real E-stop
            # into an ordinary "cancelled" run outcome, losing the record that
            # the rig tripped.
            owner = (
                self.sequence
                if sequence_was_active and self.sequence is not None
                else self.engine
            )
            shutdown_thread = owner.request_emergency_stop()
            self.operations.cancel_active()
            shutdown_thread.join(timeout=_EMERGENCY_JOIN_S)

            # Every confirmation is collected rather than raising at the first
            # failure: after an emergency stop the operator needs to know
            # everything that could not be confirmed, not just the first thing.
            problems = []
            if shutdown_thread.is_alive():
                problems.append("emergency output shutdown did not finish")
            if not self.safety.is_tripped:
                problems.append("emergency-stop safety latch was not confirmed")
            if self.sequence is not None and not self.sequence.join(
                timeout=_EMERGENCY_JOIN_S
            ):
                problems.append("auto-sequence worker did not stop")
            self.engine.join(timeout=_EMERGENCY_JOIN_S)
            if self.engine.is_busy():
                problems.append("experiment worker did not stop")
            if self.smu.output_is_on or self.wavegen_controller.outputs_armed:
                problems.append("output shutdown could not be confirmed")
            if problems:
                raise RuntimeError("; ".join(problems))

        self._emergency_worker = self.run(
            work, self.on_emergency_stop_done, name="emergency-stop"
        )

    def on_emergency_stop_done(self, error: Optional[BaseException]) -> None:
        self.ui.set_status(
            "EMERGENCY STOP complete. Outputs are OFF; safety is latched."
            if error is None
            else f"EMERGENCY STOP encountered an error: {error}"
        )
        self.ui.smu_state_changed()
        self.ui.refresh_controls()

    # -- autotune -------------------------------------------------------------

    def autotune(self, candidate: tuple[float, str, int], config: str) -> None:
        """Ramp the gate to a frequency this device was tuned to before.

        ``candidate`` is ``(frequency_hz, source_config, source_temperature)``:
        the reading is quoted back on success because a frequency borrowed from
        a different configuration or temperature is a weaker result than one
        measured at this point, and the operator should see which they got.
        """
        token = self.begin("autotune")
        if token is None:
            return
        target, src_config, src_temp = candidate
        self.ui.set_tuning(True, autotune=True)

        def work() -> None:
            self.wavegen_controller.ramp_to_frequency(
                target,
                config,
                rate_khz_s=self.settings.voltage_tune.autotune_freq_rate_khz_s,
                cancel_check=token.cancel_event.is_set,
                status=self.ui.set_status_async,
            )
            actual = self.wavegen_controller.tuned_freq_hz
            if token.cancel_event.is_set():
                raise InterruptedError("frequency recall cancelled")
            # Readback, not "the call returned": a ramp that stopped short
            # leaves the gate at a frequency nobody chose.
            if actual is None or abs(actual - target) > 1.0:
                raise RuntimeError(
                    "Frequency recall stopped before the target frequency "
                    "was applied"
                )

        self.run(
            work,
            lambda error: self.on_autotune_done(
                token, target, src_config, src_temp, error
            ),
            name="autotune",
        )

    def on_autotune_done(
        self,
        token: OperationToken,
        target: float,
        src_config: str,
        src_temp: int,
        error: Optional[BaseException],
    ) -> None:
        self.ui.set_tuning(False, autotune=True)
        self.finish(token)
        if error is not None:
            if isinstance(error, FrequencyRampAborted):
                # Not a trip. The ramp stopped short deliberately, so the gate
                # sits at an intermediate frequency and the rig is still live —
                # which is why this says where it stopped and what to do, not
                # just that it failed.
                self.ui.show_error(
                    "Frequency Recall Stopped",
                    f"{error}\n\nThe gate is left at the frequency reached, "
                    "not the target. Reduce the bus voltage before retrying.",
                )
                self.ui.set_status(
                    f"Frequency recall stopped at "
                    f"{error.frequency_hz / 1e6:.4f} MHz "
                    f"(Vds peak {error.peak_v:.0f} V)"
                )
            elif not isinstance(error, InterruptedError):
                self.ui.show_error(
                    "Recall Tuned Frequency",
                    f"Frequency recall failed: {error}",
                )
                self.ui.set_status("Frequency recall failed.")
            else:
                self.ui.set_status("Frequency recall cancelled.")
        else:
            self.ui.flash(
                f"Tuned frequency {int(target)} Hz applied\n"
                f"({src_config} @ {src_temp}\u00b0C)"
            )
            self.ui.set_status(
                f"Tuned frequency {int(target)} Hz applied from "
                f"{src_config} @ {src_temp}\u00b0C"
            )
        self.ui.refresh_confirm()

    # -- the manual DC voltage tune -------------------------------------------

    def request_voltage_tune_stop(self) -> bool:
        """Ask a running DC voltage tune to stop, returning whether one was running.

        Cooperative: the search checks the cancel flag between every step that
        arms or energises something, so it stops at a point where the rig is in
        a known state rather than wherever the command happened to land.
        """
        if self.operations.active_kind != "voltage_tune":
            return False
        self.operations.cancel_active()
        self.ui.voltage_tune_stopping()
        self.ui.set_status("Stopping the DC voltage tune safely...")
        return True

    def launch_voltage_tune(self, *, target_peak_v: float, config: str) -> None:
        """Arm, energise, hold the peak, and sweep the bus for the ZVS point.

        The interlock guard runs between every step that arms or energises,
        because cancellation and trips both arrive *during* the previous step.
        Whatever happens, the rig is made safe on the way out — that cleanup is
        a ``finally`` and behaves differently either side of a failure, which
        is why this operation does not use the shared background helper.
        """
        token = self.begin("voltage_tune")
        if token is None:
            return
        self.ui.set_tuning(True)
        self.ui.set_status("Preparing the HDO4054-verified DC voltage tune...")

        abort_check = partial(
            raise_if_aborted,
            cancelled=token.cancel_event.is_set,
            safety=self.safety,
            trip_error=SafetyTrip,
            what="DC voltage tune",
        )

        def worker() -> None:
            error = result = None
            try:
                abort_check()

                self.ui.set_status_async("Verifying LeCroy HDO4054 identity...")
                identity = self.engine.scope.verify_identity()
                log.info(
                    "Verified oscilloscope identity for manual voltage tune: %s",
                    identity,
                )

                abort_check()

                armed = self.safety.arm_wavegen(config)
                if not armed or not self.wavegen_controller.outputs_armed:
                    raise RuntimeError("Wavegen outputs did not confirm armed")

                abort_check()

                if not self.safety.enable_bus():
                    raise ConnectionError("Could not enable the SMU output")
                self.engine.peak_controller.achieve_peak(
                    float(target_peak_v),
                    cancel_check=token.cancel_event.is_set,
                    status=self.ui.set_status_async,
                )
                result = self.engine.voltage_tuner.find_minimum(
                    cancel_check=token.cancel_event.is_set,
                    status=self.ui.set_status_async,
                )
                if token.cancel_event.is_set():
                    raise InterruptedError("DC voltage tune cancelled")
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                error = exc
            finally:
                error = self._make_safe_after_voltage_tune(error)
            self.dispatcher.post(self.on_voltage_tune_done, token, result, error)

        self.pool.start(worker, name="voltage-tune")

    def _make_safe_after_voltage_tune(
        self, error: Optional[BaseException]
    ) -> Optional[BaseException]:
        """Bring the rig down after a DC voltage tune, whichever way it ended.

        The two paths differ, and deliberately. After a *successful* search the
        ordinary shutdown is expected to work, so a failure to confirm it is
        itself a new problem worth reporting. After a *failed* one the rig is
        already in an unknown state, so the safety monitor's shutdown is used
        and only an unconfirmed emergency stop is added to what went wrong.
        """
        if error is None:
            try:
                self.smu.ramp_to(0.0)
                if not self.smu.output_off():
                    raise RuntimeError("SMU output-off was not acknowledged")
                self.wavegen_controller.disarm_outputs()
                if self.smu.output_is_on or self.wavegen_controller.outputs_armed:
                    raise RuntimeError(
                        "Output readback did not confirm the rig is safe"
                    )
            except BaseException as cleanup_exc:
                error = RuntimeError(
                    f"DC voltage tune cleanup was not confirmed: {cleanup_exc}"
                )
                try:
                    # A failed output-off confirmation is a safety event even
                    # if the emergency retry succeeds.
                    if not self.safety.emergency_stop():
                        error = RuntimeError(
                            f"{error}; emergency output-off was unconfirmed"
                        )
                except Exception:
                    log.exception(
                        "Emergency fallback failed after voltage-tune cleanup"
                    )
            return error

        try:
            if not self.safety.shutdown_outputs():
                if not self.safety.emergency_stop():
                    return RuntimeError(
                        f"{error}; emergency output-off was unconfirmed"
                    )
        except Exception:
            log.exception(
                "Failed to make outputs safe after a voltage-tune error"
            )
        return error

    def on_voltage_tune_done(self, token: OperationToken, result, error) -> None:
        self.ui.set_tuning(False)
        self.finish(token)
        self.ui.smu_state_changed()
        if error is not None:
            if not isinstance(error, InterruptedError):
                self.ui.show_error(
                    "Tune DC Voltage", f"DC voltage tune failed: {error}"
                )
                self.ui.set_status("DC voltage tune failed.")
            else:
                self.ui.set_status("DC voltage tune cancelled; outputs are OFF.")
        elif result is None:
            self.ui.set_status("DC voltage tune found no improvement.")
        else:
            self.ui.set_status(
                f"DC voltage tuned to {result.tuned_voltage_v:.1f} V "
                f"(ZVS point, {result.i_min * 1000:.2f} mA). "
                "Bus and gate outputs are OFF."
            )
