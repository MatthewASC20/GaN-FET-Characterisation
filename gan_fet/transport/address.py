"""Parse legacy instrument addresses and explicit transport URIs."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlsplit

DEFAULT_BAUD = 9600
DEFAULT_TCP_PORT = 5025
DEFAULT_PROLOGIX_TCP_PORT = 1234
DEFAULT_PROLOGIX_AUTO = True


class AddressError(ValueError):
    """An instrument address could not be understood."""


@dataclass(frozen=True)
class AddressSpec:
    """A resolved byte transport plus optional Prologix framing."""

    kind: str
    target: str
    port: Optional[int] = None
    baud: int = DEFAULT_BAUD
    prologix_addr: Optional[int] = None
    prologix_auto: bool = DEFAULT_PROLOGIX_AUTO
    timeout_s: Optional[float] = None

    @property
    def is_visa(self) -> bool:
        return self.kind == "visa"

    @property
    def is_serial(self) -> bool:
        return self.kind == "serial"

    @property
    def is_tcp(self) -> bool:
        return self.kind == "tcp"

    @property
    def uses_prologix(self) -> bool:
        return self.prologix_addr is not None

    def with_prologix(
        self, gpib_addr: int, *, auto_read: bool = DEFAULT_PROLOGIX_AUTO
    ) -> "AddressSpec":
        """Return this address with Prologix framing enabled.

        VISA already performs GPIB addressing, so it is intentionally never
        wrapped and is returned unchanged.
        """
        if self.is_visa:
            return self
        return replace(
            self,
            prologix_addr=_validate_gpib(gpib_addr),
            prologix_auto=bool(auto_read),
        )

    def describe(self) -> str:
        if self.is_visa:
            base = f"VISA {self.target}"
        elif self.is_serial:
            base = f"serial {self.target}@{self.baud}"
        else:
            base = f"tcp {self.target}:{self.port}"
        if self.uses_prologix:
            base += (
                f" (Prologix addr {self.prologix_addr}, "
                f"auto {1 if self.prologix_auto else 0})"
            )
        return base


def _int_param(params: dict[str, list[str]], key: str) -> Optional[int]:
    values = params.get(key)
    if not values:
        return None
    try:
        return int(values[0])
    except (TypeError, ValueError) as exc:
        raise AddressError(f"{key}={values[0]!r} is not an integer") from exc


def _float_param(params: dict[str, list[str]], key: str) -> Optional[float]:
    values = params.get(key)
    if not values:
        return None
    try:
        value = float(values[0])
    except (TypeError, ValueError) as exc:
        raise AddressError(f"{key}={values[0]!r} is not a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise AddressError(f"timeout={values[0]!r} must be a finite positive number")
    return value


def _validate_gpib(addr: Optional[int]) -> Optional[int]:
    if addr is None:
        return None
    if isinstance(addr, bool) or not isinstance(addr, int):
        raise AddressError(f"GPIB address {addr!r} is not an integer")
    if not 0 <= addr <= 30:
        raise AddressError(f"GPIB address {addr} is outside the valid range 0-30")
    return addr


def _validate_port(value: Any, *, label: str = "TCP port") -> int:
    if isinstance(value, bool):
        raise AddressError(f"{label} {value!r} is not an integer")
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise AddressError(f"{label} {value!r} is not an integer") from exc
    if not 1 <= port <= 65535:
        raise AddressError(f"{label} {port} is outside the valid range 1-65535")
    return port


def _validate_baud(value: Any) -> int:
    if isinstance(value, bool):
        raise AddressError(f"baud rate {value!r} is not a positive integer")
    try:
        baud = int(value)
    except (TypeError, ValueError) as exc:
        raise AddressError(f"baud rate {value!r} is not an integer") from exc
    if baud <= 0:
        raise AddressError(f"baud rate {baud} must be positive")
    return baud


def _parse_auto(params: dict[str, list[str]]) -> bool:
    values = params.get("auto")
    if not values:
        return DEFAULT_PROLOGIX_AUTO
    normalized = values[0].strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise AddressError(
        f"auto={values[0]!r} is invalid; expected 0/1 or false/true"
    )


def parse_uri(uri: str) -> AddressSpec:
    """Parse an explicit TCP, serial, VISA, or Prologix URI."""
    try:
        split = urlsplit(uri)
    except ValueError as exc:
        raise AddressError(f"invalid instrument URI {uri!r}: {exc}") from exc

    scheme = split.scheme.lower()
    prologix = scheme.startswith("prologix+")
    base_scheme = scheme.split("+", 1)[1] if prologix else scheme
    params = parse_qs(split.query, keep_blank_values=True)

    gpib = _validate_gpib(_int_param(params, "addr"))
    if prologix and gpib is None:
        raise AddressError(
            f"{uri!r}: a prologix+ address requires a GPIB address, "
            "for example '?addr=24'"
        )
    if not prologix and ("addr" in params or "auto" in params):
        raise AddressError(
            f"{uri!r}: addr/auto require a prologix+serial or prologix+tcp URI"
        )

    timeout_s = _float_param(params, "timeout")
    auto_read = _parse_auto(params) if prologix else DEFAULT_PROLOGIX_AUTO

    if base_scheme == "visa":
        if prologix:
            raise AddressError(
                "Prologix framing does not apply to VISA resources; VISA "
                "performs GPIB addressing itself"
            )
        resource = unquote(split.netloc + split.path).strip()
        if not resource:
            raise AddressError(f"{uri!r}: missing VISA resource string")
        return AddressSpec(kind="visa", target=resource, timeout_s=timeout_s)

    if base_scheme == "serial":
        device = unquote(split.netloc + split.path).strip()
        if not device:
            raise AddressError(f"{uri!r}: missing serial device")
        baud_raw = _int_param(params, "baud")
        baud = _validate_baud(baud_raw if baud_raw is not None else DEFAULT_BAUD)
        return AddressSpec(
            kind="serial",
            target=device,
            baud=baud,
            prologix_addr=gpib,
            prologix_auto=auto_read,
            timeout_s=timeout_s,
        )

    if base_scheme == "tcp":
        try:
            host = split.hostname
            uri_port = split.port
        except ValueError as exc:
            raise AddressError(f"invalid TCP address {uri!r}: {exc}") from exc
        if not host:
            raise AddressError(f"{uri!r}: missing TCP host")
        default_port = (
            DEFAULT_PROLOGIX_TCP_PORT if prologix else DEFAULT_TCP_PORT
        )
        port = _validate_port(uri_port if uri_port is not None else default_port)
        return AddressSpec(
            kind="tcp",
            target=host,
            port=port,
            prologix_addr=gpib,
            prologix_auto=auto_read,
            timeout_s=timeout_s,
        )

    raise AddressError(
        f"{uri!r}: unsupported scheme {scheme!r}; expected tcp, visa, serial, "
        "prologix+serial, or prologix+tcp"
    )


def parse_address(
    host: str,
    port: int | None = None,
    *,
    prologix_addr: Optional[int] = None,
) -> AddressSpec:
    """Resolve a legacy ``(host, port)`` pair or an explicit URI."""
    if not isinstance(host, str) or not host.strip():
        raise AddressError(f"invalid instrument address {host!r}")
    host = host.strip()

    if "://" in host:
        return parse_uri(host)

    if host.upper().startswith("GPIB") or "::" in host:
        # A VISA resource owns GPIB addressing.  Never apply an external
        # Prologix setting to this path.
        return AddressSpec(kind="visa", target=host)

    if host.upper().startswith("COM") or host.startswith("/"):
        baud = _validate_baud(port if port not in (None, 0) else DEFAULT_BAUD)
        return AddressSpec(
            kind="serial",
            target=host,
            baud=baud,
            prologix_addr=_validate_gpib(prologix_addr),
        )

    tcp_port = _validate_port(port if port not in (None, 0) else DEFAULT_TCP_PORT)
    return AddressSpec(
        kind="tcp",
        target=host,
        port=tcp_port,
        prologix_addr=_validate_gpib(prologix_addr),
    )
