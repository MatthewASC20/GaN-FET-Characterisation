"""Claiming the rig, releasing it, and making it safe.

`RigOperations` holds the coordinator, the worker pool and the dispatcher, so
the threading contract is visible in one place. What the window supplies —
dialogs, the status line, control refresh — is injected, which is what lets all
of this be exercised without a display.
"""

from __future__ import annotations

from typing import Optional

from types import SimpleNamespace

from gan_fet.ui.operations.rig_control import (
    RigOperations,
    high_risk_warnings,
    reset_confirmation,
)
from gan_fet.ui.operations.worker_pool import WorkerPool
from gan_fet.ui.widgets import OperationCoordinator

LABELS = {
    "apply_wavegen": "apply wavegen settings",
    "bus_off": "control the bus output",
    "zvs": "run a ZVS search",
    "reset_safety": "reset the safety interlock",
}


class _Ui:
    """Records what the window would have been asked to show."""

    def __init__(self, *, online: bool = True, closing: bool = False) -> None:
        self.online = online
        self.closing = closing
        self.status: list[str] = []
        self.busy_reports: list[Optional[str]] = []
        self.online_checks: list[str] = []
        self.refreshes = 0
        self.confirms = 0
        self.smu_updates = 0
        self.infos: list[tuple[str, str]] = []
        self.errors: list[tuple[str, str]] = []
        self.confirmations: list[tuple[str, str, bool]] = []
        self.answer = True

    def set_status(self, message: str) -> None:
        self.status.append(message)

    def hardware_online(self, action: str) -> bool:
        self.online_checks.append(action)
        return self.online

    def report_busy(self, active_kind: Optional[str]) -> None:
        self.busy_reports.append(active_kind)

    def show_info(self, title: str, message: str) -> None:
        self.infos.append((title, message))

    def show_error(self, title: str, message: str) -> None:
        self.errors.append((title, message))

    def confirm(self, title, message, *, dangerous=False) -> bool:
        self.confirmations.append((title, message, dangerous))
        return self.answer

    def is_closing(self) -> bool:
        return self.closing

    def refresh_controls(self) -> None:
        self.refreshes += 1

    def refresh_confirm(self) -> None:
        self.confirms += 1

    def smu_state_changed(self) -> None:
        self.smu_updates += 1

    def set_status_async(self, message: str) -> None:
        self.status.append(message)


class _Dispatcher:
    def post(self, func, *args, **kwargs):
        func(*args, **kwargs)


class _Smu:
    def __init__(self, *, acknowledges: bool = True, really_off: bool = True):
        self.acknowledges = acknowledges
        self.really_off = really_off
        self.output_is_on = True
        self.ramped_to: list[float] = []

    def ramp_to(self, volts, *, cancel_check=None) -> None:
        self.ramped_to.append(volts)

    def output_off(self) -> bool:
        if self.acknowledges and self.really_off:
            self.output_is_on = False
        return self.acknowledges


class _Wavegen:
    def __init__(
        self,
        *,
        really_disarms: bool = True,
        applied_duty: Optional[int] = None,
        apply_error: Optional[BaseException] = None,
    ) -> None:
        self.outputs_armed = True
        self.really_disarms = really_disarms
        self.applied_duty = applied_duty
        self.apply_error = apply_error
        self.applied: list[tuple] = []

    def disarm_outputs(self) -> None:
        if self.really_disarms:
            self.outputs_armed = False

    def apply(self, config, frequency, duty, **kwargs) -> None:
        self.applied.append((config, frequency, duty))
        if self.apply_error is not None:
            raise self.apply_error


class _Safety:
    def __init__(
        self,
        *,
        shutdown_ok: bool = True,
        estop_ok: bool = True,
        is_tripped: bool = False,
        trip_reason=None,
        reset_error: Optional[BaseException] = None,
    ):
        self.shutdown_ok = shutdown_ok
        self.estop_ok = estop_ok
        self.is_tripped = is_tripped
        self.trip_reason = trip_reason
        self.reset_error = reset_error
        self.shutdowns = 0
        self.estops = 0
        self.resets = 0

    def reset_trip(self) -> None:
        self.resets += 1
        if self.reset_error is not None:
            raise self.reset_error

    def shutdown_outputs(self) -> bool:
        self.shutdowns += 1
        return self.shutdown_ok

    def emergency_stop(self) -> bool:
        self.estops += 1
        return self.estop_ok


