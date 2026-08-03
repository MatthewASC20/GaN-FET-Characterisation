"""The project lease guards live bench data, and only live bench data.

It is deliberately fail-closed: a stale-looking lock is never cleared
automatically, because network clock skew makes that judgement unreliable and
the records it protects cannot be recreated. That is the right trade for a
shared data directory.

Simulation does not take it at all. Its output is disposable practice data in a
private directory, so a lease has nothing to exclude — while a file left by a
crashed run blocks the next start of the mode that exists to be a safe sandbox.

The asymmetry is what these tests hold in place: a change that made live data
skip the lease, or made simulation take one, would pass every other test here.
"""

from __future__ import annotations

import json
import os
import socket
import time

import pytest

from gan_fet.storage.db import Database
from gan_fet.storage.network_lock import LOCK_STALE_THRESHOLD_S, ConcurrentAccessError

DEAD_PID = 999_999


def _write_lease(lock_file, *, pid: int, host: str, age_s: float) -> None:
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text(
        json.dumps(
            {
                "pid": pid,
                "host": host,
                "token": "someone-elses-token",
                "editing_since": "2026-01-01T00:00:00+00:00",
                "last_heartbeat": "2026-01-01T00:00:00+00:00",
                "heartbeat_timestamp": time.time() - age_s,
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def project(tmp_path):
    target = tmp_path / "data"
    target.mkdir()
    return target, tmp_path / "data.lock"


# -- live data takes the lease and never clears one --------------------------


def test_live_data_takes_the_lease(project):
    target, lock_file = project
    db = Database(target / "gan_fet.db")
    try:
        assert lock_file.exists()
    finally:
        db.close()
    assert not lock_file.exists(), "a clean close releases the lease"


def test_live_refuses_a_stale_lease_rather_than_clearing_it(project):
    target, lock_file = project
    _write_lease(
        lock_file, pid=DEAD_PID, host="another-host", age_s=LOCK_STALE_THRESHOLD_S * 3
    )
    with pytest.raises(ConcurrentAccessError, match="stale"):
        Database(target / "gan_fet.db")
    assert lock_file.exists(), "clearing it automatically is what clock skew forbids"


def test_live_refuses_a_dead_local_owner_rather_than_clearing_it(project):
    target, lock_file = project
    _write_lease(lock_file, pid=DEAD_PID, host=socket.gethostname(), age_s=1.0)
    with pytest.raises(ConcurrentAccessError, match="no longer running"):
        Database(target / "gan_fet.db")
    assert lock_file.exists()


def test_live_refuses_a_lease_held_elsewhere(project):
    """A live owner on another workstation is a plain conflict."""
    target, lock_file = project
    _write_lease(lock_file, pid=os.getpid(), host="another-host", age_s=1.0)
    with pytest.raises(ConcurrentAccessError):
        Database(target / "gan_fet.db")
    assert lock_file.exists()


def test_two_databases_in_one_process_share_the_lease(project):
    """Reference-counted on purpose: the app opens the store more than once,
    and a second open in the same process is not a competing workstation."""
    target, lock_file = project
    first = Database(target / "gan_fet.db")
    second = Database(target / "gan_fet.db")
    try:
        assert lock_file.exists()
    finally:
        second.close()
        # Still held: the lease is released only when the last user closes.
        assert lock_file.exists()
        first.close()
    assert not lock_file.exists()


# -- simulation takes no lease at all ----------------------------------------


def test_simulation_creates_no_lock_file(project):
    target, lock_file = project
    db = Database(target / "sim.db", use_project_lock=False)
    try:
        assert not lock_file.exists()
    finally:
        db.close()
    assert not lock_file.exists()


def test_simulation_starts_despite_a_leftover_lock_file(project):
    """The behaviour this change exists for: a crashed practice run must not
    block the next one."""
    target, lock_file = project
    _write_lease(
        lock_file, pid=DEAD_PID, host="another-host", age_s=LOCK_STALE_THRESHOLD_S * 3
    )
    db = Database(target / "sim.db", use_project_lock=False)
    try:
        assert db.list_devices() == []
    finally:
        db.close()


def test_two_simulation_databases_can_share_a_directory(project):
    """Without a lease, SQLite's own locking is what serialises writers."""
    target, _lock_file = project
    first = Database(target / "sim.db", use_project_lock=False)
    second = Database(target / "sim.db", use_project_lock=False)
    try:
        first.get_or_create_device("A")
        assert "A" in second.list_devices()
    finally:
        second.close()
        first.close()


def test_the_lease_is_taken_unless_explicitly_declined(project):
    """Live is the default, so a new call site cannot lose the lease silently."""
    target, lock_file = project
    db = Database(target / "gan_fet.db")
    try:
        assert lock_file.exists()
    finally:
        db.close()
