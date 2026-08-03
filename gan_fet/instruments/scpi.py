"""Backward-compatible SCPI client constructor.

The historical name is retained because the application and external scripts
construct ``ScpiTcpClient(name, host, port)``.  The resolved connection can now
be TCP, serial, or VISA, while the public write/query API remains unchanged.
"""

from __future__ import annotations

import logging
from typing import Optional

from gan_fet.scpi.client import ScpiClient as _ScpiClient
from gan_fet.transport.address import AddressError, parse_address
from gan_fet.transport.factory import transport_for_spec

log = logging.getLogger(__name__)


class ScpiTcpClient(_ScpiClient):
    """A SCPI client addressed by the legacy ``(host, port)`` API."""

    def __init__(
        self,
        name: str,
        host: str,
        port: int,
        timeout: float = 5.0,
        *,
        prologix_addr: Optional[int] = None,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        try:
            self.spec = parse_address(host, port, prologix_addr=prologix_addr)
        except AddressError:
            log.exception("%s: could not parse address %r", name, host)
            raise
        super().__init__(name, transport_for_spec(self.spec, timeout=timeout))

    @property
    def is_visa(self) -> bool:
        return self.spec.is_visa

    @property
    def is_serial(self) -> bool:
        return self.spec.is_serial

    @property
    def uses_prologix(self) -> bool:
        return self.spec.uses_prologix

    def configure_prologix(self, gpib_addr: int, *, auto_read: bool = True) -> bool:
        """Enable controller framing for a legacy TCP/serial address.

        This method is intentionally a no-op for VISA resources.  Calling it
        from a driver is therefore safe even when the settings retain their
        historical default GPIB address while the bench uses direct VISA.
        """
        with self._lock:
            if self.spec.is_visa:
                return False
            updated = self.spec.with_prologix(gpib_addr, auto_read=auto_read)
            if updated == self.spec and self.transport.handles_prologix_framing:
                return True

            # Framing must be in place before the next byte is sent.  Rebuild
            # from the immutable address spec and force a clean reconnect if
            # a caller configured an already-open legacy client.
            self.transport.close()
            self.spec = updated
            self.transport = transport_for_spec(updated, timeout=self.timeout)
            return True


# Historical alias retained for imports from gan_fet.instruments.scpi.
__all__ = ["ScpiTcpClient"]
