"""TCP socket transport for line-oriented bench instruments."""

from __future__ import annotations

import socket
from typing import Optional

from gan_fet.transport.base import Transport, TransportError


class TcpTransport(Transport):
    def __init__(self, host: str, port: int, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._read_buffer = b""

    @property
    def description(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    def open(self) -> None:
        if self._sock is not None:
            return
        try:
            sock = socket.create_connection(
                (self.host, self.port), timeout=self.timeout
            )
            sock.settimeout(self.timeout)
        except OSError as exc:
            raise TransportError(
                f"could not connect to {self.description}: {exc}"
            ) from exc
        self._sock = sock
        self._read_buffer = b""

    def close(self) -> None:
        sock, self._sock = self._sock, None
        self._read_buffer = b""
        if sock is None:
            return
        try:
            sock.close()
        except Exception:
            pass

    def write_line(self, command: str) -> None:
        if self._sock is None:
            raise TransportError(f"{self.description}: not connected")
        try:
            self._sock.sendall((command.rstrip("\r\n") + "\n").encode("utf-8"))
        except OSError as exc:
            raise TransportError(f"{self.description}: write failed: {exc}") from exc

    def read_line(self) -> str:
        if self._sock is None:
            raise TransportError(f"{self.description}: not connected")
        try:
            while b"\n" not in self._read_buffer:
                chunk = self._sock.recv(4096)
                if not chunk:
                    raise TransportError(
                        f"{self.description}: connection closed by instrument"
                    )
                self._read_buffer += chunk
            raw, self._read_buffer = self._read_buffer.split(b"\n", 1)
        except TransportError:
            raise
        except OSError as exc:
            raise TransportError(f"{self.description}: read failed: {exc}") from exc
        return raw.decode(errors="replace").strip()
