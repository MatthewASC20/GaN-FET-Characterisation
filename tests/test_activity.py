"""Rig-activity guard and control-loop cancellation.

Regressions for the concurrency defects: two threads could drive the SMU at
once (a ZVS search ramping up against a "Bus Off" ramping down), and a manual
ZVS search could not be stopped short of EMERGENCY STOP.
"""

import threading
import time

import pytest

from gan_fet.core.activity import RigActivity, RigBusy, RigOwner
from gan_fet.core.voltage_control import PeakControlError, PeakVoltageController
from gan_fet.core.zvs import ZvsTuner


def test_only_one_owner_at_a_time():
    activity = RigActivity()
    assert activity.try_acquire(RigOwner.MANUAL)
    assert not activity.try_acquire(RigOwner.EXPERIMENT)
    assert activity.owner is RigOwner.MANUAL
    assert activity.busy

    activity.release(RigOwner.MANUAL)
    assert not activity.busy
    assert activity.try_acquire(RigOwner.EXPERIMENT)


def test_release_by_a_different_owner_is_ignored():
    """An experiment finishing inside a sequence must not free the sequence's
    claim — that is exactly how a stale release would let a manual action in
    mid-sequence."""
    activity = RigActivity()
    activity.try_acquire(RigOwner.SEQUENCE)
    activity.release(RigOwner.EXPERIMENT)
    assert activity.owner is RigOwner.SEQUENCE


def test_hold_raises_when_busy_and_always_releases():
    activity = RigActivity()
    with activity.hold(RigOwner.MANUAL):
        with pytest.raises(RigBusy):
            with activity.hold(RigOwner.EXPERIMENT):
                pass
    assert not activity.busy

    with pytest.raises(ValueError):
        with activity.hold(RigOwner.MANUAL):
            raise ValueError("boom")
    assert not activity.busy, "hold must release even when the body raises"


def test_cancel_flag_clears_on_acquire_and_release():
    activity = RigActivity()
    activity.try_acquire(RigOwner.MANUAL)
    activity.request_cancel()
    assert activity.cancelled and activity.cancel_check()()
    activity.release(RigOwner.MANUAL)
    assert not activity.cancelled

    activity.try_acquire(RigOwner.EXPERIMENT)
    assert not activity.cancelled, "a new owner must not inherit a stale cancel"


def test_guard_is_thread_safe():
    activity = RigActivity()
    winners = []

    def contend():
        if activity.try_acquire(RigOwner.MANUAL):
            winners.append(threading.current_thread().name)
            time.sleep(0.01)

    threads = [threading.Thread(target=contend) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(winners) == 1


def test_zvs_search_stops_when_cancelled(rig):
    """The manual ZVS path must be interruptible: with the flag set the search
    returns promptly instead of running to max_steps."""
    rig.smu.initialize()
    rig.smu.output_on()
    rig.smu.ramp_to(200.0)  # far from the plant minimum, so a long descent

    activity = RigActivity()
    activity.try_acquire(RigOwner.MANUAL)
    tuner = ZvsTuner(rig.smu, rig.settings.zvs, rig.safety)

    activity.request_cancel()
    start = time.monotonic()
    result = tuner.find_minimum(cancel_check=activity.cancel_check())
    elapsed = time.monotonic() - start

    assert elapsed < 5.0, "cancelled ZVS search kept running"
    # It stops where it was rather than walking all the way down.
    assert result is None or abs(result.v_zvs - 200.0) < 5.0


def test_peak_control_stops_when_cancelled(rig):
    rig.smu.initialize()
    rig.smu.output_on()
    activity = RigActivity()
    activity.try_acquire(RigOwner.MANUAL)
    controller = PeakVoltageController(
        rig.smu, rig.scope, rig.settings.peak_control, rig.safety
    )

    activity.request_cancel()
    with pytest.raises(PeakControlError):
        controller.achieve_peak(400.0, cancel_check=activity.cancel_check())
