"""Fast, non-invasive reachability checks for configured instruments.

The probe deliberately opens only TCP sockets.  VISA and serial resources
cannot be tested without opening the real instrument session, which belongs
to startup diagnostics rather than a latency-sensitive GUI preflight.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import Callable

from gan_fet.settings import Settings
from gan_fet.transport.address import AddressError, parse_address


@dataclass(frozen=True)
class ProbeFailure:
    instrument: str
    address: str
    reason: str


@dataclass
class ProbeReport:
    checked_tcp: list[str] = field(default_factory=list)
    skipped_non_tcp: list[str] = field(default_factory=list)
    failures: list[ProbeFailure] = field(default_factory=list)

    @property
    def reachable(self) -> bool:
        return not self.failures


SocketConnector = Callable[..., object]


def probe_configured_instruments(
    settings: Settings,
    *,
    timeout_s: float = 0.5,
    connector: SocketConnector = socket.create_connection,
) -> ProbeReport:
    """Probe configured TCP endpoints and classify the rest without opening them."""
    report = ProbeReport()
    for name, address in settings.instruments.items():
        try:
            spec = parse_address(address.ip, address.port)
        except AddressError as exc:
            report.failures.append(
                ProbeFailure(name, str(address.ip), f"invalid address: {exc}")
            )
            continue

        if not spec.is_tcp:
            report.skipped_non_tcp.append(name)
            continue

        display = spec.describe()
        try:
            connection = connector((spec.target, spec.port), timeout=timeout_s)
            close = getattr(connection, "close", None)
            if callable(close):
                close()
            report.checked_tcp.append(name)
        except OSError as exc:
            report.failures.append(ProbeFailure(name, display, str(exc)))
    return report
