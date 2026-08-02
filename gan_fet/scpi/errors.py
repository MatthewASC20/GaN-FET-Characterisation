"""SCPI-layer exceptions."""

from gan_fet.transport.base import TransportError


class ScpiError(RuntimeError):
    """A SCPI operation failed."""


class ScpiNotConnected(ScpiError):
    """The instrument could not be reached."""


__all__ = ["ScpiError", "ScpiNotConnected", "TransportError"]
