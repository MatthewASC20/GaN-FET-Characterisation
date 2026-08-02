"""The threads behind rig operations.

Every operator action that talks to the bench runs on one of these. What they
must get right is not what they do but how they end: tracked so shutdown can
wait for them, non-daemon so a conversation with the SMU or wavegen is never
killed at interpreter exit with the output still on, and forgotten even when
they fail.
"""

from __future__ import annotations

import threading
import time

import pytest

from gan_fet.ui.operations.worker_pool import WorkerPool


def test_the_target_runs_and_the_worker_is_forgotten():
    pool = WorkerPool()
    ran = threading.Event()
    pool.start(ran.set, name="apply-wavegen").join(2.0)
    assert ran.is_set()
    assert pool.active_names() == []


def test_workers_are_not_daemons():
    """A daemon thread part-way through an SMU or wavegen conversation would be
    killed at interpreter exit with the output still on."""
    started = threading.Event()
    release = threading.Event()
    pool = WorkerPool()

    def block() -> None:
        started.set()
        release.wait(2.0)

    thread = pool.start(block, name="bus-off")
    started.wait(2.0)
    try:
        assert not thread.daemon
    finally:
        release.set()
        thread.join(2.0)


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_worker_that_raises_is_still_forgotten():
    """Otherwise one failed operation makes every later shutdown wait out its
    full timeout for a thread that is already dead."""
    pool = WorkerPool()

    def explode() -> None:
        raise RuntimeError("transport died mid-command")

    pool.start(explode, name="zvs").join(2.0)
    assert pool.active_names() == []
    assert pool.join_all(timeout=0.1)


def test_join_all_waits_for_running_workers():
    pool = WorkerPool()
    release = threading.Event()
    pool.start(lambda: release.wait(2.0), name="autotune")
    assert not pool.join_all(timeout=0.05), "should not claim a live worker finished"
    release.set()
    assert pool.join_all(timeout=2.0)
    assert pool.active_names() == []


def test_join_all_returns_true_when_there_is_nothing_to_wait_for():
    assert WorkerPool().join_all(timeout=0.0)


def test_join_all_honours_its_timeout_rather_than_the_worker_duration():
    """Shutdown has a deadline. A worker stuck on an unresponsive instrument
    must not extend it."""
    pool = WorkerPool()
    release = threading.Event()
    pool.start(lambda: release.wait(5.0), name="stuck-on-gpib")
    started = time.monotonic()
    try:
        assert not pool.join_all(timeout=0.3)
        assert time.monotonic() - started < 1.5
    finally:
        release.set()


def test_a_worker_joining_does_not_wait_for_itself():
    """Shutdown itself runs on a worker. Including the caller would mean
    waiting for the thread doing the waiting — a guaranteed timeout, and on a
    close path that means a full-length hang every time."""
    pool = WorkerPool()
    result: dict[str, object] = {}

    def shutdown() -> None:
        started = time.monotonic()
        result["joined"] = pool.join_all(timeout=2.0)
        result["elapsed"] = time.monotonic() - started

    pool.start(shutdown, name="app-shutdown").join(3.0)
    assert result["joined"] is True
    assert isinstance(result["elapsed"], float) and result["elapsed"] < 1.0


def test_several_workers_are_tracked_together():
    pool = WorkerPool()
    release = threading.Event()
    for name in ("apply-wavegen", "autotune", "zvs"):
        pool.start(lambda: release.wait(2.0), name=name)
    assert pool.active_names() == ["apply-wavegen", "autotune", "zvs"]
    release.set()
    assert pool.join_all(timeout=2.0)
    assert pool.active_names() == []
