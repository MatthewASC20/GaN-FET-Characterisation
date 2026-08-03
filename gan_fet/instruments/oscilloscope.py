"""Teledyne LeCroy HDO4054 measurement and screenshot driver."""

from __future__ import annotations

import logging
import math
import os
import tempfile
import zlib
from pathlib import Path
from typing import Any, Optional

from gan_fet.instruments.base import OscilloscopeInterface
from gan_fet.scpi.protocol import BinaryScpiSession, ScpiSession

log = logging.getLogger(__name__)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SCREENSHOT_TIMEOUT_S = 15.0
_MAX_SCREENSHOT_BYTES = 32 * 1024 * 1024


def _validate_png(payload: bytes) -> bytes:
    """Return one complete PNG, rejecting truncation, corruption, or junk."""
    if not payload.startswith(_PNG_SIGNATURE):
        raise ValueError("scope capture does not contain a PNG signature")

    offset = len(_PNG_SIGNATURE)
    chunk_index = 0
    saw_idat = False
    while True:
        if offset + 12 > len(payload):
            raise ValueError("scope capture contains a truncated PNG chunk")
        length = int.from_bytes(payload[offset : offset + 4], "big")
        chunk_type = payload[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(payload):
            raise ValueError("scope capture contains a truncated PNG chunk")

        chunk_data = payload[offset + 8 : offset + 8 + length]
        expected_crc = int.from_bytes(
            payload[offset + 8 + length : chunk_end], "big"
        )
        actual_crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            raise ValueError("scope capture contains a corrupt PNG chunk")

        if chunk_index == 0 and (chunk_type != b"IHDR" or length != 13):
            raise ValueError("scope capture has no valid PNG IHDR chunk")
        if chunk_type == b"IDAT":
            saw_idat = True
        if chunk_type == b"IEND":
            if length != 0 or not saw_idat:
                raise ValueError("scope capture has no valid PNG image end")
            trailing = payload[chunk_end:]
            if trailing not in (b"", b"\n", b"\r\n"):
                raise ValueError("scope capture has bytes after the PNG image end")
            return payload[:chunk_end]

        offset = chunk_end
        chunk_index += 1


def _decode_png_block(raw: bytes) -> bytes:
    """Decode a raw PNG or an IEEE-488.2 definite-length binary block."""
    if raw.startswith(_PNG_SIGNATURE):
        payload = raw
    elif raw.startswith(b"#"):
        if len(raw) < 2 or not raw[1:2].isdigit():
            raise ValueError("invalid IEEE-488.2 block header")
        length_digits = int(raw[1:2])
        if length_digits == 0:
            payload = raw[2:].rstrip(b"\r\n")
        else:
            header_end = 2 + length_digits
            if len(raw) < header_end:
                raise ValueError("truncated IEEE-488.2 length header")
            length_field = raw[2:header_end]
            if not length_field.isdigit():
                raise ValueError("invalid IEEE-488.2 payload length")
            payload_length = int(length_field)
            payload_end = header_end + payload_length
            if len(raw) < payload_end:
                raise ValueError("truncated IEEE-488.2 payload")
            trailing = raw[payload_end:]
            if trailing not in (b"", b"\n", b"\r\n"):
                raise ValueError("IEEE-488.2 block has trailing bytes")
            payload = raw[header_end:payload_end]
    else:
        raise ValueError("scope capture was not a PNG or IEEE-488.2 block")

    return _validate_png(payload)


def _configured_visa_session(
    client: ScpiSession,
) -> Optional[BinaryScpiSession]:
    """Return the exact configured session when VISA owns its framing."""
    if isinstance(client, BinaryScpiSession) and client.is_visa:
        return client
    return None


def _write_bytes_atomic(dest_path: Path, payload: bytes) -> None:
    """Write a capture without exposing a partial final PNG."""
    fd, temp_name = tempfile.mkstemp(
        dir=dest_path.parent,
        prefix=f".{dest_path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, dest_path)
    finally:
        temp_path.unlink(missing_ok=True)


class LeCroyHdo4054(OscilloscopeInterface):
    """Teledyne LeCroy HDO4054 with preconfigured MAUI measurements.

    The parameter slots are a required bench configuration, not intrinsic
    meanings assigned by the oscilloscope:

    * P1 = Vds peak voltage
    * P2 = RMS current
    * P3 = switch-current RMS
    * P4 = ZVS dwell, as a duty at level on Vds (optional)

    P1-P3 are read only. P4 is the one exception to that rule: its threshold
    must track the target Vds peak, because a level that is 2.5% of the swing
    at 400 V is 5% at 200 V, and an operator reconfiguring the scope at every
    matrix point is a reliable source of silently wrong data. The measurement
    itself is still operator-defined; only its level is written.
    """

    #: Parameter slot carrying the ZVS dwell measurement.
    ZVS_PARAMETER = "P4"

    def __init__(self, client: ScpiSession, powi_scope: Optional[Any] = None):
        """Create the driver with an optional injected screenshot adapter.

        The application no longer mutates ``sys.path`` to discover a private
        workstation library. Deployments that need such an adapter can inject
        an object exposing ``print_screen(path)`` from the composition layer.
        """
        self.client = client
        self.powi_scope = powi_scope

    def verify_identity(self) -> str:
        """Verify that the connected instrument is exactly an HDO4054.

        MAUI may include the ``*IDN`` command header in the response depending
        on its communication-header setting.  Model suffixes identify a
        different instrument and are intentionally rejected.
        """
        raw_identity = self.client.query("*IDN?")
        if raw_identity is None or not raw_identity.strip():
            raise ConnectionError("HDO4054 did not respond to *IDN?")

        identity = raw_identity.strip()
        payload = identity
        if payload.upper().startswith("*IDN "):
            payload = payload[5:].strip()
        fields = [field.strip() for field in payload.split(",")]
        if len(fields) < 2:
            raise ConnectionError(
                f"Malformed HDO4054 *IDN? response: {identity!r}"
            )

        manufacturer, model = fields[0].upper(), fields[1].upper()
        if manufacturer != "LECROY" or model != "HDO4054":
            raise ConnectionError(
                "Expected LECROY HDO4054, received "
                f"{identity!r}"
            )
        return identity

    def peak_voltage(self) -> Optional[float]:
        """Query P1 peak voltage parameter."""
        return self.client.query_float(
            "VBS? 'return=app.Measure.P1.Out.Result.Value'"
        )

    def rms_current(self) -> Optional[float]:
        """Query P2 RMS current parameter (CP030 current probe)."""
        return self.client.query_float(
            "VBS? 'return=app.Measure.P2.Out.Result.Value'"
        )

    def isw_rms(self) -> Optional[float]:
        """Query P3 switch-current RMS parameter (CP030 current probe)."""
        return self.client.query_float(
            "VBS? 'return=app.Measure.P3.Out.Result.Value'"
        )

    def zvs_dwell_fraction(self) -> Optional[float]:
        """Query the P4 ZVS dwell parameter, normalised to 0..1.

        MAUI reports a duty as a percentage. An unconfigured or unmeasurable
        parameter yields ``None`` rather than zero: "no dwell" and "no
        measurement" are opposite conclusions about ZVS, and conflating them
        would report hard switching whenever the scope was misconfigured.
        """
        raw = self.client.query_float(
            f"VBS? 'return=app.Measure.{self.ZVS_PARAMETER}.Out.Result.Value'"
        )
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or value < 0.0:
            return None
        # Tolerate either convention; anything above 1 is read as a percentage.
        fraction = value / 100.0 if value > 1.0 else value
        return min(1.0, fraction)

    def set_zvs_threshold(self, volts: float) -> bool:
        """Set the P4 level that defines 'Vds at zero'.

        Written rather than read because it must track the target peak. A
        failure is reported, not raised: the dwell measurement is diagnostic,
        and losing it must never stop a characterisation run.
        """
        level = float(volts)
        if not math.isfinite(level) or level <= 0.0:
            log.warning("refusing to set a non-positive ZVS threshold: %r", volts)
            return False
        ok = self.client.write(
            f"VBS 'app.Measure.{self.ZVS_PARAMETER}.Operator.LevelType = \"Absolute\"'"
        ) and self.client.write(
            f"VBS 'app.Measure.{self.ZVS_PARAMETER}.Operator.AbsLevel = {level:.4f}'"
        )
        if not ok:
            log.warning("scope did not accept a %.2f V ZVS threshold", level)
        return bool(ok)

    def _screenshot_configured_visa(self, dest_path: Path) -> Optional[Path]:
        session = _configured_visa_session(self.client)
        if session is None:
            return None
        try:
            with session.transaction():
                if not session.write(
                    "HCSU DEV,PNG,AREA,GRIDAREAONLY,DEST,REMOTE"
                ):
                    raise ConnectionError("LeCroy hardcopy setup was not accepted")
                raw = session.query_bytes(
                    "SCDP",
                    max_bytes=_MAX_SCREENSHOT_BYTES,
                    timeout_s=_SCREENSHOT_TIMEOUT_S,
                )
            _write_bytes_atomic(dest_path, _decode_png_block(raw))
            log.info(
                "HDO4054 screenshot saved to %s through configured VISA session",
                dest_path,
            )
            return dest_path
        except Exception as exc:
            log.warning(
                "HDO4054 configured-session screenshot failed: %s",
                exc,
            )
            return None

    def _screenshot_powi(self, dest_path: Path) -> Path:
        """Capture through an injected adapter without exposing partial data."""
        adapter = self.powi_scope
        if adapter is None:
            raise RuntimeError("no screenshot adapter is configured")
        fd, temp_name = tempfile.mkstemp(
            dir=dest_path.parent,
            prefix=f".{dest_path.name}.",
            suffix=".png",
        )
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            adapter.print_screen(str(temp_path))
            payload = _decode_png_block(temp_path.read_bytes())
            _write_bytes_atomic(dest_path, payload)
            return dest_path
        finally:
            temp_path.unlink(missing_ok=True)

    def screenshot(self, dest_path: Path) -> Optional[Path]:
        """Capture the HDO4054 display as a validated PNG."""
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        # Software-in-the-loop renders directly from its in-process electrical
        # plant.  This branch always returns, so a missing or failed renderer
        # can never fall through into a real VISA or injected adapter path.
        if getattr(self.client, "is_simulated", False):
            renderer = getattr(self.client, "simulated_screenshot_png", None)
            if not callable(renderer):
                log.warning("Simulated HDO4054 session has no capture renderer")
                return None
            try:
                payload = renderer()
                _write_bytes_atomic(dest_path, _decode_png_block(payload))
                log.info("Simulated HDO4054 screenshot saved to %s", dest_path)
                return dest_path
            except Exception as exc:
                log.warning("Simulated HDO4054 screenshot failed: %s", exc)
                return None

        if self.powi_scope is not None:
            try:
                self._screenshot_powi(dest_path)
                log.info("HDO4054 screenshot saved to %s via adapter", dest_path)
                return dest_path
            except Exception as exc:
                log.warning(
                    "HDO4054 screenshot adapter failed: %s; falling back to SCPI",
                    exc,
                )

        configured_session = _configured_visa_session(self.client)
        if configured_session is not None:
            return self._screenshot_configured_visa(dest_path)

        log.warning(
            "HDO4054 screenshot capture requires the configured VISA session"
        )
        return None


__all__ = ["LeCroyHdo4054"]
