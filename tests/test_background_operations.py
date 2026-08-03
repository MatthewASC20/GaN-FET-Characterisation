"""Running a rig operation off the Tk thread and reporting how it ended.

The completion callback is what releases the operation token. A worker that
failed to post one would leave the coordinator believing an operation was still
running, and the rig would refuse every later command with "Rig Busy" until the
application was restarted. So the substance here is not what the work does — it
is that completion is reported no matter how the work ends.
"""

from __future__ import annotations

import threading

import pytest

from gan_fet.ui.operations.background import run_in_background
from gan_fet.ui.operations.worker_pool import WorkerPool


class _Dispatcher:
    """Stands in for UiDispatcher, recording instead of marshalling."""

    def __init__(self) -> None:
        self.posted: list[tuple] = []

    def post(self, func, *args, **kwargs):
        self.posted.append((func, args, kwargs))
        func(*args, **kwargs)
        return None


def _run(work, on_error=None, name="apply-wavegen"):
    pool, dispatcher = WorkerPool(), _Dispatcher()
    reported: list[BaseException | None] = []
    run_in_background(
        pool=pool,
        dispatcher=dispatcher,
        work=work,
        on_done=reported.append,
        name=name,
        on_error=on_error,
    ).join(2.0)
    return reported, dispatcher


def test_success_reports_no_error():
    ran = threading.Event()
    reported, _dispatcher = _run(ran.set)
    assert ran.is_set()
    assert reported == [None]


def test_a_failure_is_reported_rather_than_lost():
    failure = RuntimeError("wavegen did not acknowledge")

    def work() -> None:
        raise failure

    reported, _dispatcher = _run(work)
    assert reported == [failure]


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_even_a_base_exception_releases_the_operation(interrupt):
    """KeyboardInterrupt or SystemExit inside a worker is still an operation
    that ended. Catching only Exception would leave the token held and every
    later command refused as "Rig Busy" until a restart."""

    def work() -> None:
        raise interrupt()

    reported, _dispatcher = _run(work)
    assert len(reported) == 1
    assert isinstance(reported[0], interrupt)


def test_completion_goes_through_the_dispatcher_not_the_worker_thread():
    """Everything the completion callback touches is Tk state."""
    reported, dispatcher = _run(lambda: None)
    assert len(dispatcher.posted) == 1
    assert reported == [None]


def test_the_work_runs_off_the_calling_thread():
    where: dict[str, int] = {}
    _run(lambda: where.update(worker=threading.get_ident()))
    assert where["worker"] != threading.get_ident()


# -- recovery ----------------------------------------------------------------


def test_recovery_runs_on_failure_and_can_describe_the_outcome():
    """Bus Off uses this to take the emergency path while the rig is still in
    whatever state the failure left it."""
    attempted = []

    def work() -> None:
        raise RuntimeError("output-off was not acknowledged")

    def recover(error):
        attempted.append(error)
        return RuntimeError(f"{error}; emergency output-off was unconfirmed")

    reported, _dispatcher = _run(work, on_error=recover)
    assert len(attempted) == 1
    assert "emergency output-off was unconfirmed" in str(reported[0])


def test_recovery_returning_nothing_keeps_the_original_failure():
    """The emergency path succeeding does not make the failure go away — the
    operator still needs to know Bus Off took it."""
    failure = RuntimeError("output readback did not confirm the rig is safe")

    def work() -> None:
        raise failure

    reported, _dispatcher = _run(work, on_error=lambda _error: None)
    assert reported == [failure]


def test_recovery_does_not_run_when_the_work_succeeds():
    called = []
    reported, _dispatcher = _run(lambda: None, on_error=called.append)
    assert called == []
    assert reported == [None]


def test_a_failing_recovery_still_reports_the_original_failure():
    """Otherwise a broken transport during emergency shutdown would swallow
    the failure that triggered it, and the token with it."""
    failure = RuntimeError("SMU output-off was not acknowledged")

    def work() -> None:
        raise failure

    def recover(_error):
        raise OSError("transport is gone")

    reported, _dispatcher = _run(work, on_error=recover)
    assert reported == [failure]
