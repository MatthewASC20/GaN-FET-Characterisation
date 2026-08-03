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
from typing import Callable, Optional, Protocol

from gan_fet.ui.operations.background import Dispatcher, run_in_background
from gan_fet.ui.operations.worker_pool import WorkerPool
from gan_fet.ui.widgets import OperationCoordinator, OperationToken

log = logging.getLogger(__name__)


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
