"""Line-oriented byte transport primitives for bench instruments.

Transports own connection and wire framing.  They deliberately know nothing
about SCPI retries, command logging, or instrument semantics; those concerns
live in :mod:`gan_fet.scpi` and the instrument drivers above it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class TransportError(RuntimeError):
    """A transport could not be opened or an I/O operation failed."""


class BinaryReadUnsupported(TransportError):
    """A transport cannot preserve boundaries for a binary response."""


class Transport(ABC):
    """One line-oriented connection to one instrument."""

    handles_prologix_framing: bool = False
    is_visa: bool = False
    is_simulated: bool = False

    @property
    def supports_binary_reads(self) -> bool:
        """Whether this transport can return one bounded binary message.

        Line-oriented transports intentionally default to ``False``.  A raw
        TCP stream has no message boundary, and treating a timeout or newline
        as the end of arbitrary binary data would corrupt the next SCPI
        conversation.
        """
        return False

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable target used in logs and error messages."""

    @property
    @abstractmethod
    def is_open(self) -> bool:
        """Whether the connection is ready for I/O."""

    @abstractmethod
    def open(self) -> None:
        """Open the connection.  Implementations must be idempotent."""

    @abstractmethod
    def close(self) -> None:
        """Release the connection without raising."""

    @abstractmethod
    def write_line(self, command: str) -> None:
        """Send one command, raising :class:`TransportError` on failure."""

    @abstractmethod
    def read_line(self) -> str:
        """Read and strip one response line."""

    def query(self, command: str) -> str:
        """Send a command and read its reply."""
        self.write_line(command)
        return self.read_line()

    def read_binary(self, *, max_bytes: int, timeout_s: float) -> bytes:
        """Read one bounded binary message from an already-open transport.

        Concrete implementations must have a trustworthy message boundary.
        Unsupported transports fail before callers send a binary-producing
        command.
        """
        del max_bytes, timeout_s
        raise BinaryReadUnsupported(
            f"{self.description} does not support bounded binary messages"
        )

    def request_local_control(self) -> str:
        """Return an instrument to local/front-panel control.

        The returned string is the actual command sent and is used by the
        SCPI layer for observability.  Protocol wrappers may override this
        hook when local control is a controller operation rather than SCPI.
        """
        command = ":SYST:LOC"
        self.write_line(command)
        return command
