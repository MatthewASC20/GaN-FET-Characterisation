"""Prologix GPIB-controller framing layered over TCP or serial."""

from __future__ import annotations

import time

from gan_fet.transport.base import Transport

PREAMBLE_SETTLE_S = 0.3


class PrologixFraming(Transport):
    """Wrap a raw transport in the Prologix ``++`` protocol."""

    handles_prologix_framing = True

    def __init__(
        self,
        inner: Transport,
        gpib_addr: int,
        *,
        auto_read: bool = True,
        read_timeout_ms: int | None = None,
        settle_s: float = PREAMBLE_SETTLE_S,
    ):
        if isinstance(gpib_addr, bool) or not isinstance(gpib_addr, int):
            raise ValueError(f"GPIB address {gpib_addr!r} is not an integer")
        if not 0 <= gpib_addr <= 30:
            raise ValueError(
                f"GPIB address {gpib_addr} is outside the valid range 0-30"
            )
        self.inner = inner
        self.gpib_addr = gpib_addr
        self.auto_read = bool(auto_read)
        self.read_timeout_ms = read_timeout_ms
        self.settle_s = max(0.0, float(settle_s))
        self._configured = False

    @property
    def description(self) -> str:
        return (
            f"{self.inner.description} (Prologix addr {self.gpib_addr}, "
            f"auto {1 if self.auto_read else 0})"
        )

    @property
    def is_open(self) -> bool:
        return self.inner.is_open and self._configured

    @property
    def configuration_commands(self) -> tuple[str, ...]:
        """Controller commands sent before the first instrument command."""
        commands = [
            "++mode 1",
            f"++addr {self.gpib_addr}",
            f"++auto {1 if self.auto_read else 0}",
            "++eoi 1",
        ]
        if self.read_timeout_ms is not None:
            commands.append(f"++read_tmo_ms {self.read_timeout_ms}")
        return tuple(commands)

    def open(self) -> None:
        if self.is_open:
            return
        if not self.inner.is_open:
            self.inner.open()
        try:
            for command in self.configuration_commands:
                self.inner.write_line(command)
        except Exception:
            self.inner.close()
            self._configured = False
            raise
        self._configured = True
        if self.settle_s:
            time.sleep(self.settle_s)

    def close(self) -> None:
        self._configured = False
        self.inner.close()

    def write_line(self, command: str) -> None:
        self.inner.write_line(command)

    def read_line(self) -> str:
        return self.inner.read_line()

    def query(self, command: str) -> str:
        self.inner.write_line(command)
        if not self.auto_read:
            self.inner.write_line("++read eoi")
        return self.inner.read_line()

    def request_local_control(self) -> str:
        command = "++loc"
        self.inner.write_line(command)
        return command
