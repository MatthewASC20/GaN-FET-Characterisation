"""Reclaiming an abandoned lease — simulation only.

The lease is fail-closed by design: a stale-looking lock on live bench data is
never removed automatically, because network clock skew makes "stale" an
unreliable judgement and the records are irreplaceable. Simulation opts out of
that, because its data is disposable practice output and a lease left by a
crash otherwise blocks the mode that exists to be a safe sandbox.

The asymmetry is the thing under test. A change that let live data reclaim a
lease would pass every other test in the suite.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from gan_fet.storage.network_lock import (
    LOCK_STALE_THRESHOLD_S,
    ConcurrentAccessError,
    NetworkProjectLock,
)


def _write_lease(
    lock_file, *, pid: int, host: str, age_s: float, token: str = "other-token"
) -> None:
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text(
        json.dumps(
            {
                "pid": pid,
                "host": host,
                "token": token,
                "editing_since": "2026-01-01T00:00:00+00:00",
                "last_heartbeat": "2026-01-01T00:00:00+00:00",
                "heartbeat_timestamp": time.time() - age_s,
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def project(tmp_path):
    target = tmp_path / "simulation"
    target.mkdir()
    return target, tmp_path / "simulation.lock"


def _dead_pid() -> int:
    """A PID that is almost certainly not running, on this host."""
    return 999_999


# -- live data refuses -------------------------------------------------------


def test_live_refuses_a_stale_lease(project):
    target, lock_file = project
    _write_lease(
        lock_file,
        pid=_dead_pid(),
        host="some-other-host",
        age_s=LOCK_STALE_THRESHOLD_S * 3,
    )
    lock = NetworkProjectLock(target)
    with pytest.raises(ConcurrentAccessError, match="stale"):
        lock.acquire()
    assert lock_file.exists(), "live data must never remove a lease itself"


def test_live_refuses_a_dead_local_owner(project):
    target, lock_file = project
    import socket

    _write_lease(lock_file, pid=_dead_pid(), host=socket.gethostname(), age_s=1.0)
    lock = NetworkProjectLock(target)
    with pytest.raises(ConcurrentAccessError, match="no longer running"):
        lock.acquire()
    assert lock_file.exists()


# -- simulation reclaims -----------------------------------------------------


def test_simulation_reclaims_a_stale_lease(project):
    target, lock_file = project
    _write_lease(
        lock_file,
        pid=_dead_pid(),
        host="some-other-host",
        age_s=LOCK_STALE_THRESHOLD_S * 3,
    )
    lock = NetworkProjectLock(target, allow_stale_takeover=True)
    assert lock.acquire() is True
    try:
        assert lock.ownership_token is not None
        held = json.loads(lock_file.read_text(encoding="utf-8"))
        assert held["token"] == lock.ownership_token
        assert held["pid"] == os.getpid()
    finally:
        lock.release()


def test_simulation_reclaims_a_dead_local_owner(project):
    target, lock_file = project
    import socket

    _write_lease(lock_file, pid=_dead_pid(), host=socket.gethostname(), age_s=1.0)
    lock = NetworkProjectLock(target, allow_stale_takeover=True)
    assert lock.acquire() is True
    lock.release()


def test_simulation_still_refuses_a_live_owner(project):
    """Takeover is for abandoned leases, not for stealing one in use."""
    target, lock_file = project
    _write_lease(lock_file, pid=os.getpid(), host="a-remote-host", age_s=1.0)
    lock = NetworkProjectLock(target, allow_stale_takeover=True)
    with pytest.raises(ConcurrentAccessError):
        lock.acquire()
    assert lock_file.exists(), "a live lease must survive"


def test_takeover_leaves_a_relocked_path_alone(project, monkeypatch):
    """Between judging a lease abandoned and removing it, someone else may
    have acquired the path. Removing *their* live lease would be worse than
    refusing to start, so a changed token aborts the removal."""
    target, lock_file = project
    _write_lease(
        lock_file,
        pid=_dead_pid(),
        host="some-other-host",
        age_s=LOCK_STALE_THRESHOLD_S * 3,
        token="original",
    )
    lock = NetworkProjectLock(target, allow_stale_takeover=True)

    real_read = lock._read_lock_info

    def replaced_between_reads(*args, **kwargs):
        info = real_read(*args, **kwargs)
        # Simulate a new owner arriving before the unlink.
        _write_lease(
            lock_file,
            pid=os.getpid(),
            host="a-remote-host",
            age_s=0.0,
            token="someone-else",
        )
        return info

    monkeypatch.setattr(lock, "_read_lock_info", replaced_between_reads)
    with pytest.raises(ConcurrentAccessError):
        lock.acquire()
    surviving = json.loads(lock_file.read_text(encoding="utf-8"))
    assert surviving["token"] == "someone-else"


def test_repeated_retakes_eventually_give_up(project, monkeypatch):
    """A path being re-locked continuously must not spin forever."""
    target, lock_file = project
    lock = NetworkProjectLock(target, allow_stale_takeover=True)

    def always_stale(*_args, **_kwargs):
        _write_lease(
            lock_file,
            pid=_dead_pid(),
            host="some-other-host",
            age_s=LOCK_STALE_THRESHOLD_S * 3,
        )

    always_stale()
    monkeypatch.setattr(
        NetworkProjectLock, "_take_over", lambda self, info, reason: always_stale()
    )
    with pytest.raises(ConcurrentAccessError, match="re-taken"):
        lock.acquire()


def test_takeover_is_off_by_default(project):
    target, _lock_file = project
    assert NetworkProjectLock(target).allow_stale_takeover is False