class _Engine:
    """The experiment engine, as far as emergency stop is concerned."""

    def __init__(self, *, busy_after_stop: bool = False, order=None) -> None:
        self.busy_after_stop = busy_after_stop
        self.order = order if order is not None else []

    def request_emergency_stop(self):
        self.order.append("latch")
        return _FinishedThread()

    def join(self, timeout=None) -> None:
        pass

    def is_busy(self) -> bool:
        return self.busy_after_stop


class _Sequence:
    def __init__(self, *, active: bool = False, stops: bool = True, order=None):
        self.active = active
        self.stops = stops
        self.order = order if order is not None else []

    def request_emergency_stop(self):
        self.order.append("latch")
        return _FinishedThread()

    def join(self, timeout=None) -> bool:
        return self.stops


class _FinishedThread:
    def __init__(self, *, finishes: bool = True) -> None:
        self.finishes = finishes

    def join(self, timeout=None) -> None:
        pass

    def is_alive(self) -> bool:
        return not self.finishes


SETTINGS = SimpleNamespace(
    wavegen=SimpleNamespace(duty_ramp_rate_pct_s=5.0, freq_ramp_rate_khz_s=200.0)
)


def _build(ui=None, smu=None, wavegen=None, safety=None, engine=None):
    ui = ui or _Ui()
    rig = RigOperations(
        ui=ui,
        operations=OperationCoordinator(),
        pool=WorkerPool(),
        dispatcher=_Dispatcher(),
        safety=safety or _Safety(),
        smu=smu or _Smu(),
        wavegen_controller=wavegen or _Wavegen(),
        settings=SETTINGS,
        engine=engine or _Engine(),
        hardware_labels=LABELS,
    )
    return rig, ui


# -- claiming and releasing the rig -------------------------------------------


def test_an_idle_rig_can_be_claimed():
    rig, ui = _build()
    assert rig.begin("bus_off") is not None
    assert ui.refreshes == 1


def test_a_second_claim_is_refused_and_names_what_holds_the_rig():
    rig, ui = _build()
    rig.begin("bus_off")
    assert rig.begin("zvs") is None
    assert ui.busy_reports == ["bus_off"]


def test_shutdown_refuses_silently():
    """A dialog raised against a window that is going away has nowhere to go,
    and the operator has already asked to leave."""
    rig, ui = _build(_Ui(closing=True))
    assert rig.begin("bus_off") is None
    assert ui.busy_reports == []
    assert ui.online_checks == []


def test_offline_hardware_refuses_before_the_rig_is_claimed():
    """Otherwise the coordinator would hold a token for an operation that
    never started, and every later command would be refused as busy."""
    rig, ui = _build(_Ui(online=False))
    assert rig.begin("bus_off") is None
    assert ui.online_checks == ["control the bus output"]
    assert rig.operations.active_kind is None


def test_an_operation_with_no_hardware_label_skips_the_online_check():
    """Generating a report touches no instrument."""
    rig, ui = _build()
    assert rig.begin("report") is not None
    assert ui.online_checks == []


def test_finishing_releases_the_rig_and_refreshes_both_presentations():
    rig, ui = _build()
    token = rig.begin("bus_off")
    assert token is not None
    rig.finish(token)
    assert rig.operations.active_kind is None
    assert ui.confirms == 1
    assert rig.begin("zvs") is not None, "the rig should be claimable again"


# -- bus off -------------------------------------------------------------------


def _bus_off(**kwargs):
    rig, ui = _build(**kwargs)
    rig.bus_off()
    # The work runs on a real worker. In the application the dispatcher
    # marshals completion onto the Tk thread, so ordering against the
    # "ramping" status is fixed; here it is not, and the tests below assert
    # that a message was shown rather than that it was shown last.
    rig.pool.join_all(2.0)
    return rig, ui


def _said(ui, fragment: str) -> bool:
    return any(fragment in message for message in ui.status)


def test_bus_off_ramps_down_and_confirms_both_outputs():
    smu, wavegen = _Smu(), _Wavegen()
    rig, ui = _bus_off(smu=smu, wavegen=wavegen)
    assert smu.ramped_to == [0.0]
    assert not smu.output_is_on and not wavegen.outputs_armed
    assert _said(ui, "Bus and gate outputs are OFF.")
    assert ui.smu_updates == 1


def test_an_unacknowledged_output_off_takes_the_emergency_path():
    """An instrument that accepted a command it did not carry out is the
    failure this exists to catch."""
    safety = _Safety()
    rig, ui = _bus_off(smu=_Smu(acknowledges=False), safety=safety)
    assert safety.shutdowns == 1
    assert _said(ui, "emergency shutdown path")


