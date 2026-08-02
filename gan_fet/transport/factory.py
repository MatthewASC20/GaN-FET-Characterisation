"""Construct concrete transports from parsed addresses."""

from __future__ import annotations

from typing import Optional

from gan_fet.transport.address import AddressError, AddressSpec, parse_address
from gan_fet.transport.base import Transport
from gan_fet.transport.prologix import PrologixFraming
from gan_fet.transport.serial_port import SerialTransport
from gan_fet.transport.tcp import TcpTransport
from gan_fet.transport.visa_resource import VisaTransport


def transport_for_spec(spec: AddressSpec, timeout: float = 5.0) -> Transport:
    """Build the transport represented by ``spec``."""
    effective_timeout = spec.timeout_s if spec.timeout_s is not None else timeout

    if spec.kind == "visa":
        # Direct VISA must never see Prologix controller commands.
        return VisaTransport(spec.target, timeout=effective_timeout)
    if spec.kind == "serial":
        base: Transport = SerialTransport(
            spec.target, baud=spec.baud, timeout=effective_timeout
        )
    elif spec.kind == "tcp":
        base = TcpTransport(
            spec.target, port=spec.port or 5025, timeout=effective_timeout
        )
    else:
        raise AddressError(f"unsupported transport kind {spec.kind!r}")

    if spec.uses_prologix:
        assert spec.prologix_addr is not None
        return PrologixFraming(
            base,
            spec.prologix_addr,
            auto_read=spec.prologix_auto,
        )
    return base


def create_transport(
    host: str,
    port: Optional[int] = None,
    *,
    timeout: float = 5.0,
    prologix_addr: Optional[int] = None,
) -> Transport:
    spec = parse_address(host, port, prologix_addr=prologix_addr)
    return transport_for_spec(spec, timeout=timeout)
