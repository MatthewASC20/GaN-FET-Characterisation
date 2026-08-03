"""Command Risk Classifier, Pseudo-Command Loop Contexts, and SCPI Logger.

Provides real-time SCPI risk level analysis, loop collapsing/pseudo-command
generation, and asynchronous SCPI disk logging.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Generator, Optional

from gan_fet.core.events import InstrumentCommandEvent, PseudoCommandEvent, bus

log = logging.getLogger(__name__)

# Patterns for High and Medium Risk SCPI classification
HIGH_RISK_PATTERNS = (
    re.compile(r"\bOUTP(?:UT)?(?::STAT(?:E)?)?\s+(?:ON|1)\b", re.IGNORECASE),
    re.compile(r"\bSOUR(?:CE)?:VOLT(?:AGE)?(?::LEV(?:EL)?)?(?::IMM(?:EDIATE)?)?\s+([0-9.]+)", re.IGNORECASE),
    re.compile(r"\bVOLT(?:AGE)?\s+([0-9.]+)", re.IGNORECASE),
    re.compile(r"\bABOR(?:T)?\b", re.IGNORECASE),
    re.compile(r"\bEMERGENCY\b", re.IGNORECASE),
)

HIGH_RISK_VOLTAGE_THRESHOLD_V = 50.0

MEDIUM_RISK_PATTERNS = (
    re.compile(r"\bOUTP(?:UT)?(?::STAT(?:E)?)?\s+(?:OFF|0)\b", re.IGNORECASE),
    re.compile(r"\bSOUR(?:CE)?:VOLT(?:AGE)?", re.IGNORECASE),
    re.compile(r"\bSOUR(?:CE)?:CURR(?:ENT)?", re.IGNORECASE),
    re.compile(r"\bVOLT(?:AGE)?", re.IGNORECASE),
    re.compile(r"\bFREQ(?:UENCY)?", re.IGNORECASE),
    re.compile(r"\bDUTY\b", re.IGNORECASE),
    re.compile(r"\bC[12]:BSWV\b", re.IGNORECASE),
    re.compile(r"\bCONF(?:IGURE)?\b", re.IGNORECASE),
)


def classify_scpi_risk(command: str) -> str:
    """Classify an SCPI command into 'HIGH', 'MEDIUM', or 'LOW' risk level."""
    clean_cmd = command.strip()

    # Read-only queries are always LOW risk
    if "?" in clean_cmd:
        return "LOW"

    # Check High Risk output enable
    if re.search(r"\bOUTP(?:UT)?(?::STAT(?:E)?)?\s+(?:ON|1)\b", clean_cmd, re.IGNORECASE):
        return "HIGH"

    # Check High Risk voltage setpoints > THRESHOLD
    for pattern in (
        re.compile(r"\bSOUR(?:CE)?:VOLT(?:AGE)?(?::LEV(?:EL)?)?(?::IMM(?:EDIATE)?)?\s+([0-9.]+)", re.IGNORECASE),
        re.compile(r"\bVOLT(?:AGE)?\s+([0-9.]+)", re.IGNORECASE),
    ):
        match = pattern.search(clean_cmd)
        if match:
            try:
                val = float(match.group(1))
                if val >= HIGH_RISK_VOLTAGE_THRESHOLD_V:
                    return "HIGH"
            except ValueError:
                pass

    # Check generic High Risk keywords
    if (
        re.search(r"\bABOR(?:T)?\b", clean_cmd, re.IGNORECASE)
        or "EMERGENCY" in clean_cmd.upper()
    ):
        return "HIGH"

    # Check Medium Risk setting commands
    for pattern in MEDIUM_RISK_PATTERNS:
        if pattern.search(clean_cmd):
            return "MEDIUM"

    return "LOW"


@contextmanager
def loop_context(
    category: str,
    title: str,
    total_iterations: int = 0,
    is_simulated: bool = False,
) -> Generator[Callable[[int, str], None], None, None]:
    """Context manager for emitting high-level pseudo-command loop events.

    Yields a callback `update_loop(iteration, detail)` for updating the loop row in-place.
    """
    loop_id = f"{category}_{title}_{time.time():.3f}"
    start_time = time.time()

    # Initial start event
    bus.publish(
        PseudoCommandEvent(
            timestamp=start_time,
            category=category,
            title=f"Started {title}",
            detail="Initializing loop...",
            loop_id=loop_id,
            iteration=0,
            total_iterations=total_iterations,
            is_simulated=is_simulated,
        )
    )

    def update_loop(iteration: int, detail: str) -> None:
        bus.publish(
            PseudoCommandEvent(
                timestamp=time.time(),
                category=category,
                title=title,
                detail=detail,
                loop_id=loop_id,
                iteration=iteration,
                total_iterations=total_iterations,
                is_simulated=is_simulated,
            )
        )

    try:
        yield update_loop
    finally:
        elapsed = time.time() - start_time
        bus.publish(
            PseudoCommandEvent(
                timestamp=time.time(),
                category=category,
                title=f"Completed {title}",
                detail=f"Finished in {elapsed:.2f}s",
                loop_id=loop_id,
                iteration=total_iterations,
                total_iterations=total_iterations,
                is_simulated=is_simulated,
            )
        )


class ScpiFileLogger:
    """Thread-safe background logger for writing raw SCPI commands to disk."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self._queue: queue.Queue[Optional[str]] = queue.Queue()
        self._lifecycle_lock = threading.Lock()
        self._closed = False
        self._unsubscribe: Optional[Callable[[], None]] = bus.subscribe(
            InstrumentCommandEvent, self._on_command_event
        )
        self._worker = threading.Thread(
            target=self._drain,
            daemon=True,
            name="scpi-file-logger",
        )
        self._worker.start()

    def _on_command_event(self, event: InstrumentCommandEvent) -> None:
        try:
            timestamp_str = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(event.timestamp)
            )
            ms = int((event.timestamp % 1) * 1000)
            mode_tag = "[SIM]" if event.is_simulated else "[RAW]"
            resp_tag = f" -> {event.response}" if event.response is not None else ""
            line = (
                f"{timestamp_str}.{ms:03d} {mode_tag} [{event.risk_level}] "
                f"[{event.instrument_name}] {event.command}{resp_tag} ({event.duration_ms:.1f}ms)\n"
            )
            # The event bus publisher may be a time-sensitive hardware loop.
            # Queueing an immutable string keeps filesystem latency off it.
            with self._lifecycle_lock:
                if not self._closed:
                    self._queue.put_nowait(line)
        except Exception as exc:
            log.warning("Failed queueing SCPI log line for %s: %s", self.log_path, exc)

    def _drain(self) -> None:
        while True:
            line = self._queue.get()
            try:
                if line is None:
                    return
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.log_path, "a", encoding="utf-8") as handle:
                    handle.write(line)
            except Exception as exc:
                log.warning(
                    "Failed writing to SCPI log file %s: %s",
                    self.log_path,
                    exc,
                )
            finally:
                self._queue.task_done()

    def close(
        self,
        flush: bool = True,
        timeout: Optional[float] = None,
    ) -> bool:
        """Stop the writer and optionally flush queued lines.

        Returns ``True`` when the worker terminated within ``timeout``.
        """
        with self._lifecycle_lock:
            if not self._closed:
                self._closed = True
                unsubscribe = self._unsubscribe
                self._unsubscribe = None
                if unsubscribe is not None:
                    unsubscribe()
                if not flush:
                    while True:
                        try:
                            self._queue.get_nowait()
                        except queue.Empty:
                            break
                        else:
                            self._queue.task_done()
                self._queue.put_nowait(None)
        self._worker.join(timeout)
        return not self._worker.is_alive()
