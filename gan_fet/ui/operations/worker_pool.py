"""Tracked background workers for UI-initiated rig operations.

Every operation the operator starts — applying the wavegen, autotune, the ZVS
search, bus off, emergency stop — runs off the Tk thread. This owns the part
that must be right about all of them: the threads are tracked so shutdown can
wait for them, and they are **not** daemons, because a daemon thread mid-way
through an SMU or wavegen conversation would be killed at interpreter exit with
the output still on.

No tkinter here. Worker callbacks marshal back through ``UiDispatcher``; this
class never touches Tcl and neither do the workers it starts.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

#: Longest single wait inside :meth:`WorkerPool.join_all`, so the overall
#: timeout is honoured to roughly this resolution rather than overshooting by
#: however long one worker happens to take.
_JOIN_SLICE_S = 0.2


class WorkerPool:
    """Starts and tracks the non-daemon threads behind rig operations."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._workers: set[threading.Thread] = set()

    def start(self, target: Callable[[], None], *, name: str) -> threading.Thread:
        """Run ``target`` on a tracked worker thread.

        The thread removes itself in a ``finally``, so a worker that raises is
        still forgotten — otherwise one failed operation would make every
        subsequent shutdown wait out its full timeout.
        """

        def run() -> None:
            try:
                target()
            finally:
                with self._lock:
                    self._workers.discard(threading.current_thread())

        thread = threading.Thread(target=run, daemon=False, name=name)
        with self._lock:
            self._workers.add(thread)
        thread.start()
        return thread

    def join_all(self, timeout: float) -> bool:
        """Wait for every other worker, returning whether they all finished.

        The calling thread is excluded. Shutdown itself runs on a worker, so
        including it would mean waiting for the thread doing the waiting.
        """
        deadline = time.monotonic() + timeout
        current = threading.current_thread()
        while True:
            with self._lock:
                workers = [
                    worker
                    for worker in self._workers
                    if worker is not current and worker.is_alive()
                ]
            if not workers:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.warning(
                    "Timed out waiting for UI workers: %s",
                    ", ".join(worker.name for worker in workers),
                )
                return False
            workers[0].join(min(_JOIN_SLICE_S, remaining))

    def active_names(self) -> list[str]:
        """Names of the workers still running, for diagnostics."""
        with self._lock:
            return sorted(
                worker.name for worker in self._workers if worker.is_alive()
            )
