"""Structural interface consumed by instrument drivers."""

from __future__ import annotations

from typing import ContextManager, Optional, Protocol, runtime_checkable


class ScpiSession(Protocol):
    """Minimal SCPI API shared by live, simulated, and test clients."""

    name: str

    @property
    def is_simulated(self) -> bool:
        """Whether this session is backed by a simulated instrument."""
        ...

    def write(self, command: str) -> bool:
        """Send one command and report whether it succeeded."""
        ...

    def query(self, command: str) -> Optional[str]:
        """Send one query and return its response, or ``None`` on failure."""
        ...

    def query_float(self, command: str) -> Optional[float]:
        """Query and parse one finite or non-finite numeric response."""
        ...

    def request_local_control(self) -> bool:
        """Return the instrument to local/front-panel control."""
        ...


@runtime_checkable
class BinaryScpiSession(ScpiSession, Protocol):
    """Optional capability for an atomic, bounded binary conversation."""

    @property
    def is_visa(self) -> bool:
        """Whether VISA owns this session's message framing."""
        ...

    def transaction(self) -> ContextManager[None]:
        """Prevent other calls on this session for the context lifetime."""
        ...

    def query_bytes(
        self,
        command: str,
        *,
        max_bytes: int,
        timeout_s: float,
    ) -> bytes:
        """Send one command and return one bounded binary response."""
        ...


__all__ = ["BinaryScpiSession", "ScpiSession"]
