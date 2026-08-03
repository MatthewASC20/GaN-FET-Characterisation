"""Siglent SDM3055 — independent DC bus-voltage measurement.

Kept as a cross-check of the SMU's own voltage readback (the SMU measures at
its terminals; the DMM can be probed at the DUT).
"""

from __future__ import annotations

from typing import Optional

from gan_fet.instruments.base import MultimeterInterface
from gan_fet.scpi.protocol import ScpiSession


class Sdm3055(MultimeterInterface):
    def __init__(self, client: ScpiSession):
        self.client = client

    def dc_voltage(self) -> Optional[float]:
        value = self.client.query_float("MEAS:VOLT:DC?")
        self.client.write("SYST:LOC")
        return value