def test_a_readback_that_does_not_confirm_takes_the_emergency_path():
    """output_off() returning True is not evidence the output is off."""
    safety = _Safety()
    rig, ui = _bus_off(smu=_Smu(really_off=False), safety=safety)
    assert safety.shutdowns == 1
    assert _said(ui, "emergency shutdown path")


def test_a_gate_that_stays_armed_takes_the_emergency_path():
    safety = _Safety()
    rig, ui = _bus_off(wavegen=_Wavegen(really_disarms=False), safety=safety)
    assert safety.shutdowns == 1


def test_a_failed_shutdown_escalates_to_emergency_stop():
    safety = _Safety(shutdown_ok=False)
    rig, ui = _bus_off(smu=_Smu(acknowledges=False), safety=safety)
    assert safety.estops == 1
    assert _said(ui, "emergency shutdown path")


def test_an_unconfirmed_emergency_stop_is_reported_as_such():
    """The worst case, and the one the operator most needs to see: the rig may
    still be energised and nothing has confirmed otherwise."""
    safety = _Safety(shutdown_ok=False, estop_ok=False)
    rig, ui = _bus_off(smu=_Smu(acknowledges=False), safety=safety)
    assert _said(ui, "emergency output-off was unconfirmed")


def test_bus_off_releases_the_rig_even_when_it_fails():
    """Otherwise a failed Bus Off would leave the rig unclaimable, and the
    retry the operator immediately reaches for would be refused as busy."""
    rig, _ui = _bus_off(smu=_Smu(acknowledges=False))
    assert rig.operations.active_kind is None


def test_bus_off_refuses_when_the_hardware_is_offline():
    rig, ui = _bus_off(ui=_Ui(online=False))
    assert ui.status == []
    assert rig.operations.active_kind is None


# -- resetting the safety latch ------------------------------------------------
#
# Nothing here makes the rig safe; it only stops the software refusing to
# energise it. That asymmetry is what the guards are for.


def _reset(ui=None, safety=None):
    safety = safety or _Safety(is_tripped=True)
    rig, ui = _build(ui=ui, safety=safety)
    rig.reset_safety()
    rig.pool.join_all(2.0)
    return rig, ui, safety


def test_resetting_a_latched_trip_clears_it():
    rig, ui, safety = _reset()
    assert safety.resets == 1
    assert _said(ui, "Safety latch reset. Rig remains disarmed.")


def test_resetting_when_nothing_is_latched_is_reported_not_attempted():
    """An operator who believes they just cleared a trip, and did not, will
    read the next refusal as the software misbehaving."""
    rig, ui, safety = _reset(safety=_Safety(is_tripped=False))
    assert safety.resets == 0
    assert ui.infos == [("Reset Safety", "No safety trip is currently latched.")]
    assert rig.operations.active_kind is None


def test_the_confirmation_quotes_the_latched_reason():
    """Clearing an interlock without being reminded what set it is how the
    same fault gets reset twice instead of fixed."""
    safety = _Safety(
        is_tripped=True,
        trip_reason=("overcurrent", "DC input current exceeded limit"),
    )
    rig, ui, _safety = _reset(safety=safety)
    _title, message, dangerous = ui.confirmations[0]
    assert "overcurrent" in message
    assert "DC input current exceeded limit" in message
    assert dangerous, "clearing an interlock is not a routine confirmation"


def test_the_confirmation_still_asks_when_the_reason_is_unknown():
    rig, ui, safety = _reset(safety=_Safety(is_tripped=True, trip_reason=None))
    assert ui.confirmations, "an unknown reason must not skip the confirmation"
    assert safety.resets == 1


def test_declining_the_confirmation_resets_nothing_and_claims_nothing():
    ui = _Ui()
    ui.answer = False
    rig, ui, safety = _reset(ui=ui)
    assert safety.resets == 0
    assert rig.operations.active_kind is None


def test_a_failed_reset_says_the_latch_is_still_active():
    """SafetyMonitor refuses a reset that races a fresh trip. The operator was
    expecting the opposite and has to be told."""
    safety = _Safety(is_tripped=True, reset_error=RuntimeError("trip landed mid-reset"))
    rig, ui, _safety = _reset(safety=safety)
    assert ui.errors and "trip landed mid-reset" in ui.errors[0][1]
    assert _said(ui, "Safety latch remains active.")


def test_a_failed_reset_still_releases_the_rig():
    safety = _Safety(is_tripped=True, reset_error=RuntimeError("nope"))
    rig, _ui, _safety = _reset(safety=safety)
    assert rig.operations.active_kind is None


