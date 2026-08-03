"""Claiming the rig, releasing it, and making it safe.

`RigOperations` holds the coordinator, the worker pool and the dispatcher, so
the threading contract is visible in one place. What the window supplies —
dialogs, the status line, control refresh — is injected, which is what lets all
of this be exercised without a display.
"""

from __future__ import annotations

from typing import Optional

from gan_fet.ui.operations.rig_control import RigOperations, reset_confirmation
from gan_fet.ui.operations.worker_pool import WorkerPool
from gan_fet.ui.widgets import OperationCoordinator

LABELS = {
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
    def __init__(self, *, really_disarms: bool = True) -> None:
        self.outputs_armed = True
        self.really_disarms = really_disarms

    def disarm_outputs(self) -> None:
        if self.really_disarms:
            self.outputs_armed = False


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


def _build(ui=None, smu=None, wavegen=None, safety=None):
    ui = ui or _Ui()
    rig = RigOperations(
        ui=ui,
        operations=OperationCoordinator(),
        pool=WorkerPool(),
        dispatcher=_Dispatcher(),
        safety=safety or _Safety(),
        smu=smu or _Smu(),
        wavegen_controller=wavegen or _Wavegen(),
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
