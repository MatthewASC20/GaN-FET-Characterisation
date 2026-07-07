"""Siglent SDM3055 — independent DC bus-voltage measurement.

Kept as a cross-check of the SMU's own voltage readback (the SMU measures at
its terminals; the DMM can be probed at the DUT).
"""

from __future__ import annotations

from typing import Optional

from gan_fet.instruments.scpi import ScpiTcpClient


class Sdm3055:
    def __init__(self, client: ScpiTcpClient):
        self.client = client

    def dc_voltage(self) -> Optional[float]:
        value = self.client.query_float("MEAS:VOLT:DC?")
        self.client.write("SYST:LOC")
        return value
