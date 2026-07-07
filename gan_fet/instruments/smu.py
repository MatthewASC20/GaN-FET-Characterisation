"""Keithley 2400-series SMU (2400/2410/...) — DC bus source AND input-current meter.

Replaces both the manual bench PSU and the legacy Prologix-USB GPIB ammeter.
Reached over the network via a GPIB/serial-to-LAN bridge:

* transparent bridge → plain SCPI over the TCP socket (leave
  `prologix_gpib_addr` unset);
* Prologix GPIB-ETHERNET → set `prologix_gpib_addr`; the driver sends the
  `++` framing commands after connecting (default bridge port 1234).

The SMU sources voltage and senses current, so a single `:READ?` yields the
bus voltage at its terminals and the DC input current of the rig.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from gan_fet.instruments.scpi import ScpiTcpClient
from gan_fet.settings import SmuSettings

log = logging.getLogger(__name__)


class SmuLimitError(ValueError):
    """Requested setpoint is outside the configured safe envelope."""


class Keithley2400:
    def __init__(self, client: ScpiTcpClient, settings: SmuSettings):
        self.client = client
        self.settings = settings
        self._setpoint_v = 0.0
        self._output_on = False
        self._initialized = False

    # -- lifecycle -------------------------------------------------------

    def initialize(self) -> bool:
        """Configure as a voltage source measuring current. Output stays OFF."""
        if self.settings.prologix_gpib_addr is not None:
            for cmd in ("++mode 1",
                        f"++addr {self.settings.prologix_gpib_addr}",
                        "++auto 1",
                        "++eoi 1"):
                if not self.client.write(cmd):
                    return False
            time.sleep(0.3)

        setup = (
            "*RST",
            "*CLS",
            ":SOUR:FUNC VOLT",
            ":SOUR:VOLT:MODE FIXED",
            f":SOUR:VOLT:RANG {self.settings.max_voltage_v}",
            ":SOUR:VOLT 0",
            ':SENS:FUNC "CURR"',
            f":SENS:CURR:PROT {self.settings.current_compliance_a}",
            ":SENS:CURR:RANG:AUTO ON",
            f":SENS:CURR:NPLC {self.settings.nplc}",
            ":FORM:ELEM VOLT,CURR",
        )
        for cmd in setup:
            if not self.client.write(cmd):
                log.error("SMU initialisation failed at '%s'", cmd)
                return False
        self._setpoint_v = 0.0
        self._output_on = False
        self._initialized = True
        log.info(
            "SMU initialised (limit %.0f V, compliance %.3g A, NPLC %.2g)",
            self.settings.max_voltage_v,
            self.settings.current_compliance_a,
            self.settings.nplc,
        )
        return True

    def ensure_initialized(self) -> bool:
        return self._initialized or self.initialize()

    # -- source ------------------------------------------------------------

    @property
    def setpoint_v(self) -> float:
        return self._setpoint_v

    @property
    def output_is_on(self) -> bool:
        return self._output_on

    def set_voltage(self, volts: float) -> None:
        if not (0 <= volts <= self.settings.max_voltage_v):
            raise SmuLimitError(
                f"Setpoint {volts:.1f} V outside 0..{self.settings.max_voltage_v:.0f} V"
            )
        if not self.client.write(f":SOUR:VOLT {volts:.4f}"):
            raise ConnectionError("SMU write failed while setting voltage")
        self._setpoint_v = float(volts)

    def ramp_to(
        self,
        volts: float,
        *,
        step_v: Optional[float] = None,
        delay_s: Optional[float] = None,
        cancel_check=None,
    ) -> None:
        """Soft ramp from the current setpoint to `volts` (all changes are
        rate-limited through here; direct set_voltage is for small trims)."""
        step = abs(step_v if step_v is not None else self.settings.ramp_step_v)
        delay = delay_s if delay_s is not None else self.settings.ramp_delay_s
        if step <= 0:
            self.set_voltage(volts)
            return

        current = self._setpoint_v
        direction = 1 if volts > current else -1
        while abs(volts - current) > step:
            if cancel_check is not None and cancel_check():
                return
            current += direction * step
            self.set_voltage(current)
            time.sleep(delay)
        self.set_voltage(volts)

    def output_on(self) -> bool:
        """Enable the output — always from a 0 V setpoint (soft start)."""
        if not self.ensure_initialized():
            return False
        if not self._output_on:
            self.set_voltage(0.0)
            if not self.client.write(":OUTP ON"):
                return False
            self._output_on = True
        return True

    def output_off(self) -> bool:
        ok = self.client.write(":OUTP OFF")
        if ok:
            self._output_on = False
            self._setpoint_v = 0.0
        return ok

    def emergency_off(self) -> None:
        """Best-effort kill: never raises."""
        try:
            self.client.write(":ABOR")
            self.client.write(":OUTP OFF")
            self._output_on = False
            self._setpoint_v = 0.0
        except Exception:
            pass

    # -- measure -------------------------------------------------------------

    def read(self) -> Optional[tuple[float, float]]:
        """Trigger a measurement; returns (voltage_v, current_a) or None."""
        raw = self.client.query(":READ?")
        if not raw:
            return None
        parts = raw.split(",")
        try:
            return float(parts[0]), float(parts[1])
        except (IndexError, ValueError):
            log.warning("SMU: unparseable :READ? response: %r", raw)
            return None

    def measure_dc_current(self) -> Optional[float]:
        result = self.read()
        return result[1] if result else None

    def compliance_tripped(self) -> bool:
        raw = self.client.query(":SENS:CURR:PROT:TRIP?")
        return raw is not None and raw.strip().lstrip("+") .startswith("1")

    def go_local(self) -> None:
        if self.settings.prologix_gpib_addr is not None:
            self.client.write("++loc")
        else:
            self.client.write(":SYST:LOC")
