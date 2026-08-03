"""Backward-compatible SCPI client constructor.

The historical name is retained because the application and external scripts
construct ``ScpiTcpClient(name, host, port)``.  The resolved connection can now
be TCP, serial, or VISA, while the public write/query API remains unchanged.
"""

from __future__ import annotations

import logging

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
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        try:
            self.spec = parse_address(host, port)
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


__all__ = ["ScpiTcpClient"]
