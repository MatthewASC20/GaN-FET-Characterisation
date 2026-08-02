"""Raw serial transport for line-oriented bench instruments."""

from __future__ import annotations

from typing import Any, Optional

from gan_fet.transport.base import Transport, TransportError


class SerialTransport(Transport):
    def __init__(self, device: str, baud: int = 9600, timeout: float = 5.0):
        self.device = device
        self.baud = baud
        self.timeout = timeout
        self._ser: Optional[Any] = None

    @property
    def description(self) -> str:
        return f"{self.device}@{self.baud}"

    @property
    def is_open(self) -> bool:
        return self._ser is not None

    def open(self) -> None:
        if self._ser is not None:
            return
        try:
            import serial
        except ImportError as exc:
            raise TransportError(
                "pyserial is not installed; cannot open a serial instrument"
            ) from exc
        try:
            self._ser = serial.Serial(
                self.device, baudrate=self.baud, timeout=self.timeout
            )
        except Exception as exc:
            self._ser = None
            raise TransportError(
                f"could not open serial port {self.description}: {exc}"
            ) from exc

    def close(self) -> None:
        serial_port, self._ser = self._ser, None
        if serial_port is None:
            return
        try:
            serial_port.close()
        except Exception:
            pass

    def write_line(self, command: str) -> None:
        if self._ser is None:
            raise TransportError(f"{self.description}: not connected")
        try:
            payload = (command.rstrip("\r\n") + "\r\n").encode("utf-8")
            self._ser.write(payload)
        except Exception as exc:
            raise TransportError(f"{self.description}: write failed: {exc}") from exc

    def read_line(self) -> str:
        if self._ser is None:
            raise TransportError(f"{self.description}: not connected")
        try:
            raw = self._ser.readline()
        except Exception as exc:
            raise TransportError(f"{self.description}: read failed: {exc}") from exc
        if raw == b"":
            raise TransportError(
                f"{self.description}: read timed out after {self.timeout:g}s"
            )
        return raw.decode(errors="replace").strip()
