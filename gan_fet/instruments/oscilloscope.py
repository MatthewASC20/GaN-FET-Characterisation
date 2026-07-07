"""Tektronix MSO44 — reads the rig's pre-configured measurement slots and
captures screenshots.

Slot assignment on the scope (unchanged from v1):
    MEAS6 = Vds peak voltage, MEAS7 = RMS current, MEAS9 = switch-current RMS
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from gan_fet.instruments.scpi import ScpiTcpClient

log = logging.getLogger(__name__)


class Mso44:
    def __init__(self, client: ScpiTcpClient):
        self.client = client

    def rms_current(self) -> Optional[float]:
        return self.client.query_float("MEASUrement:MEAS7:VALue?")

    def peak_voltage(self) -> Optional[float]:
        return self.client.query_float("MEASUrement:MEAS6:VALue?")

    def isw_rms(self) -> Optional[float]:
        return self.client.query_float("MEASUrement:MEAS9:VALue?")

    def screenshot(self, dest_path: Path) -> Optional[Path]:
        """Save a PNG of the scope display to dest_path (via pyvisa)."""
        try:
            import pyvisa
        except ImportError:
            log.warning("pyvisa not installed; skipping screenshot")
            return None

        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        temp_on_scope = f"C:/{dest_path.name}"
        resource = f"TCPIP0::{self.client.host}::inst0::INSTR"

        rm = scope = None
        try:
            rm = pyvisa.ResourceManager("@py")
            scope = rm.open_resource(resource)
            scope.timeout = 15000
            scope.write("SAVE:IMAGE:FILEFORMAT PNG")
            scope.write(f'SAVE:IMAGE "{temp_on_scope}"')
            scope.query("*OPC?")
            scope.write(f'FILESystem:READFile "{temp_on_scope}"')
            img_data = scope.read_raw(1024 * 1024)
            dest_path.write_bytes(img_data)
            scope.write(f'FILESystem:DELEte "{temp_on_scope}"')
            log.info("Screenshot saved to %s", dest_path)
            return dest_path
        except Exception as exc:
            log.warning("Oscilloscope screenshot failed: %s", exc)
            return None
        finally:
            for closable in (scope, rm):
                try:
                    if closable is not None:
                        closable.close()
                except Exception:
                    pass
