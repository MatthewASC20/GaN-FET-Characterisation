"""VISA transport for vendor-managed GPIB and other VISA resources."""

from __future__ import annotations

from typing import Any, Optional

from gan_fet.transport.base import (
    BinaryReadUnsupported,
    Transport,
    TransportError,
)


class VisaTransport(Transport):
    """A VISA resource.  VISA itself owns GPIB addressing and termination."""

    is_visa = True

    def __init__(self, resource: str, timeout: float = 5.0):
        self.resource = resource
        self.timeout = timeout
        self._manager: Optional[Any] = None
        self._visa: Optional[Any] = None

    @property
    def description(self) -> str:
        return f"VISA {self.resource}"

    @property
    def is_open(self) -> bool:
        return self._visa is not None

    @property
    def supports_binary_reads(self) -> bool:
        """VISA INSTR resources expose an end-of-message indication.

        VISA ``SOCKET`` and serial (``ASRL``) resources are still byte
        streams.  Supporting those would require instrument-specific framing,
        so they remain explicitly outside this safe capability.
        """
        resource = self.resource.strip().upper()
        return not (
            resource.endswith("::SOCKET") or resource.startswith("ASRL")
        )

    def open(self) -> None:
        if self._visa is not None:
            return
        try:
            import pyvisa
        except ImportError as exc:
            raise TransportError(
                "pyvisa is not installed; cannot open a VISA resource"
            ) from exc

        manager = None
        try:
            manager = pyvisa.ResourceManager()
            visa = manager.open_resource(
                self.resource, timeout=int(self.timeout * 1000)
            )
        except Exception as exc:
            if manager is not None:
                try:
                    manager.close()
                except Exception:
                    pass
            raise TransportError(f"could not open {self.description}: {exc}") from exc
        self._manager = manager
        self._visa = visa

    def close(self) -> None:
        visa, self._visa = self._visa, None
        manager, self._manager = self._manager, None
        if visa is not None:
            try:
                visa.close()
            except Exception:
                pass
        if manager is not None:
            try:
                manager.close()
            except Exception:
                pass

    def write_line(self, command: str) -> None:
        if self._visa is None:
            raise TransportError(f"{self.description}: not connected")
        try:
            self._visa.write(command.rstrip("\r\n"))
        except Exception as exc:
            raise TransportError(f"{self.description}: write failed: {exc}") from exc

    def read_line(self) -> str:
        if self._visa is None:
            raise TransportError(f"{self.description}: not connected")
        try:
            return str(self._visa.read()).strip()
        except Exception as exc:
            raise TransportError(f"{self.description}: read failed: {exc}") from exc

    def query(self, command: str) -> str:
        if self._visa is None:
            raise TransportError(f"{self.description}: not connected")
        try:
            return str(self._visa.query(command.rstrip("\r\n"))).strip()
        except Exception as exc:
            raise TransportError(
                f"{self.description}: query {command!r} failed: {exc}"
            ) from exc

    def read_binary(self, *, max_bytes: int, timeout_s: float) -> bytes:
        """Read one VISA message without exceeding ``max_bytes``.

        ``read_raw`` accepts a chunk size rather than a total bound.  Asking
        VISA for at most ``max_bytes + 1`` via ``read_bytes`` lets us detect an
        oversized response while still stopping on the resource's EOI/message
        boundary.  Binary reads temporarily disable character termination and
        restore both it and the caller's timeout before returning.
        """
        if not self.supports_binary_reads:
            raise BinaryReadUnsupported(
                f"{self.description}: this VISA byte stream has no reliable "
                "binary message boundary"
            )
        if self._visa is None:
            raise TransportError(f"{self.description}: not connected")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
            raise ValueError("max_bytes must be a positive integer")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")

        visa = self._visa
        previous_timeout = visa.timeout
        no_termination = object()
        previous_termination: object = no_termination
        if hasattr(visa, "read_termination"):
            previous_termination = visa.read_termination

        try:
            visa.timeout = int(timeout_s * 1000)
            if previous_termination is not no_termination:
                visa.read_termination = None
            reader = getattr(visa, "read_bytes", None)
            if not callable(reader):
                raise BinaryReadUnsupported(
                    f"{self.description}: VISA resource has no bounded read API"
                )
            payload = bytes(
                reader(max_bytes + 1, break_on_termchar=True)
            )
            if len(payload) > max_bytes:
                raise TransportError(
                    f"{self.description}: binary response exceeds "
                    f"{max_bytes} bytes"
                )
            return payload
        except (BinaryReadUnsupported, TransportError):
            raise
        except Exception as exc:
            raise TransportError(
                f"{self.description}: binary read failed: {exc}"
            ) from exc
        finally:
            try:
                if previous_termination is not no_termination:
                    visa.read_termination = previous_termination
                visa.timeout = previous_timeout
            except Exception as exc:
                raise TransportError(
                    f"{self.description}: could not restore VISA session "
                    f"settings: {exc}"
                ) from exc