def test_the_confirmation_text_is_built_from_the_reason():
    title, message = reset_confirmation(("overcurrent", "1.2 A"))
    assert title == "Reset Safety Interlock"
    assert "inspected and both outputs are OFF" in message
    assert "overcurrent — 1.2 A" in message
    assert "Latched reason" not in reset_confirmation(None)[1]


# -- applying the wavegen settings --------------------------------------------


def _apply(ui=None, wavegen=None, **kwargs):
    wavegen = wavegen or _Wavegen()
    rig, ui = _build(ui=ui, wavegen=wavegen)
    request = {"config": "Single Device", "frequency_hz": 6_000_000, "duty_pct": 50}
    request.update(kwargs)
    started = rig.apply_wavegen(**request)
    rig.pool.join_all(2.0)
    return rig, ui, wavegen, started


def test_applying_pushes_the_selected_settings():
    rig, ui, wavegen, started = _apply()
    assert started
    assert wavegen.applied == [("Single Device", 6_000_000, 50)]
    assert _said(ui, "Wavegen parameters applied.")


def test_an_unchanged_duty_asks_nothing():
    """Warning on every apply teaches the operator to dismiss the dialog
    without reading it."""
    rig, ui, _wavegen, _started = _apply(wavegen=_Wavegen(applied_duty=50))
    assert ui.confirmations == []


def test_a_duty_change_is_confirmed_first():
    """Duty changes how long the switch conducts each cycle, so it changes the
    thermal load on a device that may already be at temperature."""
    rig, ui, wavegen, _started = _apply(wavegen=_Wavegen(applied_duty=40))
    assert ui.confirmations, "a duty change must be confirmed"
    _title, message, dangerous = ui.confirmations[0]
    assert "40% to 50%" in message
    assert dangerous


def test_declining_a_duty_change_applies_nothing():
    ui = _Ui()
    ui.answer = False
    rig, ui, wavegen, started = _apply(ui=ui, wavegen=_Wavegen(applied_duty=40))
    assert not started
    assert wavegen.applied == []
    assert rig.operations.active_kind is None


def test_the_first_apply_after_startup_is_not_a_change():
    """Nothing to compare against yet."""
    assert high_risk_warnings(None, 50) == []
    assert high_risk_warnings(50, 50) == []
    assert high_risk_warnings(40, 50)


def test_a_failed_apply_is_reported_and_releases_the_rig():
    wavegen = _Wavegen(apply_error=RuntimeError("wavegen did not answer"))
    rig, ui, _wavegen, _started = _apply(wavegen=wavegen)
    assert ui.errors and "wavegen did not answer" in ui.errors[0][1]
    assert _said(ui, "Wavegen configuration failed.")
    assert rig.operations.active_kind is None


def test_a_cancelled_apply_is_not_reported_as_an_error():
    """The operator stopped it. A dialog would be telling them what they just
    did."""
    wavegen = _Wavegen(apply_error=InterruptedError("cancelled"))
    rig, ui, _wavegen, _started = _apply(wavegen=wavegen)
    assert ui.errors == []
    assert _said(ui, "Wavegen configuration cancelled.")


def test_what_was_queued_behind_a_successful_apply_runs():
    ran = []
    rig, ui, _wavegen, _started = _apply(after_success=lambda: ran.append(True))
    assert ran == [True]


def test_nothing_queued_runs_after_a_failed_apply():
    """It assumed the wavegen now holds the selected settings. If it does not,
    running it would drive the rig from parameters nobody chose."""
    ran = []
    wavegen = _Wavegen(apply_error=RuntimeError("nope"))
    rig, ui, _wavegen, _started = _apply(
        wavegen=wavegen, after_success=lambda: ran.append(True)
    )
    assert ran == []


def test_nothing_queued_runs_while_the_window_is_closing():
    ran = []
    ui = _Ui()
    rig, _ui, wavegen, _started = _apply(
        ui=ui, after_success=lambda: ran.append(True)
    )
    assert ran == [True]
    # Same again, but the window went away while the ramp was in flight.
    ui2 = _Ui()
    rig2, _ui2 = _build(ui=ui2, wavegen=_Wavegen())
    token = rig2.begin("apply_wavegen")
    assert token is not None
    ui2.closing = True
    later = []
    rig2.on_apply_wavegen_done(token, None, after_success=lambda: later.append(True))
    assert later == []


# -- emergency stop ------------------------------------------------------------


