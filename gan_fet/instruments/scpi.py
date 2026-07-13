"""SCPI-over-TCP transport shared by all instruments.

One persistent socket per instrument, guarded by a lock so the UI thread,
engine thread and sync thread never interleave commands. Failures close the
socket (forcing a reconnect on the next call), log, and return None/False —
matching the fault tolerance of the original per-command implementation.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

# After a failed connect, don't re-attempt for this long. Without it, every
# command sent to an unreachable instrument blocks for a full connect
# timeout — which freezes whatever thread is doing the sending.
RETRY_COOLDOWN_S = 10.0


class ScpiTcpClient:
    def __init__(self, name: str, host: str, port: int, timeout: float = 5.0):
        self.name = name
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._lock = threading.RLock()
        self._next_attempt = 0.0

    # -- connection ----------------------------------------------------

    def connect(self) -> bool:
        with self._lock:
            if self._sock is not None:
                return True
            if time.monotonic() < self._next_attempt:
                return False  # still in cooldown from the last failure
            try:
                self._sock = socket.create_connection(
                    (self.host, self.port), timeout=self.timeout
                )
                self._sock.settimeout(self.timeout)
                self._next_attempt = 0.0
                return True
            except OSError as exc:
                log.warning("%s: connect to %s:%s failed: %s (retry in %.0fs)",
                            self.name, self.host, self.port, exc, RETRY_COOLDOWN_S)
                self._sock = None
                self._next_attempt = time.monotonic() + RETRY_COOLDOWN_S
                return False

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._sock is not None

    # -- I/O -------------------------------------------------------------

    def write(self, command: str) -> bool:
        with self._lock:
            if not self.connect():
                return False
            try:
                self._sock.sendall((command + "\n").encode())
                return True
            except OSError as exc:
                log.warning("%s: write '%s' failed: %s", self.name, command, exc)
                self.close()
                return False

    def query(self, command: str) -> Optional[str]:
        with self._lock:
            if not self.write(command):
                return None
            try:
                chunks = []
                while True:
                    chunk = self._sock.recv(4096)
                    if not chunk:
                        raise ConnectionError("connection closed by instrument")
                    chunks.append(chunk)
                    if chunk.endswith(b"\n"):
                        break
                return b"".join(chunks).decode(errors="replace").strip()
            except (OSError, ConnectionError) as exc:
                log.warning("%s: query '%s' failed: %s", self.name, command, exc)
                self.close()
                return None

    def query_float(self, command: str) -> Optional[float]:
        raw = self.query(command)
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            log.warning("%s: non-numeric response to '%s': %r", self.name, command, raw)
            return None
