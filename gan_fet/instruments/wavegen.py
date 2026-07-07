"""Siglent SDG6022X waveform generator — gates the FET(s).

Channel 1 drives the (first) device; channel 2 mirrors it in Dual
Conduction mode. Configuration leaves outputs OFF (the operator enables
them once the rig is ready), matching the original workflow.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from gan_fet.instruments.scpi import ScpiTcpClient

log = logging.getLogger(__name__)

# Gate-drive waveform fixed by the rig: square, 6.5 Vpp, +1.9 V offset.
_BSWV = "BSWV WVTP,SQUARE,FRQ,{freq},AMP,6.5VPP,OFST,1.9V,DUTY,{duty},PHSE,0"


class Sdg6022x:
    def __init__(self, client: ScpiTcpClient):
        self.client = client

    @staticmethod
    def is_dual(config: str) -> bool:
        return config == "Dual Conduction"

    def configure(self, config: str, freq_hz: float, duty_pct: float) -> None:
        """Apply the full channel setup for a rig configuration. Outputs stay OFF."""
        if config not in ("Dual Conduction", "Single Conduction", "Single Device"):
            raise ValueError(f"Unknown configuration: {config}")

        self.outputs_off()
        self.client.write("C1:" + _BSWV.format(freq=int(freq_hz), duty=duty_pct))
        time.sleep(0.2)

        if config == "Dual Conduction":
            self.client.write("C2:" + _BSWV.format(freq=int(freq_hz), duty=duty_pct))
            self.client.write("COUP STATE,ON")
            self.client.write("COUP FCOUP,ON")
            self.client.write("COUP DCOUP,ON")
        elif config == "Single Conduction":
            self.client.write("COUP STATE,OFF")
            self.client.write("C2:BSWV WVTP,DC,OFST,0")
            time.sleep(0.2)
        # "Single Device": channel 2 stays off, nothing to configure.

        time.sleep(0.3)
        log.info("Wavegen configured: %s, %.0f Hz, %s%% duty", config, freq_hz, duty_pct)

    def set_frequency(self, freq_hz: float, dual: bool) -> None:
        freq = int(round(freq_hz))
        self.client.write(f"C1:BSWV FRQ,{freq}")
        if dual:
            self.client.write(f"C2:BSWV FRQ,{freq}")

    def set_duty(self, duty_pct: float, dual: bool) -> None:
        self.client.write(f"C1:BSWV DUTY,{duty_pct}")
        if dual:
            self.client.write(f"C2:BSWV DUTY,{duty_pct}")

    def read_frequency(self) -> Optional[float]:
        """Parse the FRQ value out of a C1:BSWV? response."""
        resp = self.client.query("C1:BSWV?")
        if not resp:
            return None
        tokens = [t.strip() for t in resp.split(",")]
        for i, token in enumerate(tokens):
            if token.upper().endswith("FRQ") and i + 1 < len(tokens):
                try:
                    return float(tokens[i + 1].upper().replace("HZ", ""))
                except ValueError:
                    return None
        return None

    def outputs_off(self) -> None:
        self.client.write("C1:OUTP OFF")
        self.client.write("C2:OUTP OFF")