def _estop(ui=None, engine=None, safety=None, smu=None, wavegen=None,
           sequence=None):
    rig, ui = _build(
        ui=ui,
        engine=engine,
        safety=safety or _Safety(is_tripped=True),
        smu=smu or _Smu(really_off=True),
        wavegen=wavegen,
    )
    if smu is None:
        rig.smu.output_is_on = False
    if wavegen is None:
        rig.wavegen_controller.outputs_armed = False
    rig.sequence = sequence
    rig.emergency_stop()
    rig.pool.join_all(3.0)
    return rig, ui


def test_emergency_stop_reports_success_when_everything_confirms():
    rig, ui = _estop()
    assert _said(ui, "EMERGENCY STOP complete. Outputs are OFF; safety is latched.")


def test_the_latch_is_requested_before_cancellation_becomes_visible():
    """These APIs latch the interlock before exposing cancellation to the
    experiment worker. Cancelling first would race a real E-stop into an
    ordinary "cancelled" run outcome, losing the record that the rig tripped.
    """
    order: list[str] = []
    engine = _Engine(order=order)
    rig, _ui = _build(engine=engine, safety=_Safety(is_tripped=True))
    rig.smu.output_is_on = False
    rig.wavegen_controller.outputs_armed = False
    original = rig.operations.cancel_active
    rig.operations.cancel_active = lambda: (  # type: ignore[method-assign]
        order.append("cancel"), original()
    )[1]
    rig.emergency_stop()
    rig.pool.join_all(3.0)
    assert order.index("latch") < order.index("cancel")


def test_emergency_stop_does_not_claim_the_rig():
    """Every other operation goes through begin(), which refuses when
    something else is running — and something else running is exactly when
    this is needed."""
    rig, _ui = _build(safety=_Safety(is_tripped=True))
    rig.smu.output_is_on = False
    rig.wavegen_controller.outputs_armed = False
    held = rig.begin("bus_off")
    assert held is not None
    rig.emergency_stop()
    rig.pool.join_all(3.0)
    assert rig._emergency_worker is not None


def test_a_second_press_while_one_is_running_is_ignored():
    """Two shutdown sequences racing each other is worse than one.

    The first press is held inside request_emergency_stop so that the second
    genuinely lands while it is still in flight — otherwise the first would
    finish first and the second would legitimately start a new one.
    """
    import threading

    started, release = threading.Event(), threading.Event()
    latches: list[str] = []

    class _SlowEngine(_Engine):
        def request_emergency_stop(self):
            latches.append("latch")
            started.set()
            release.wait(3.0)
            return _FinishedThread()

    rig, _ui = _build(engine=_SlowEngine(), safety=_Safety(is_tripped=True))
    rig.smu.output_is_on = False
    rig.wavegen_controller.outputs_armed = False
    rig.emergency_stop()
    assert started.wait(3.0), "the first press should have reached the engine"
    rig.emergency_stop()
    release.set()
    rig.pool.join_all(3.0)
    assert latches == ["latch"], "the second press started another shutdown"


def test_an_unlatched_safety_is_reported():
    """The whole point of an emergency stop is that the interlock is set
    afterwards. If it is not, nothing else about the outcome matters."""
    rig, ui = _estop(safety=_Safety(is_tripped=False))
    assert _said(ui, "safety latch was not confirmed")


def test_an_output_still_on_is_reported():
    smu = _Smu()
    smu.output_is_on = True
    rig, ui = _estop(smu=smu)
    assert _said(ui, "output shutdown could not be confirmed")


def test_every_unconfirmed_thing_is_reported_not_just_the_first():
    """After an emergency stop the operator needs to know everything that
    could not be confirmed."""
    smu = _Smu()
    smu.output_is_on = True
    rig, ui = _estop(safety=_Safety(is_tripped=False), smu=smu)
    problems = [m for m in ui.status if "encountered an error" in m]
    assert problems
    assert "safety latch was not confirmed" in problems[0]
    assert "output shutdown could not be confirmed" in problems[0]


def test_an_active_sequence_is_stopped_at_the_sequence_level():
    """Stopping the engine alone would let the sequence start the next point
    after an emergency stop."""
    order: list[str] = []
    sequence = _Sequence(active=True, order=order)
    rig, ui = _estop(sequence=sequence)
    assert order == ["latch"], "the sequence, not the engine, was asked to stop"


def test_a_sequence_that_will_not_stop_is_reported():
    rig, ui = _estop(sequence=_Sequence(active=True, stops=False))
    assert _said(ui, "auto-sequence worker did not stop")
