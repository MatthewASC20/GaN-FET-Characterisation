"""Thread-safe SCPI conversation management over a transport."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

from gan_fet.core.command_logger import classify_scpi_risk
from gan_fet.core.events import InstrumentCommandEvent, bus
from gan_fet.transport.base import (
    BinaryReadUnsupported,
    Transport,
    TransportError,
)

log = logging.getLogger(__name__)


class ScpiClient:
    """Add connection recovery, serialization, and events to a transport."""

    def __init__(self, name: str, transport: Transport):
        self.name = name
        self.transport = transport
        self._lock = threading.RLock()

    @property
    def is_simulated(self) -> bool:
        return self.transport.is_simulated

    @property
    def is_visa(self) -> bool:
        """True only when VISA owns addressing for this client."""
        return self.transport.is_visa

    @property
    def handles_prologix_framing(self) -> bool:
        return self.transport.handles_prologix_framing

    @property
    def uses_prologix(self) -> bool:
        return self.transport.handles_prologix_framing

    @property
    def connected(self) -> bool:
        with self._lock:
            return self.transport.is_open

    def __repr__(self) -> str:
        return f"<ScpiClient {self.name} via {self.transport.description}>"

    def connect(self) -> bool:
        with self._lock:
            if self.transport.is_open:
                return True
            try:
                self.transport.open()
            except TransportError as exc:
                log.warning("%s: %s", self.name, exc)
                return False
            except Exception as exc:
                log.warning(
                    "%s: unexpected error opening transport: %s", self.name, exc
                )
                self.transport.close()
                return False
            return True

    def close(self) -> None:
        with self._lock:
            self.transport.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Serialize a whole multi-command instrument conversation.

        Individual methods already use the same re-entrant lock, so callers
        may safely compose ``write``, ``query``, and ``query_bytes`` inside
        this context without another thread interleaving a command.
        """
        with self._lock:
            yield

    def write(self, command: str) -> bool:
        start_time = time.time()
        with self._lock:
            if not self.connect():
                return False
            try:
                self.transport.write_line(command)
            except Exception as exc:
                log.warning("%s: write '%s' failed: %s", self.name, command, exc)
                self.close()
                return False
            self._publish(command, None, False, start_time)
            return True

    def query(self, command: str) -> Optional[str]:
        start_time = time.time()
        with self._lock:
            if not self.connect():
                return None
            try:
                response = self.transport.query(command)
            except Exception as exc:
                log.warning("%s: query '%s' failed: %s", self.name, command, exc)
                self.close()
                return None
            self._publish(command, response, True, start_time)
            return response

    def query_float(self, command: str) -> Optional[float]:
        raw = self.query(command)
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            log.warning(
                "%s: non-numeric response to '%s': %r", self.name, command, raw
            )
            return None

    def query_bytes(
        self,
        command: str,
        *,
        max_bytes: int,
        timeout_s: float,
    ) -> bytes:
        """Return one bounded binary response on a capable transport.

        Unsupported byte streams fail before the command is sent.  Any I/O or
        size failure closes the transport so a partial response cannot poison
        the next line-oriented query.
        """
        start_time = time.time()
        with self._lock:
            if not self.transport.supports_binary_reads:
                raise BinaryReadUnsupported(
                    f"{self.transport.description} does not support bounded "
                    "binary messages"
                )
            if not self.connect():
                raise TransportError(
                    f"{self.name}: could not connect for binary query"
                )
            try:
                self.transport.write_line(command)
                response = self.transport.read_binary(
                    max_bytes=max_bytes,
                    timeout_s=timeout_s,
                )
            except BinaryReadUnsupported:
                self.transport.close()
                raise
            except Exception as exc:
                log.warning(
                    "%s: binary query '%s' failed: %s",
                    self.name,
                    command,
                    exc,
                )
                self.transport.close()
                if isinstance(exc, TransportError):
                    raise
                raise TransportError(
                    f"{self.name}: binary query {command!r} failed: {exc}"
                ) from exc
            self._publish(
                command,
                f"<binary {len(response)} bytes>",
                True,
                start_time,
            )
            return response

    def request_local_control(self) -> bool:
        """Return control to the front panel through the active protocol."""
        start_time = time.time()
        with self._lock:
            if not self.connect():
                return False
            try:
                command = self.transport.request_local_control()
            except Exception as exc:
                log.warning("%s: request for local control failed: %s", self.name, exc)
                self.close()
                return False
            self._publish(command, None, False, start_time)
            return True

    def _publish(
        self,
        command: str,
        response: Optional[str],
        is_query: bool,
        start_time: float,
    ) -> None:
        bus.publish(
            InstrumentCommandEvent(
                timestamp=start_time,
                instrument_name=self.name,
                command=command,
                response=response,
                is_query=is_query,
                risk_level=classify_scpi_risk(command),
                duration_ms=(time.time() - start_time) * 1000.0,
                is_simulated=self.is_simulated,
            )
        )
